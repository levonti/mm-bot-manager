from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.caller_user_id not in ctx.approver_ids:
        return "You are not authorised to deny bot creation requests."
    if not args.positional:
        return "Usage: `!deny <request_id> [reason]`"

    request_id = args.positional[0]
    reason = " ".join(args.positional[1:]) or None

    req = ctx.pending_repo.get(request_id)
    if req is None:
        return f"Request `{request_id}` not found."
    if req.status != "pending":
        return f"Request `{request_id}` is already **{req.status}**."

    ctx.pending_repo.resolve(
        request_id,
        status="denied",
        resolved_by=ctx.caller_user_id,
        deny_reason=reason,
    )

    reason_line = f"\nReason: {reason}" if reason else ""
    try:
        dm = await ctx.bot_mm.create_direct_channel(
            ctx.manager_user_id, req.requester_user_id
        )
        await ctx.bot_mm.create_post(
            channel_id=dm["id"],
            message=(
                f"Your request to create bot `@{req.username}` has been **denied** "
                f"by @{ctx.caller_username} (request `{request_id}`).{reason_line}"
            ),
        )
    except Exception:
        pass

    return f"Denied request `{request_id}` for `@{req.username}`."
