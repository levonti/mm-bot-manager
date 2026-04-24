import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .logging import get_logger

log = get_logger(__name__)


async def _authenticate(conn: ClientConnection, token: str) -> None:
    await conn.send(
        json.dumps(
            {
                "seq": 1,
                "action": "authentication_challenge",
                "data": {"token": token},
            }
        )
    )


async def stream_events(
    ws_url: str, token: str, *, verify_ssl: bool = True
) -> AsyncIterator[dict[str, Any]]:
    ssl_context: ssl.SSLContext | bool
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    else:
        ssl_context = True

    backoff = 1.0
    max_backoff = 30.0
    while True:
        try:
            log.info("ws_connecting", url=ws_url)
            async with websockets.connect(ws_url, max_size=2**22, ssl=ssl_context) as conn:
                await _authenticate(conn, token)
                backoff = 1.0
                log.info("ws_connected")
                async for raw in conn:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("ws_bad_json", raw=raw[:200])
                        continue
                    yield msg
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("ws_disconnected", error=str(e), reconnect_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
