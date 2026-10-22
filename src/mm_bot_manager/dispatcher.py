import asyncio
import json
import re
from dataclasses import replace
from typing import Any

from .commands import CommandContext, dispatch
from .localization import LanguageResolver, user_language
from .logging import get_logger
from .mm_client import MattermostClient
from .repository import BotRepo, OperationRepo, PendingRequestRepo, PostRepo, TokenRepo

log = get_logger(__name__)


class MessageRouter:
    def __init__(self, manager_user_id: str, manager_username: str) -> None:
        self._manager_user_id = manager_user_id
        self._mention = re.compile(rf"(^|\s)@{re.escape(manager_username)}\b")

    def extract_command(self, post: dict[str, Any], channel_type: str | None) -> str | None:
        if post.get("user_id") == self._manager_user_id:
            return None

        message: str = post.get("message") or ""
        if channel_type == "D":
            return message.strip()

        if self._mention.search(message):
            return self._mention.sub("", message, count=1).strip()
        return None


async def handle_event(
    event: dict[str, Any],
    router: MessageRouter,
    ctx_factory: CommandContextFactory,
) -> None:
    if event.get("event") != "posted":
        return

    data = event.get("data") or {}
    if not isinstance(data, dict):
        return
    post_raw = data.get("post")
    if not post_raw:
        return
    try:
        post = json.loads(post_raw) if isinstance(post_raw, str) else post_raw
    except json.JSONDecodeError:
        log.warning("bad_post_json")
        return

    if not isinstance(post, dict):
        return
    channel_type = data.get("channel_type")
    command_text = router.extract_command(post, channel_type)
    if command_text is None:
        return

    sender_id = post.get("user_id")
    sender_name = data.get("sender_name", "").lstrip("@")
    channel_id = post.get("channel_id")
    post_id = post.get("id")
    if (
        not isinstance(sender_id, str)
        or not sender_id
        or not isinstance(channel_id, str)
        or not channel_id
        or not isinstance(post_id, str)
        or not post_id
    ):
        log.warning("post_missing_identity")
        return
    if not ctx_factory.post_repo.claim(post_id):
        return

    ctx = ctx_factory.build(
        caller_user_id=sender_id,
        caller_username=sender_name,
        channel_id=channel_id,
        channel_type=channel_type,
    )
    log.info("dispatching", user_id=sender_id, channel_id=channel_id, post_id=post_id)
    try:
        try:
            reply = await dispatch(ctx, command_text)
        except Exception as exc:
            log.error("command_failed", post_id=post_id, error_type=type(exc).__name__)
            try:
                language = await user_language(ctx, ctx.caller_user_id)
            except Exception:
                language = ctx.default_language
            ctx = replace(ctx, language=language)
            reply = ctx.tr("error.unexpected")
        await ctx_factory.bot_mm.create_post(channel_id=channel_id, message=reply)
    except asyncio.CancelledError:
        ctx_factory.post_repo.finish(post_id, interrupted=True)
        raise
    except Exception as exc:
        log.error("reply_post_failed", post_id=post_id, error_type=type(exc).__name__)
        ctx_factory.post_repo.finish(post_id, interrupted=True)
    else:
        ctx_factory.post_repo.finish(post_id)


class CommandContextFactory:
    """Builds a CommandContext for each incoming message."""

    def __init__(
        self,
        bot_repo: BotRepo,
        token_repo: TokenRepo,
        pending_repo: PendingRequestRepo,
        admin_mm: MattermostClient,
        bot_mm: MattermostClient,
        manager_user_id: str,
        approver_ids: frozenset[str],
        catalog_viewer_ids: frozenset[str] = frozenset(),
        default_language: str = "en",
        display_timezone: str = "UTC",
    ) -> None:
        self._bot_repo = bot_repo
        self._token_repo = token_repo
        self._pending_repo = pending_repo
        self._admin_mm = admin_mm
        self.bot_mm = bot_mm
        self._manager_user_id = manager_user_id
        self._approver_ids = approver_ids
        self._catalog_viewer_ids = catalog_viewer_ids
        self.language_resolver = LanguageResolver(bot_repo.conn, admin_mm, default_language)
        self.default_language = default_language
        self.display_timezone = display_timezone
        self.operation_repo = OperationRepo(bot_repo.conn)
        self.post_repo = PostRepo(bot_repo.conn)

    def build(
        self,
        *,
        caller_user_id: str,
        caller_username: str,
        channel_id: str,
        channel_type: str | None = None,
    ) -> CommandContext:
        return CommandContext(
            caller_user_id=caller_user_id,
            caller_username=caller_username,
            channel_id=channel_id,
            bot_repo=self._bot_repo,
            token_repo=self._token_repo,
            pending_repo=self._pending_repo,
            mm=self._admin_mm,
            bot_mm=self.bot_mm,
            manager_user_id=self._manager_user_id,
            approver_ids=self._approver_ids,
            operation_repo=self.operation_repo,
            channel_type=channel_type,
            catalog_viewer_ids=self._catalog_viewer_ids,
            language=self.default_language,
            default_language=self.default_language,
            language_resolver=self.language_resolver,
            display_timezone=self.display_timezone,
        )
