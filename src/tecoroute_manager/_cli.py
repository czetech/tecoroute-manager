from argparse import (
    SUPPRESS,
    ArgumentDefaultsHelpFormatter,
    ArgumentParser,
    FileType,
    Namespace,
)
from asyncio import (
    CancelledError,
    create_task,
    get_event_loop,
    run,
    set_event_loop_policy,
)
from logging import DEBUG, INFO, basicConfig
from pathlib import Path
from signal import SIGINT, SIGTERM

from aiohttp.web import Application, AppRunner, Request, Response, TCPSite, get
from aiohttp_cors import ResourceOptions
from aiohttp_cors import setup as cors_setup
from connexion import AioHttpApi
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from ._manager import Manager
from ._misc import APPLICATION, CONNECTOR_HOST, PORT, dist

try:
    from uvloop import EventLoopPolicy
except ImportError:
    pass
else:
    set_event_loop_policy(EventLoopPolicy())


async def _metrics(request: Request) -> Response:
    gauge_http_time = request.app["gauge_http_time"]
    for plc, connector in request.app["manager"].connectors.items():
        http_time = connector.http_time
        if http_time is not None:
            gauge_http_time.labels(plc.id, plc.name, plc.serial_number).set(http_time)
    return Response(text=generate_latest(request.app["registry"]).decode())


async def _main(args: Namespace) -> None:
    try:
        database = args.database
    except AttributeError:
        database = args.database_file.read()
    try:
        api_auth = args.api_auth
    except AttributeError:
        api_auth = args.api_auth_file.read()
    basicConfig(level=DEBUG if getattr(args, "verbose", None) else INFO)
    manager = Manager(
        database,
        args.communication_type,
        args.trc_server_id,
        host=args.connector_host,
        application=args.application,
        debug_id=getattr(args, "debug_id", None),
    )
    registry = CollectorRegistry()
    gauge_http_time = Gauge(
        "tecoroute_http_time",
        "Response time from TecoRoute server",
        ("id", "name", "serial_number"),
        registry=registry,
    )
    web = Application()
    web.update(
        dict(
            api_auth=api_auth,
            manager=manager,
            registry=registry,
            gauge_http_time=gauge_http_time,
        )
    )
    web.add_routes((get("/metrics", _metrics),))

    # Add Connexion API to web application
    api = AioHttpApi(
        Path(Path(__file__).parent, "openapi", "api_v1.yaml"),
        pass_context_arg_name="request",
    )
    resource_options = ResourceOptions(
        allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
    )
    cors = cors_setup(api.subapp, defaults={"*": resource_options})
    for route in api.subapp.router.routes():
        cors.add(route)
    web.add_subapp(api.base_path, api.subapp)

    web_runner = AppRunner(web)
    await web_runner.setup()
    await TCPSite(web_runner, getattr(args, "host", None), args.port).start()
    manager_runner = create_task(manager.run())
    for signum in (SIGINT, SIGTERM):
        get_event_loop().add_signal_handler(signum, manager_runner.cancel)
    try:
        await manager_runner
    except CancelledError:
        pass
    finally:
        await web_runner.cleanup()


def cli() -> None:
    """Run the command-line interface."""
    parser = ArgumentParser(
        prog=dist.entry_points[0].name,
        description=dist.metadata["Summary"],
        formatter_class=ArgumentDefaultsHelpFormatter,
        argument_default=SUPPRESS,
    )
    parser.add_argument(
        "-H",
        "--host",
        help="host to listen on, all interfaces if not set",
    )
    parser.add_argument(
        "-p",
        "--port",
        default=PORT,
        type=int,
        help="port to listen on",
    )
    parser_database = parser.add_mutually_exclusive_group(required=True)
    parser_database.add_argument(
        "-d",
        "--database",
        help="database URL",
    )
    parser_database.add_argument(
        "-D",
        "--database-file",
        type=FileType(),
        help="database URL from file",
    )
    parser_api_auth = parser.add_mutually_exclusive_group(required=True)
    parser_api_auth.add_argument(
        "-s",
        "--api-auth",
        help="API authentication in username:password format",
    )
    parser_api_auth.add_argument(
        "-S",
        "--api-auth-file",
        type=FileType(),
        help=(
            "API authentication in username:password format from file; multiple "
            "credentials can be separated by a new line"
        ),
    )
    parser.add_argument(
        "-c",
        "--communication-type",
        required=True,
        help="value for filtering PLCs by communication_type column",
    )
    parser.add_argument(
        "-t",
        "--trc-server-id",
        type=int,
        required=True,
        help="value for filtering PLCs by trc_server_id column",
    )
    parser.add_argument(
        "-o",
        "--connector-host",
        default=CONNECTOR_HOST,
        help="host to listen connectors on",
    )
    parser.add_argument(
        "-a",
        "--application",
        default=APPLICATION,
        help=(
            "TecoRoute application name; only if you have assigned your own "
            "application name from Teco a.s."
        ),
    )
    parser.add_argument(
        "-i",
        "--debug-id",
        help=(
            "run only one connector by PLC ID; communication-type and trc-server-id "
            "are not taken into account"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose mode",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"TecoRoute Proxy {dist.version}",
    )
    run(_main(parser.parse_args()))
