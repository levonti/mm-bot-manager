from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return (
            'Usage: `!update <username> [--username "..."] '
            '[--display-name "..."] [--description "..."]`'
        )
    username = args.positional[0]
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
