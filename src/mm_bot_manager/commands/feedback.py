"""DM-only text commands. Parse only the protocol prefix, never the free-text body."""

import re

from ..feedback import FeedbackRepo
from ..feedback_views import event_text, summary
from ..i18n import UserFacingError
from .context import CommandContext

_REF = re.compile(r"FB-([1-9][0-9]{0,17})\Z", re.IGNORECASE)
_MUTATIONS = {"reply", "close", "reopen", "set-status", "take"}


def _page(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,5}", value):
        raise UserFacingError("feedback.usage")
    return int(value)


def _body(value: str) -> str:
    body = value.strip()
    if not 1 <= len(body) <= 3000 or "\x00" in body:
        raise UserFacingError("feedback.body_limit")
    return body


async def handle(ctx: CommandContext, text: str) -> str:
    if ctx.channel_type != "D":
        return ctx.tr("feedback.dm_only")
    if not ctx.feedback_handler_ids:
        return ctx.tr("feedback.disabled")
    if ctx.file_ids:
        return ctx.tr("feedback.no_files")
    try:
        return _handle(ctx, text)
    except UserFacingError as exc:
        return ctx.error(exc)


def _handle(ctx: CommandContext, text: str) -> str:
    repo = FeedbackRepo(ctx.bot_repo.conn)
    parts = text.split(maxsplit=1)
    action = parts[0] if parts else "help"
    rest = parts[1] if len(parts) > 1 else ""
    handler = ctx.caller_user_id in ctx.feedback_handler_ids
    if action == "help" and not rest:
        sections = [ctx.tr("feedback.usage")]
        if handler:
            sections.append(ctx.tr("feedback.handler_help"))
        return "\n\n".join(sections)
    if action == "new":
        ticket = repo.create(
            ctx.caller_user_id,
            _body(rest),
            ctx.post_id,
            ctx.feedback_handler_ids,
            actor_name=ctx.caller_username,
        )
        return ctx.tr("feedback.created", ref=ticket.ref) + "\n\n" + summary(ctx, ticket)
    if action in {"list", "queue"}:
        if action == "queue" and not handler:
            raise UserFacingError("feedback.forbidden")
        page = _page(rest) if rest else 1
        tickets = repo.listing(None if action == "queue" else ctx.caller_user_id, (page - 1) * 10)
        lines = [ctx.tr("feedback.page", page=page)]
        for ticket in tickets[:10]:
            lines.append(summary(ctx, ticket))
        if not tickets:
            lines.append(ctx.tr("feedback.empty"))
        if len(tickets) > 10:
            lines.append(ctx.tr("feedback.next", command=f"!feedback {action} {page + 1}"))
        return "\n\n".join(lines)
    if action not in {"status", "show", *_MUTATIONS}:
        raise UserFacingError("feedback.usage")
    args = rest.split(maxsplit=1)
    match = _REF.fullmatch(args[0]) if args else None
    if match is None:
        raise UserFacingError("feedback.usage")
    ticket = repo.visible(int(match[1]), ctx.caller_user_id, ctx.feedback_handler_ids)
    tail = args[1] if len(args) > 1 else ""
    if action in {"status", "show"}:
        if action == "status":
            if tail:
                raise UserFacingError("feedback.usage")
            event = repo.latest(ticket.number)
            return (
                summary(ctx, ticket)
                + "\n\n"
                + event_text(ctx, ticket, event)
                + "\n\n"
                + ctx.tr("feedback.actions", ref=ticket.ref)
            )
        page = _page(tail) if tail else 1
        events = repo.history(ticket.number, page - 1)
        lines = [summary(ctx, ticket), ctx.tr("feedback.page", page=page)]
        # One event per page keeps even a 3000-character escaped body within a post.
        if events:
            lines.append(event_text(ctx, ticket, events[0]))
        else:
            lines.append(ctx.tr("feedback.empty"))
        if len(events) > 1:
            lines.append(ctx.tr("feedback.next", command=f"!feedback show {ticket.ref} {page + 1}"))
        lines.append(ctx.tr("feedback.actions", ref=ticket.ref))
        return "\n\n".join(lines)
    status = None
    if action == "set-status":
        values = tail.split(maxsplit=1)
        if len(values) != 2 or values[0] not in {"waiting", "resolved"}:
            raise UserFacingError("feedback.usage")
        status, tail = values
    if action == "take":
        if tail:
            raise UserFacingError("feedback.usage")
        body = ""
    else:
        body = _body(tail)
    ticket = repo.change(
        ticket,
        action,
        ctx.caller_user_id,
        body,
        ctx.post_id,
        ctx.feedback_handler_ids,
        status=status,
        actor_name=ctx.caller_username,
    )
    return ctx.tr("feedback.saved", ref=ticket.ref) + "\n\n" + summary(ctx, ticket)
