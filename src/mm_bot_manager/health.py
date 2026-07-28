import asyncio
from collections.abc import Callable
from functools import partial
from typing import Protocol


class HealthCheck(Protocol):
    def ping(self) -> bool:
        raise NotImplementedError


_RESPONSES = {
    200: ("OK", b"ok\n"),
    404: ("Not Found", b"not found\n"),
    503: ("Service Unavailable", b"unavailable\n"),
}


async def _handle_request(
    db: HealthCheck,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        status = 404
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            parts = line.decode("ascii", errors="replace").split()
            if (
                len(parts) == 3
                and parts[0] == "GET"
                and parts[1]
                in {
                    "/health/live",
                    "/health/ready",
                }
            ):
                status = 200 if db.ping() else 503
        except TimeoutError, ValueError:
            status = 404

        reason, body = _RESPONSES[status]
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _schedule_request(
    db: HealthCheck,
    handler_tasks: set[asyncio.Task[None]],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    task = asyncio.create_task(_handle_request(db, reader, writer))
    handler_tasks.add(task)
    task.add_done_callback(handler_tasks.discard)


async def start_health_server(
    db: HealthCheck,
    host: str,
    port: int,
    handler_tasks: set[asyncio.Task[None]] | None = None,
) -> asyncio.Server:
    if handler_tasks is None:
        handler_tasks = set()
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None] = partial(
        _schedule_request, db, handler_tasks
    )
    return await asyncio.start_server(handler, host, port, limit=8192)
