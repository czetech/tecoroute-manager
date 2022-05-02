from types import SimpleNamespace
from typing import Literal

from aiohttp.web import Request
from aiohttp.web_exceptions import HTTPNotFound, HTTPUnauthorized


async def authentication(
    username: str, passowrd: str, request: Request
) -> SimpleNamespace:
    for auth in request.config_dict["api_auth"].splitlines():
        auth_split = auth.split(":", 1)
        auth_split += [""] * (len(auth_split) == 1)
        if [username, passowrd] == auth_split:
            return SimpleNamespace(username=username)
    raise HTTPUnauthorized


async def plc_delete(
    plc_id: int, token_info: SimpleNamespace, request: Request
) -> tuple[None, Literal[204]]:
    try:
        request.config_dict["manager"].close_connector(
            plc_id, f"by API user {repr(token_info.username)}"
        )
    except KeyError:
        raise HTTPNotFound(text="The PLC not found") from None
    return None, 204
