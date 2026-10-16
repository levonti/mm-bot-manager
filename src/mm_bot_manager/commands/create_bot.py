import secrets

from ..metadata import BotMetadata, MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..operations import OperationError, create_managed_bot
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return (
            'Usage: `!create <username> --repo <url> --criticality <1|2|3> [--display-name "..."]`'
        )
    username = args.positional[0]
    display_name = args.take_flag("display-name")
    description = args.take_flag("description")

    if not username.endswith("-bot"):
        return f"Bot username must end with `-bot` (got `{username}`)."
    if len(args.positional) != 1 or args.flags.keys() - {
        "repo",
        "criticality",
        "display-name",
        "description",
    }:
        return "Usage: `!create <username> --repo <url> --criticality <1|2|3>`"
    try:
        metadata = validate_metadata(args.take_flag("repo"), args.take_flag("criticality"))
    except MetadataError as exc:
        return str(exc)

    if not ctx.approver_ids:
        return await _create_now(ctx, username, display_name, description, metadata)

    return await _submit_for_approval(ctx, username, display_name, description, metadata)


async def _create_now(
    ctx: CommandContext,
    username: str,
    display_name: str | None,
    description: str | None,
    metadata: BotMetadata,
) -> str:
    try:
        bot_user_id = await create_managed_bot(
            ctx,
            username=username,
            display_name=display_name,
            description=description,
            git_repository=metadata.git_repository,
            criticality=metadata.criticality,
        )
    except OperationError as e:
        return f"Could not create bot: {e}"
    except MattermostError as e:
        if e.status == 403:
            return (
                "Could not create bot: permission denied.\n"
                "`MM_TOKEN` must be a Personal Access Token "
                "from a **system_admin user account** (not a bot token). "
                "Also ensure *System Console → Integrations → Bot Accounts → "
                "Enable Bot Account Creation* is **true**."
            )
        return f"Could not create bot: {e}"

    return (
        f"Created bot `@{username}` (id `{bot_user_id}`). "
        f"Use `!token create {username}` to mint a token for it."
    )


async def _submit_for_approval(
    ctx: CommandContext,
    username: str,
    display_name: str | None,
    description: str | None,
    metadata: BotMetadata,
) -> str:
    request_id = secrets.token_hex(4)
    ctx.pending_repo.create(
        request_id=request_id,
        username=username,
        display_name=display_name,
        description=description,
        requester_user_id=ctx.caller_user_id,
        requester_username=ctx.caller_username,
        git_repository=metadata.git_repository,
        criticality=metadata.criticality,
    )

    dn_line = f"\n- display name: {display_name}" if display_name else ""
    desc_line = f"\n- description: {description}" if description else ""
    notice = (
        f"**Bot creation request** (ID: `{request_id}`)\n"
        f"- requested by: @{ctx.caller_username}\n"
        f"- username: `@{username}`"
        f"{dn_line}{desc_line}\n"
        f"- repository: {metadata.git_repository}\n"
        f"- criticality: {metadata.criticality} (1 critical, 2 important, 3 auxiliary)\n\n"
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
