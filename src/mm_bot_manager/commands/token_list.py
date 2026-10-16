from ..metadata import cell
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("tokens.usage")
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)
    tokens = ctx.token_repo.list_active_for_bot(bot.bot_user_id)
    if not tokens:
        return ctx.tr("tokens.empty", username=username)
    lines = [
        ctx.tr("tokens.title", username=username),
        "",
        ctx.tr("tokens.header"),
        "|---|---|---|",
    ]
    for token in tokens:
        lines.append(
            f"| `{token.token_id}` | {cell(token.description or '—')} | {token.created_at} |"
        )
    return "\n".join(lines)
