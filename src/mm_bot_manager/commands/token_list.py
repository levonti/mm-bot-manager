from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return "Usage: `!token list <username>`"
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    tokens = ctx.token_repo.list_active_for_bot(bot.bot_user_id)
    if not tokens:
        return f"Bot `@{username}` has no active tokens."

    lines = [
        f"Active tokens for `@{username}`:",
        "",
        "| token id | description | created |",
        "|---|---|---|",
    ]
    for t in tokens:
        lines.append(f"| `{t.token_id}` | {t.description or '—'} | {t.created_at} |")
    return "\n".join(lines)
