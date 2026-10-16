from ..mm_client import MattermostError
from ..operations import OperationError, mint_and_deliver_token
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return 'Usage: `!token create <username> [--description "..."]`'
    if ctx.channel_type != "D":
        return "Tokens can only be created in a direct message to this manager."
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    description = args.take_flag("description") or f"minted by @{ctx.caller_username}"
    try:
        return await mint_and_deliver_token(
            ctx,
            bot_user_id=bot.bot_user_id,
            username=username,
            description=description,
        )
    except OperationError as e:
        return f"Could not create token: {e}"
    except MattermostError as e:
        return f"Could not create token (API status {e.status})."
