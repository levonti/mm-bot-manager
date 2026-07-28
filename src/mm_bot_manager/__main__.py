import asyncio
import contextlib
import signal

from .config import Settings, load_settings
from .db import DbConn, connect, connect_postgres, init_schema
from .dispatcher import CommandContextFactory, MessageRouter, handle_event
from .health import start_health_server
from .logging import configure_logging, get_logger
from .mm_client import MattermostClient, MattermostError
from .repository import BotRepo, PendingRequestRepo, TokenRepo
from .ws_listener import stream_events


async def _resolve_approvers(
    mm: MattermostClient, usernames: list[str], log: object
) -> frozenset[str]:
    ids: list[str] = []
    for username in usernames:
        try:
            user = await mm.get_user_by_username(username)
            ids.append(user["id"])
        except MattermostError as e:
            raise SystemExit(
                f"Cannot resolve approver username '{username}': {e}. "
                "Fix MM_APPROVERS or remove it to disable approval."
            ) from e
    return frozenset(ids)


def _connect_database(settings: Settings) -> DbConn:
    if settings.db_url:
        return connect_postgres(settings.db_url)
    if settings.db_host:
        return connect_postgres(
            None,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
    return connect(settings.db_path)


async def main_async() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("mm_bot_manager")

    conn = _connect_database(settings)
    background: set[asyncio.Task[None]] = set()
    health_handlers: set[asyncio.Task[None]] = set()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    sigterm_handler_installed = False
    if main_task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
            sigterm_handler_installed = True
        except NotImplementedError:
            pass

    try:
        init_schema(conn)
        bot_repo = BotRepo(conn)
        token_repo = TokenRepo(conn)
        pending_repo = PendingRequestRepo(conn)

        kw = {"verify_ssl": settings.mm_verify_ssl}
        async with (
            MattermostClient(settings.mm_rest_base, settings.mm_bot_token, **kw) as bot_mm,
            MattermostClient(settings.mm_rest_base, settings.mm_token, **kw) as admin_mm,
        ):
            me = await bot_mm.get_me()
            manager_user_id = me["id"]
            manager_username = me["username"]
            log.info("logged_in", user_id=manager_user_id, username=manager_username)

            approver_ids = await _resolve_approvers(admin_mm, settings.approver_usernames, log)
            if approver_ids:
                log.info("approval_required", approver_count=len(approver_ids))
            else:
                log.info("approval_disabled")

            router = MessageRouter(manager_user_id, manager_username)
            factory = CommandContextFactory(
                bot_repo=bot_repo,
                token_repo=token_repo,
                pending_repo=pending_repo,
                admin_mm=admin_mm,
                bot_mm=bot_mm,
                manager_user_id=manager_user_id,
                approver_ids=approver_ids,
            )

            health_server = await start_health_server(
                conn, settings.health_host, settings.health_port, health_handlers
            )
            log.info(
                "health_server_started",
                host=settings.health_host,
                port=settings.health_port,
            )
            async with health_server:
                async for event in stream_events(
                    settings.mm_ws_url,
                    settings.mm_bot_token,
                    verify_ssl=settings.mm_verify_ssl,
                ):
                    task = asyncio.create_task(handle_event(event, router, factory))
                    background.add(task)
                    task.add_done_callback(background.discard)
    finally:
        for task in health_handlers:
            task.cancel()
        await asyncio.gather(*health_handlers, return_exceptions=True)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        conn.close()
        if sigterm_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main_async())


if __name__ == "__main__":
    run()
