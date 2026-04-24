from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return 'Usage: `!token create <username> [--description "..."]`'
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    description = args.take_flag("description") or f"minted by @{ctx.caller_username}"
    try:
        resp = await ctx.mm.create_user_access_token(bot.bot_user_id, description)
    except MattermostError as e:
        return f"Could not create token: {e}"

    token_id = resp["id"]
    token_value = resp["token"]
    ctx.token_repo.record(
        token_id=token_id,
        bot_user_id=bot.bot_user_id,
        description=description,
    )
    return (
        f"Minted token for `@{username}`.\n"
        f"- token id: `{token_id}`\n"
        f"- description: {description}\n\n"
        f"**Copy this token now — it will not be shown again:**\n"
        f"```\n{token_value}\n```"
    )
