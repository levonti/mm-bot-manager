import asyncio
import contextlib
import signal

from .config import Settings, load_settings
from .db import DbConn, connect, connect_postgres, init_schema
from .dispatcher import CommandContextFactory, MessageRouter
from .logging import configure_logging, get_logger
from .mm_client import MattermostClient, MattermostError
from .operations import recover_operations
from .repository import BotRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock, process_events
from .ws_listener import stream_events


async def _resolve_approvers(mm: MattermostClient, usernames: list[str]) -> frozenset[str]:
    ids: list[str] = []
    for username in usernames:
        try:
            user = await mm.get_user_by_username(username)
            ids.append(user["id"])
        except MattermostError as e:
            raise SystemExit(
                f"Cannot resolve approver username '{username}' (API status {e.status}). "
                "Fix MM_APPROVERS or remove it to disable approval."
            ) from None
    return frozenset(ids)


async def serve(settings: Settings, conn: DbConn) -> None:
    log = get_logger("mm_bot_manager")
    init_schema(conn)
    kw = {"verify_ssl": settings.mm_verify_ssl}
    async with (
        MattermostClient(settings.mm_rest_base, settings.mm_bot_token, **kw) as bot_mm,
        MattermostClient(settings.mm_rest_base, settings.mm_token, **kw) as admin_mm,
    ):
        me = await bot_mm.get_me()
        manager_user_id = me["id"]
        manager_username = me["username"]
        approver_ids = await _resolve_approvers(admin_mm, settings.approver_usernames)
        log.info("logged_in", user_id=manager_user_id, approver_count=len(approver_ids))
        router = MessageRouter(manager_user_id, manager_username)
        factory = CommandContextFactory(
            bot_repo=BotRepo(conn),
            token_repo=TokenRepo(conn),
            pending_repo=PendingRequestRepo(conn),
            admin_mm=admin_mm,
            bot_mm=bot_mm,
            manager_user_id=manager_user_id,
            approver_ids=approver_ids,
        )
        recovery_ctx = factory.build(
            caller_user_id=manager_user_id, caller_username=manager_username, channel_id=""
        )
        unresolved = await recover_operations(recovery_ctx)
        interrupted = factory.post_repo.interrupt_unfinished()
        log.info(
            "startup_recovery", unresolved_operations=unresolved, interrupted_posts=interrupted
        )
        async with contextlib.aclosing(
            stream_events(
                settings.mm_ws_url,
                settings.mm_bot_token,
                verify_ssl=settings.mm_verify_ssl,
            )
        ) as events:
            await process_events(
                events,
                router,
                factory,
                worker_count=settings.max_workers,
                queue_size=settings.event_queue_size,
                drain_timeout=settings.shutdown_timeout,
            )


async def main_async() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    conn = connect_postgres(settings.db_url) if settings.db_url else connect(settings.db_path)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        with instance_lock(conn, settings.db_path):
            await serve(settings, conn)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        conn.close()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main_async())


if __name__ == "__main__":
    run()
