from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.caller_user_id not in ctx.approver_ids:
        return "You are not authorised to approve bot creation requests."
    if not args.positional:
        return "Usage: `!approve <request_id>`"

    request_id = args.positional[0]
    req = ctx.pending_repo.get(request_id)
    if req is None:
        return f"Request `{request_id}` not found."
    if req.status != "pending":
        return f"Request `{request_id}` is already **{req.status}**."

    try:
        bot = await ctx.mm.create_bot(
            username=req.username,
            display_name=req.display_name,
            description=req.description,
        )
    except MattermostError as e:
        return f"Could not create bot: {e}"

    bot_user_id = bot["user_id"]
    ctx.bot_repo.insert(
        bot_user_id=bot_user_id,
        username=req.username,
        display_name=req.display_name,
        description=req.description,
        owner_user_id=req.requester_user_id,
        owner_username=req.requester_username,
    )
    ctx.pending_repo.resolve(request_id, status="approved", resolved_by=ctx.caller_user_id)

    try:
        dm = await ctx.bot_mm.create_direct_channel(
            ctx.manager_user_id, req.requester_user_id
        )
        await ctx.bot_mm.create_post(
            channel_id=dm["id"],
            message=(
                f"Your request to create bot `@{req.username}` has been **approved** "
                f"by @{ctx.caller_username} (request `{request_id}`).\n"
                f"Bot id: `{bot_user_id}`. "
                f"Use `!token create {req.username}` to mint a token."
            ),
        )
    except Exception:
        pass

    return f"Approved. Bot `@{req.username}` created (id `{bot_user_id}`)."
