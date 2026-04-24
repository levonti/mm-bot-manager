import asyncio
import contextlib

from .config import load_settings
from .db import DbConn, connect, connect_postgres, init_schema
from .dispatcher import CommandContextFactory, MessageRouter, handle_event
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


async def main_async() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("mm_bot_manager")

    conn: DbConn = (
        connect_postgres(settings.db_url)
        if settings.db_url
        else connect(settings.db_path)
    )
    init_schema(conn)
    bot_repo = BotRepo(conn)
    token_repo = TokenRepo(conn)
    pending_repo = PendingRequestRepo(conn)

    background: set[asyncio.Task[None]] = set()

    kw = {"verify_ssl": settings.mm_verify_ssl}
    async with (
        MattermostClient(settings.mm_rest_base, settings.mm_bot_token, **kw) as bot_mm,
        MattermostClient(settings.mm_rest_base, settings.mm_token, **kw) as admin_mm,
    ):
        me = await bot_mm.get_me()
        manager_user_id = me["id"]
        manager_username = me["username"]
        log.info("logged_in", user_id=manager_user_id, username=manager_username)

        approver_ids = await _resolve_approvers(
            admin_mm, settings.approver_usernames, log
        )
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

        async for event in stream_events(
            settings.mm_ws_url, settings.mm_bot_token, verify_ssl=settings.mm_verify_ssl
        ):
            task = asyncio.create_task(handle_event(event, router, factory))
            background.add(task)
            task.add_done_callback(background.discard)


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async())


if __name__ == "__main__":
    run()
