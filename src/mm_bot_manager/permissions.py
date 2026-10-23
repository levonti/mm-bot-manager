"""Identity checks shared by command execution and personalized help."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands.context import CommandContext
    from .repository import PendingRequestRecord


def is_approver(ctx: CommandContext) -> bool:
    return ctx.caller_user_id in ctx.approver_ids


def can_read_catalog(ctx: CommandContext) -> bool:
    return is_approver(ctx) or ctx.caller_user_id in ctx.catalog_viewer_ids


def is_self_approval(ctx: CommandContext, request: PendingRequestRecord) -> bool:
    if ctx.caller_user_id == request.requester_user_id:
        return True
    if request.kind == "import":
        submission = ctx.bot_repo.conn.execute(
            "SELECT submitted_by FROM import_submission WHERE request_id = ?",
            (request.request_id,),
        ).fetchone()
        return submission is not None and submission["submitted_by"] == ctx.caller_user_id
    return False
