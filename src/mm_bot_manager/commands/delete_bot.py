from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return "Usage: `!delete <username>`"
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    try:
        await ctx.mm.disable_bot(bot.bot_user_id)
    except MattermostError as e:
        return f"Could not disable bot: {e}"

    ctx.bot_repo.mark_deleted(bot.bot_user_id)
    return f"Disabled bot `@{username}`."
