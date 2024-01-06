from asyncio import Future, create_task, sleep
from functools import partial, reduce
from operator import xor
from time import time
from types import MappingProxyType
from typing import NoReturn, Optional, Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.future import select
from sqlalchemy.orm import declarative_base, sessionmaker
from tecoroute.connector import (
    ConnectorError,
    ConnectorPlcError,
    ConnectorUserError,
    UdpConnector,
)

from ._misc import APPLICATION, CONNECTOR_HOST, logger

Base: Any = declarative_base()


class _Plc(Base):  # type: ignore
    __tablename__ = "plc"
    id = Column(Integer, autoincrement=True, nullable=False, primary_key=True)
    name = Column(String, nullable=False)
    serial_number = Column(String, nullable=False)
    teco_online_username = Column(String, nullable=False)
    teco_online_password = Column(String, nullable=False)
    teco_online_plc_name = Column(String, nullable=False)
    communication_type = Column(String)
    trc_server_id = Column(Integer)
    port = Column(Integer)
    services_enabled = Column(Boolean, nullable=False)

    def __hash__(self) -> int:
        return reduce(
            xor,
            (
                hash(attr)
                for attr in (
                    self.id,
                    self.teco_online_username,
                    self.teco_online_password,
                    self.teco_online_plc_name,
                    self.port,
                )
            ),
        )

    def __eq__(self, other: object) -> bool:
        return hash(self) == hash(other)


class Manager:
    def __init__(
        self,
        database_url: str,
        communication_type: str,
        trc_server_id: int,
        host: str = CONNECTOR_HOST,
        application: str = APPLICATION,
        debug_id: Optional[int] = None,
    ) -> None:
        self._database_url = urlsplit(database_url, scheme="mysql+aiomysql")
        self._communication_type = communication_type
        self._trc_server_id = trc_server_id
        self._host = host
        self._application = application
        self._debug_id = debug_id
        self._connectors: dict[_Plc, UdpConnector] = {}
        self._postpones: dict[_Plc, float] = {}

    def _delete_connector(self, plc: _Plc, future: Future[NoReturn]) -> None:
        connector = self._connectors[plc]
        future.cancel()
        if not future.cancelled():
            e = future.exception()
            if isinstance(e, (ConnectorUserError, ConnectorPlcError)):
                self._postpones[plc] = time() + 600
                logger.info(
                    f"PLC {plc.id} postponed by 600 seconds due to {e.error_code}"
                )
            if not isinstance(e, ConnectorError):
                logger.error(
                    f"Connector {connector} closed with error: {type(e).__name__}: {e}"
                )
        del self._connectors[plc]
        logger.info(f"Connector {connector} deleted")

    @property
    def connectors(self) -> MappingProxyType[_Plc, UdpConnector]:
        return MappingProxyType(self._connectors)

    def close_connector(self, plc_id: int, info: str = "") -> None:
        try:
            connector = next(
                self._connectors[plc] for plc in self._connectors if plc.id == plc_id
            )
        except StopIteration:
            raise KeyError from None
        connector.close()
        info_msg = f" ({info})" if info else ""
        logger.info(f"Connector {connector} closed" + info_msg)

    async def run(self) -> NoReturn:
        engine = create_async_engine(urlunsplit(self._database_url))
        Db = sessionmaker(engine, class_=AsyncSession)  # noqa: N806

        while True:
            t0 = time()

            async with Db() as db:
                stmt = select(_Plc)
                if self._debug_id is not None:
                    stmt = stmt.where(_Plc.id == self._debug_id)
                else:
                    stmt = stmt.where(
                        _Plc.communication_type == self._communication_type,
                        _Plc.trc_server_id == self._trc_server_id,
                        _Plc.services_enabled == True,  # noqa: E712
                    )
                plcs = set((await db.execute(stmt)).scalars())

                # Delete expired postpones
                for plc in set(self._postpones):
                    current_time = time()
                    if self._postpones[plc] < current_time:
                        del self._postpones[plc]

                # Run connectors of created and enabled PLCs
                plc_count = 0
                for plc in plcs - set(self._connectors) - set(self._postpones):
                    # Skip if a connector with the same PLC ID still exists (if only
                    # some PLC parameters have been changed)
                    if next((p for p in self._connectors if p.id == plc.id), None):
                        continue

                    # Limit to start only 5 connectors in one cycle
                    plc_count += 1
                    if plc_count > 5:
                        self._postpones[plc] = t0
                        logger.info(f"PLC {plc.id} postponed to the next cycle")
                        continue

                    name = str(plc.id)
                    logger.info(f"Connector {name} created")
                    connector = UdpConnector(
                        self._host,
                        plc.port,
                        plc.teco_online_username,
                        plc.teco_online_password,
                        plc.teco_online_plc_name,
                        application=self._application,
                        name=name,
                    )
                    task = create_task(
                        connector.run(), name=f"{__name__}.Connector-{connector}"
                    )
                    task.add_done_callback(partial(self._delete_connector, plc))
                    self._connectors[plc] = connector

                # Close connectors of deleted and disabled PLCs
                for plc in set(self._connectors) - plcs:
                    connector = self._connectors[plc]
                    if connector.is_running:
                        connector.close()
                        logger.info(f"Connector {connector} closed (by manager)")

            await sleep(t0 + 1 - time())
