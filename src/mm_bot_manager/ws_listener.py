import asyncio
import json
import random
import ssl
from collections.abc import AsyncGenerator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, process_exception
from websockets.exceptions import ConnectionClosed

from .logging import get_logger

log = get_logger(__name__)


class AuthenticationError(RuntimeError):
    pass


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    try:
        msg = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        log.warning("ws_bad_json")
        return None
    return msg if isinstance(msg, dict) else None


async def _authenticate(
    conn: ClientConnection,
    token: str,
    *,
    timeout: float = 10,
) -> list[dict[str, Any]]:
    await conn.send(
        json.dumps(
            {
                "seq": 1,
                "action": "authentication_challenge",
                "data": {"token": token},
            }
        )
    )
    buffered: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while True:
            msg = _decode(await conn.recv())
            if msg is None:
                continue
            if msg.get("seq_reply") == 1:
                if msg.get("status") != "OK":
                    raise AuthenticationError("Mattermost WebSocket authentication failed")
                return buffered
            if len(buffered) >= 100:
                raise AuthenticationError("Missing WebSocket authentication acknowledgement")
            buffered.append(msg)


async def stream_events(
    ws_url: str,
    token: str,
    *,
    verify_ssl: bool = True,
) -> AsyncGenerator[dict[str, Any]]:
    ssl_context: ssl.SSLContext | bool | None = None
    if ws_url.startswith("wss://"):
        if verify_ssl:
            ssl_context = True
        else:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    backoff = 1.0
    while True:
        try:
            log.info("ws_connecting")
            async with websockets.connect(ws_url, max_size=2**22, ssl=ssl_context) as conn:
                buffered = await _authenticate(conn, token)
                backoff = 1.0
                log.info("ws_authenticated")
                for buffered_msg in buffered:
                    yield buffered_msg
                async for raw in conn:
                    msg = _decode(raw)
                    if msg is not None:
                        yield msg
            # Normal closes (1000/1001) need a delay too.
        except Exception as exc:
            if not isinstance(exc, ConnectionClosed) and process_exception(exc) is not None:
                raise
            log.warning("ws_disconnected", error_type=type(exc).__name__)
        delay = backoff + random.uniform(0, backoff / 4)
        log.info("ws_reconnecting", reconnect_in=delay)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, 30.0)
