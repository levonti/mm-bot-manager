import json
import re
from typing import Any

from .commands import CommandContext, dispatch
from .logging import get_logger

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
    post_raw = data.get("post")
    if not post_raw:
        return
    try:
        post = json.loads(post_raw) if isinstance(post_raw, str) else post_raw
    except json.JSONDecodeError:
        log.warning("bad_post_json")
        return

    channel_type = data.get("channel_type")
    command_text = router.extract_command(post, channel_type)
    if command_text is None:
        return

    sender_id = post.get("user_id")
    sender_name = data.get("sender_name", "").lstrip("@")
    channel_id = post.get("channel_id")
    if not sender_id or not channel_id:
        return

    ctx = ctx_factory.build(
        caller_user_id=sender_id,
        caller_username=sender_name,
        channel_id=channel_id,
    )
    log.info("dispatching", user=sender_name, channel=channel_id, cmd=command_text[:120])
    try:
        reply = await dispatch(ctx, command_text)
    except Exception:
        log.exception("command_failed")
        reply = "Something went wrong running that command. Check the server logs."

    try:
        await ctx_factory.bot_mm.create_post(channel_id=channel_id, message=reply)
    except Exception:
        log.exception("reply_post_failed")


class CommandContextFactory:
    """Builds a CommandContext for each incoming message."""

    def __init__(
        self,
        bot_repo: Any,
        token_repo: Any,
        pending_repo: Any,
        admin_mm: Any,
        bot_mm: Any,
        manager_user_id: str,
        approver_ids: frozenset[str],
    ) -> None:
        self._bot_repo = bot_repo
        self._token_repo = token_repo
        self._pending_repo = pending_repo
        self._admin_mm = admin_mm
        self.bot_mm = bot_mm
        self._manager_user_id = manager_user_id
        self._approver_ids = approver_ids

    def build(
        self, *, caller_user_id: str, caller_username: str, channel_id: str
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
        )
