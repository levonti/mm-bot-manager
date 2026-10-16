from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("token_revoke.usage_token_revoke_token_id")
    token_id = args.positional[0]

    record = ctx.token_repo.get(token_id)
    if record is None:
        return ctx.tr("token_revoke.token_is_not_tracked_by_this_manager", v0=token_id)
    bot = ctx.bot_repo.get_by_owner_and_username(
        ctx.caller_user_id,
        username=_lookup_username(ctx, record.bot_user_id),
    )
    if bot is None or bot.bot_user_id != record.bot_user_id:
        return ctx.tr("token_revoke.token_does_not_belong_to_a_bot", v0=token_id)
    if record.revoked_at is not None:
        return ctx.tr(
            "token_revoke.token_was_already_revoked_at", v0=token_id, v1=record.revoked_at
        )

    try:
        await ctx.mm.revoke_user_access_token(token_id)
    except MattermostError as e:
        return ctx.tr("token_revoke.could_not_revoke_token", v0=ctx.error(e))

    ctx.token_repo.mark_revoked(token_id)
    return ctx.tr("token_revoke.revoked_token", v0=token_id)


def _lookup_username(ctx: CommandContext, bot_user_id: str) -> str:
    for b in ctx.bot_repo.list_by_owner(ctx.caller_user_id):
        if b.bot_user_id == bot_user_id:
            return b.username
    return ""
