import secrets

from ..i18n import tr
from ..localization import user_language
from ..metadata import BotMetadata, MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..operations import OperationError, create_managed_bot
from ..validation import BotValidationError, validate_bot_fields
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("create_bot.usage_create_username_repo_url_criticality_display")
    username = args.positional[0]
    display_name = args.take_flag("display-name")
    description = args.take_flag("description")

    if len(args.positional) != 1 or args.flags.keys() - {
        "repo",
        "criticality",
        "display-name",
        "description",
    }:
        return ctx.tr("create_bot.usage_create_username_repo_url_criticality")
    try:
        validate_bot_fields(
            {
                key: value
                for key, value in {
                    "username": username,
                    "display_name": display_name,
                    "description": description,
                }.items()
                if value is not None
            },
            require_bot_suffix=True,
        )
        metadata = validate_metadata(args.take_flag("repo"), args.take_flag("criticality"))
    except (MetadataError, BotValidationError) as exc:
        return ctx.error(exc)

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
    except (OperationError, BotValidationError) as e:
        return ctx.tr("approve.could_not_create_bot", v0=ctx.error(e))
    except MattermostError as e:
        if e.status == 403:
            reply = ctx.tr("create_bot.could_not_create_bot_permission_denied_mm")
            return f"{reply} {ctx.error(e)}" if e.diagnostic_id else reply
        return ctx.tr("approve.could_not_create_bot", v0=ctx.error(e))

    return ctx.tr(
        "create_bot.created_bot_id_use_token_create_to", v0=username, v1=bot_user_id, v2=username
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

    for approver_id in ctx.approver_ids:
        try:
            language = await user_language(ctx, approver_id)
            notice = tr(
                language,
                "notice.creation",
                request_id=request_id,
                requester=ctx.caller_username,
                username=username,
                display_name=display_name or "—",
                description=description or "—",
                repository=metadata.git_repository,
                criticality=(
                    f"{metadata.criticality} "
                    f"({tr(language, f'criticality.{metadata.criticality}')})"
                ),
            )
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, approver_id)
            await ctx.bot_mm.create_post(channel_id=dm["id"], message=notice)
        except Exception:
            pass  # best-effort; don't fail the whole request if one DM fails

    return ctx.tr(
        "create_bot.your_request_to_create_has_been_submitted", v0=username, v1=request_id
    )
