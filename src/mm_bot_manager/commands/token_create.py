from ..mm_client import MattermostError
from ..operations import OperationError, mint_and_deliver_token
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("token_create.usage_token_create_username_description")
    if ctx.channel_type != "D":
        return ctx.tr("token_create.tokens_can_only_be_created_in_a")
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)

    description = args.take_flag("description") or ctx.tr(
        "tokens.default_description", username=ctx.caller_username
    )
    try:
        return await mint_and_deliver_token(
            ctx,
            bot_user_id=bot.bot_user_id,
            username=username,
            description=description,
        )
    except OperationError as e:
        return ctx.tr("token_create.could_not_create_token", v0=ctx.error(e))
    except MattermostError as e:
        return ctx.tr("token_create.could_not_create_token_api_status", v0=e.status)
