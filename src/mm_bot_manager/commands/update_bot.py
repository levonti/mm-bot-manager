from typing import Any

from ..metadata import MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..permissions import is_approver
from ..updates import UpdateError, bot_mutation, require_idle, submit_update
from ..validation import BotValidationError, validate_bot_fields
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("update_bot.usage_update_username_username_display_name_description")
    if len(args.positional) != 1 or args.flags.keys() - {
        "username",
        "display-name",
        "description",
        "repo",
        "criticality",
    }:
        return ctx.tr("update_bot.unknown_update_arguments_use_repo_criticality_or")
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_username(username)
    registry_only = not (args.flags.keys() & {"username", "display-name", "description"})
    if bot is None or (
        bot.owner_user_id != ctx.caller_user_id and not (is_approver(ctx) and registry_only)
    ):
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)
    if not args.flags:
        return ctx.tr("update_bot.nothing_to_update_pass_at_least_one")
    key_change = bool(args.flags.keys() & {"username", "repo", "criticality"})
    if key_change and ctx.channel_type != "D":
        return ctx.tr("updates.dm")
    changes: dict[str, Any] = {}
    for flag, field in (
        ("username", "username"),
        ("display-name", "display_name"),
        ("description", "description"),
    ):
        if flag in args.flags:
            changes[field] = args.flags[flag]
    try:
        validate_bot_fields(changes)
        if args.flags.keys() & {"repo", "criticality"}:
            metadata = validate_metadata(
                args.flags.get("repo", bot.git_repository),
                args.flags.get("criticality", bot.criticality),
            )
            changes.update(git_repository=metadata.git_repository, criticality=metadata.criticality)
        async with bot_mutation(ctx, bot.bot_user_id):
            require_idle(ctx, bot.bot_user_id)
            if key_change:
                return await submit_update(ctx, bot, changes)
            await ctx.mm.patch_bot(bot.bot_user_id, **changes)
            ctx.bot_repo.patch(bot.bot_user_id, **changes)
    except (MetadataError, UpdateError, BotValidationError) as exc:
        return ctx.error(exc)
    except MattermostError as exc:
        return ctx.tr("update_bot.could_not_update_bot", v0=ctx.error(exc))
    return ctx.tr("update_bot.updated_bot", v0=username)
