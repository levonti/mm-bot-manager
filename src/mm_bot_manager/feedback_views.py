"""Bounded, localized feedback output. User text cannot create mentions or links."""

import re
from typing import TYPE_CHECKING

from .dates import format_datetime
from .feedback import Event, Ticket

if TYPE_CHECKING:
    from .commands.context import CommandContext


def safe(value: str) -> str:
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = re.sub(r"([\\`*_{}\[\]()!|~])", r"\\\1", value)
    return value.replace("@", "@\u200b")


def summary(ctx: CommandContext, ticket: Ticket) -> str:
    return ctx.tr(
        "feedback.summary",
        ref=ticket.ref,
        status=ctx.tr(f"feedback.state.{ticket.status}"),
        author=safe(ticket.author_name),
        assignee=safe(ticket.assignee_name or ticket.assignee_id)
        if ticket.assignee_id
        else ctx.tr("feedback.unassigned"),
        updated=format_datetime(ticket.updated_at, ctx.language, ctx.display_timezone),
    )


def event_text(ctx: CommandContext, ticket: Ticket, event: Event) -> str:
    body = "\n".join("> " + line for line in safe(event.body).splitlines())
    return ctx.tr(
        "feedback.event",
        ref=ticket.ref,
        sequence=event.version,
        kind=ctx.tr(f"feedback.kind.{event.kind}"),
        actor=safe(event.actor_name),
        date=format_datetime(event.created_at, ctx.language, ctx.display_timezone),
        status=ctx.tr(f"feedback.state.{event.status}"),
        body=body,
    )
