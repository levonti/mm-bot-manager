from ..db import transaction
from ..metadata import MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..repository import MetadataRepo
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return (
            'Usage: `!update <username> [--username "..."] '
            '[--display-name "..."] [--description "..."]`'
        )
    username = args.positional[0]
    if len(args.positional) != 1 or args.flags.keys() - {
        "username",
        "display-name",
        "description",
        "repo",
        "criticality",
    }:
        return (
            "Unknown update arguments. Use --repo, --criticality or the documented remote fields."
        )
    if "repo" in args.flags or "criticality" in args.flags:
        if ctx.channel_type != "D":
            return "Registry metadata can only be updated in a direct message."
        if args.flags.keys() & {"username", "display-name", "description"}:
            return "Update registry metadata separately from remote bot attributes."
        target = ctx.bot_repo.get_by_username(username)
        if target is None or (
            target.owner_user_id != ctx.caller_user_id
            and ctx.caller_user_id not in ctx.approver_ids
        ):
            return f"Bot `@{username}` not found or not owned by you."
        try:
            metadata = validate_metadata(
                args.take_flag("repo") if "repo" in args.flags else target.git_repository,
                args.take_flag("criticality")
                if "criticality" in args.flags
                else target.criticality,
            )
        except MetadataError as exc:
            return str(exc)
        with transaction(ctx.bot_repo.conn):
            MetadataRepo(ctx.bot_repo.conn).set(
                "bot", target.bot_user_id, metadata, actor=ctx.caller_user_id, source="chat"
            )
        return f"Updated registry metadata for `@{username}` (criticality {metadata.criticality})."
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    new_username = args.take_flag("username")
    new_display_name = args.take_flag("display-name")
    new_description = args.take_flag("description")
    if new_username is None and new_display_name is None and new_description is None:
        return "Nothing to update. Pass at least one of --username, --display-name, --description."

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
        return f"Could not update bot: {e}"

    ctx.bot_repo.patch(
        bot.bot_user_id,
        username=new_username,
        display_name=new_display_name,
        description=new_description,
    )
    effective = new_username or bot.username
    return f"Updated bot `@{effective}`."
