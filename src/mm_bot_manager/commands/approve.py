from ..imports import ImportError, approve_import
from ..mm_client import MattermostError
from ..operations import OperationError, create_managed_bot
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.caller_user_id not in ctx.approver_ids:
        return "You are not authorised to approve bot requests."
    if not args.positional:
        return "Usage: `!approve <request_id>`"

    request_id = args.positional[0]
    req = ctx.pending_repo.get(request_id)
    if req is None:
        return f"Request `{request_id}` not found."
    if req.status != "pending":
        return f"Request `{request_id}` is already **{req.status}**."

    if req.kind == "import":
        try:
            bot_user_id = await approve_import(ctx, req)
        except (ImportError, MattermostError) as exc:
            return f"Could not import bot: {exc}"
        try:
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
            await ctx.bot_mm.create_post(
                channel_id=dm["id"],
                message=(
                    f"Import of `@{req.username}` approved by @{ctx.caller_username} "
                    f"(request `{request_id}`). Bot id: `{bot_user_id}`. "
                    "Existing tokens remain unchanged."
                ),
            )
        except Exception:
            pass
        return f"Approved. Bot `@{req.username}` imported (id `{bot_user_id}`)."
    if req.kind != "create":
        return "Unknown request kind; no bot will be created."

    try:
        bot_user_id = await create_managed_bot(
            ctx,
            username=req.username,
            display_name=req.display_name,
            description=req.description,
            request=req,
        )
    except OperationError as e:
        return f"Could not create bot: {e}"
    except MattermostError as e:
        return f"Could not create bot: {e}"

    try:
        dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
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
