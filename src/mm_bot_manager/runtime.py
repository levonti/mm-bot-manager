import asyncio
import fcntl
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .db import DbConn
from .dispatcher import CommandContextFactory, MessageRouter, handle_event
from .logging import get_logger

log = get_logger(__name__)
_LOCK_ID = 1296908877


@contextmanager
def instance_lock(conn: DbConn, db_path: str) -> Iterator[None]:
    """Recovery and WebSocket processing require a single active manager per database."""
    if conn.dialect == "postgres":
        row = conn.execute("SELECT pg_try_advisory_lock(?) AS locked", (_LOCK_ID,)).fetchone()
        if not row["locked"]:
            raise RuntimeError("Another mm-bot-manager instance holds the database lock")
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(?)", (_LOCK_ID,))
    else:
        with open(f"{Path(db_path).resolve()}.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Another mm-bot-manager instance holds the database lock"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


async def process_events(
    events: AsyncIterable[dict[str, Any]],
    router: MessageRouter,
    factory: CommandContextFactory,
    *,
    worker_count: int = 4,
    queue_size: int = 100,
    drain_timeout: float = 30,
) -> None:
    if worker_count < 1 or queue_size < 1:
        raise ValueError("Worker count and queue size must be positive")
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)

    async def worker() -> None:
        while True:
            event = await queue.get()
            try:
                await handle_event(event, router, factory)
            except Exception as exc:
                log.error("event_failed", error_type=type(exc).__name__)
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        async for event in events:
            await queue.put(event)
    finally:
        try:
            await asyncio.wait_for(queue.join(), timeout=drain_timeout)
        except TimeoutError:
            log.warning("shutdown_drain_timeout")
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
