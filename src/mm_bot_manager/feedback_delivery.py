"""Single-instance durable DM delivery, independent of WebSocket command workers."""

import asyncio
from dataclasses import replace

from .commands.context import CommandContext
from .feedback import FeedbackRepo
from .feedback_views import event_text
from .localization import user_language
from .logging import get_logger

log = get_logger(__name__)


async def deliver_once(ctx: CommandContext) -> None:
    repo = FeedbackRepo(ctx.bot_repo.conn)
    for row in repo.due():
        recipient = row["recipient_id"]
        ticket = repo.get(row["ticket_number"])
        assert ticket is not None
        # Configuration changes must revoke pending deliveries to former handlers.
        if recipient != ticket.author_id and recipient not in ctx.feedback_handler_ids:
            repo.delivered(row["outbox_id"], skipped=True)
            continue
        try:
            language = await user_language(ctx, recipient)
            localized = replace(ctx, language=language)
            event = repo.history(ticket.number, row["version"] - 1, limit=1)[0]
            message = (
                event_text(localized, ticket, event)
                + "\n\n"
                + localized.tr("feedback.actions", ref=ticket.ref)
            )
            channel = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, recipient)
            await ctx.bot_mm.create_post(channel_id=channel["id"], message=message)
        except Exception as exc:
            repo.retry(row["outbox_id"], row["attempts"])
            log.warning("feedback_delivery_failed", error_type=type(exc).__name__)
        else:
            repo.delivered(row["outbox_id"])


async def run_delivery(ctx: CommandContext) -> None:
    while True:
        try:
            await deliver_once(ctx)
        except Exception as exc:
            # DB failures must not permanently kill notification processing.
            log.error("feedback_outbox_failed", error_type=type(exc).__name__)
        await asyncio.sleep(1)
