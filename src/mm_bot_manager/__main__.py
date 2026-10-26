import argparse
import asyncio
import contextlib
import signal

from .config import Settings, load_settings
from .db import DbConn, connect, connect_postgres, init_schema
from .dispatcher import CommandContextFactory, MessageRouter
from .feedback_delivery import run_delivery
from .health import start_health_server
from .i18n import tr, validate_catalogs
from .logging import configure_logging, get_logger
from .mm_client import MattermostClient, MattermostError
from .operations import recover_operations
from .repository import BotRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock, process_events
from .version import build_info, version_text
from .ws_listener import stream_events


async def _resolve_approvers(
    mm: MattermostClient,
    usernames: list[str],
    setting: str = "MM_APPROVERS",
    *,
    language: str = "en",
) -> frozenset[str]:
    ids: list[str] = []
    for username in usernames:
        try:
            user = await mm.get_user_by_username(username)
            ids.append(user["id"])
        except MattermostError as e:
            raise SystemExit(
                tr(language, "startup.resolve", username=username, status=e.status, setting=setting)
            ) from None
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


async def serve(settings: Settings, conn: DbConn) -> None:
    validate_catalogs()
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
        approver_ids = await _resolve_approvers(
            admin_mm, settings.approver_usernames, language=settings.mm_default_language
        )
        catalog_ids = await _resolve_approvers(
            admin_mm,
            settings.catalog_viewer_usernames,
            "MM_CATALOG_VIEWERS",
            language=settings.mm_default_language,
        )
        feedback_ids = await _resolve_approvers(
            admin_mm,
            settings.feedback_handler_usernames,
            "MM_FEEDBACK_HANDLERS",
            language=settings.mm_default_language,
        )
        if manager_user_id in feedback_ids:
            raise SystemExit("MM_FEEDBACK_HANDLERS must not contain the manager bot")
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
            catalog_viewer_ids=catalog_ids,
            feedback_handler_ids=feedback_ids,
            default_language=settings.mm_default_language,
            display_timezone=settings.mm_display_timezone,
        )
        recovery_ctx = factory.build(
            caller_user_id=manager_user_id, caller_username=manager_username, channel_id=""
        )
        unresolved = await recover_operations(recovery_ctx)
        interrupted = factory.post_repo.interrupt_unfinished()
        log.info(
            "startup_recovery", unresolved_operations=unresolved, interrupted_posts=interrupted
        )
        health_handlers: set[asyncio.Task[None]] = set()
        delivery = asyncio.create_task(run_delivery(recovery_ctx)) if feedback_ids else None
        try:
            server = await start_health_server(
                conn,
                settings.health_host,
                settings.health_port,
                health_handlers,
            )
            async with (
                server,
                contextlib.aclosing(
                    stream_events(
                        settings.mm_ws_url,
                        settings.mm_bot_token,
                        verify_ssl=settings.mm_verify_ssl,
                    )
                ) as events,
            ):
                await process_events(
                    events,
                    router,
                    factory,
                    worker_count=settings.max_workers,
                    queue_size=settings.event_queue_size,
                    drain_timeout=settings.shutdown_timeout,
                )
        finally:
            if delivery is not None:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
            handlers = tuple(health_handlers)
            for handler in handlers:
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)


async def main_async() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    release, revision = build_info()
    get_logger("mm_bot_manager").info("starting", version=release, revision=revision)
    conn = _connect_database(settings)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    try:
        if task is not None:
            try:
                loop.add_signal_handler(signal.SIGTERM, task.cancel)
                installed = True
            except NotImplementedError:
                pass
        with instance_lock(conn, settings.db_path):
            await serve(settings, conn)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)
        conn.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="Mattermost bot manager")
    parser.add_argument("--version", action="version", version=version_text())
    parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main_async())


if __name__ == "__main__":
    run()
