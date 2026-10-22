from ..mm_client import MattermostError
from ..updates import UpdateError, bot_mutation, require_idle
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("delete_bot.usage_delete_username")
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)

    try:
        async with bot_mutation(ctx, bot.bot_user_id):
            require_idle(ctx, bot.bot_user_id)
            await ctx.mm.disable_bot(bot.bot_user_id)
            ctx.bot_repo.mark_deleted(bot.bot_user_id)
    except UpdateError as exc:
        return ctx.error(exc)
    except MattermostError as e:
        return ctx.tr("delete_bot.could_not_disable_bot", v0=ctx.error(e))

    return ctx.tr("delete_bot.disabled_bot", v0=username)
