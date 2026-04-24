import secrets

from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return 'Usage: `!create <username> [--display-name "..."] [--description "..."]`'
    username = args.positional[0]
    display_name = args.take_flag("display-name")
    description = args.take_flag("description")

    if not username.endswith("-bot"):
        return f"Bot username must end with `-bot` (got `{username}`)."

    if not ctx.approver_ids:
        return await _create_now(ctx, username, display_name, description)

    return await _submit_for_approval(ctx, username, display_name, description)


async def _create_now(
    ctx: CommandContext,
    username: str,
    display_name: str | None,
    description: str | None,
) -> str:
    try:
        bot = await ctx.mm.create_bot(
            username=username,
            display_name=display_name,
            description=description,
        )
    except MattermostError as e:
        if e.status == 403:
            return (
                "Could not create bot: permission denied.\n"
                "The manager bot's `MM_TOKEN` must be a Personal Access Token "
                "from a **system_admin user account** (not a bot token). "
                "Also ensure *System Console → Integrations → Bot Accounts → "
                "Enable Bot Account Creation* is **true**."
            )
        return f"Could not create bot: {e}"

    bot_user_id = bot["user_id"]
    ctx.bot_repo.insert(
        bot_user_id=bot_user_id,
        username=username,
        display_name=display_name,
        description=description,
        owner_user_id=ctx.caller_user_id,
        owner_username=ctx.caller_username,
    )
    return (
        f"Created bot `@{username}` (id `{bot_user_id}`). "
        f"Use `!token create {username}` to mint a token for it."
    )


async def _submit_for_approval(
    ctx: CommandContext,
    username: str,
    display_name: str | None,
    description: str | None,
) -> str:
    request_id = secrets.token_hex(4)
    ctx.pending_repo.create(
        request_id=request_id,
        username=username,
        display_name=display_name,
        description=description,
        requester_user_id=ctx.caller_user_id,
        requester_username=ctx.caller_username,
    )

    dn_line = f"\n- display name: {display_name}" if display_name else ""
    desc_line = f"\n- description: {description}" if description else ""
    notice = (
        f"**Bot creation request** (ID: `{request_id}`)\n"
        f"- requested by: @{ctx.caller_username}\n"
        f"- username: `@{username}`"
        f"{dn_line}{desc_line}\n\n"
        f"Reply `!approve {request_id}` or `!deny {request_id} [reason]`."
    )

    for approver_id in ctx.approver_ids:
        try:
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, approver_id)
            await ctx.bot_mm.create_post(channel_id=dm["id"], message=notice)
        except Exception:
            pass  # best-effort; don't fail the whole request if one DM fails

    return (
        f"Your request to create `@{username}` has been submitted for approval "
        f"(request ID: `{request_id}`). You will be notified once it is reviewed."
    )
