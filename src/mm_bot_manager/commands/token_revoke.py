from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return "Usage: `!token revoke <token_id>`"
    token_id = args.positional[0]

    record = ctx.token_repo.get(token_id)
    if record is None:
        return f"Token `{token_id}` is not tracked by this manager."
    bot = ctx.bot_repo.get_by_owner_and_username(
        ctx.caller_user_id,
        username=_lookup_username(ctx, record.bot_user_id),
    )
    if bot is None or bot.bot_user_id != record.bot_user_id:
        return f"Token `{token_id}` does not belong to a bot you own."
    if record.revoked_at is not None:
        return f"Token `{token_id}` was already revoked at {record.revoked_at}."

    try:
        await ctx.mm.revoke_user_access_token(token_id)
    except MattermostError as e:
        return f"Could not revoke token: {e}"

    ctx.token_repo.mark_revoked(token_id)
    return f"Revoked token `{token_id}`."


def _lookup_username(ctx: CommandContext, bot_user_id: str) -> str:
    for b in ctx.bot_repo.list_by_owner(ctx.caller_user_id):
        if b.bot_user_id == bot_user_id:
            return b.username
    return ""
