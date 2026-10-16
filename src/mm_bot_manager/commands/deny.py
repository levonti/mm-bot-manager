from ..i18n import tr
from ..localization import user_language
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.caller_user_id not in ctx.approver_ids:
        return ctx.tr("deny.you_are_not_authorised_to_deny_bot")
    if not args.positional:
        return ctx.tr("deny.usage_deny_request_id_reason")

    request_id = args.positional[0]
    reason = " ".join(args.positional[1:]) or None

    req = ctx.pending_repo.get(request_id)
    if req is None:
        return ctx.tr("approve.request_not_found", v0=request_id)
    if req.status != "pending":
        return ctx.tr(
            "approve.request_is_already", v0=request_id, v1=ctx.tr(f"status.{req.status}")
        )

    if not ctx.pending_repo.resolve(
        request_id,
        status="denied",
        resolved_by=ctx.caller_user_id,
        deny_reason=reason,
    ):
        return ctx.tr("deny.request_is_already_being_processed_or_resolved", v0=request_id)

    try:
        language = await user_language(ctx, req.requester_user_id)
        reason_line = tr(language, "notice.reason", reason=reason) if reason else ""
        dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
        await ctx.bot_mm.create_post(
            channel_id=dm["id"],
            message=tr(
                language,
                "notice.denied",
                kind=tr(language, f"kind.{req.kind}"),
                username=req.username,
                approver=ctx.caller_username,
                request_id=request_id,
                reason=reason_line,
            ),
        )
    except Exception:
        pass

    return ctx.tr("deny.denied_request_for", v0=request_id, v1=req.username)
