from ..db import transaction
from ..metadata import MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..repository import MetadataRepo
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("update_bot.usage_update_username_username_display_name_description")
    username = args.positional[0]
    if len(args.positional) != 1 or args.flags.keys() - {
        "username",
        "display-name",
        "description",
        "repo",
        "criticality",
    }:
        return ctx.tr("update_bot.unknown_update_arguments_use_repo_criticality_or")
    if "repo" in args.flags or "criticality" in args.flags:
        if ctx.channel_type != "D":
            return ctx.tr("update_bot.registry_metadata_can_only_be_updated_in")
        if args.flags.keys() & {"username", "display-name", "description"}:
            return ctx.tr("update_bot.update_registry_metadata_separately_from_remote_bot")
        target = ctx.bot_repo.get_by_username(username)
        if target is None or (
            target.owner_user_id != ctx.caller_user_id
            and ctx.caller_user_id not in ctx.approver_ids
        ):
            return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)
        try:
            metadata = validate_metadata(
                args.take_flag("repo") if "repo" in args.flags else target.git_repository,
                args.take_flag("criticality")
                if "criticality" in args.flags
                else target.criticality,
            )
        except MetadataError as exc:
            return ctx.error(exc)
        with transaction(ctx.bot_repo.conn):
            MetadataRepo(ctx.bot_repo.conn).set(
                "bot", target.bot_user_id, metadata, actor=ctx.caller_user_id, source="chat"
            )
        return ctx.tr(
            "update_bot.updated_registry_metadata_for_criticality",
            v0=username,
            v1=metadata.criticality,
        )
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)

    new_username = args.take_flag("username")
    new_display_name = args.take_flag("display-name")
    new_description = args.take_flag("description")
    if new_username is None and new_display_name is None and new_description is None:
        return ctx.tr("update_bot.nothing_to_update_pass_at_least_one")

    try:
        if new_display_name is not None or new_description is not None:
            await ctx.mm.patch_bot(
                bot.bot_user_id,
                display_name=new_display_name,
                description=new_description,
            )
        if new_username is not None:
            await ctx.mm.patch_user(bot.bot_user_id, username=new_username)
    except MattermostError as e:
        return ctx.tr("update_bot.could_not_update_bot", v0=ctx.error(e))

    ctx.bot_repo.patch(
        bot.bot_user_id,
        username=new_username,
        display_name=new_display_name,
        description=new_description,
    )
    effective = new_username or bot.username
    return ctx.tr("update_bot.updated_bot", v0=effective)
