from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return "Usage: `!info <username>`"
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return f"Bot `@{username}` not found or not owned by you."

    active_tokens = ctx.token_repo.list_active_for_bot(bot.bot_user_id)
    audit = ctx.bot_repo.conn.execute(
        "SELECT imported_at, submitted_by, approved_by FROM bot_import WHERE bot_user_id = ?",
        (bot.bot_user_id,),
    ).fetchone()
    source = (
        f"- source: imported at {audit['imported_at']}\n"
        f"- import submitted by: `{audit['submitted_by']}`\n"
        f"- import approved by: `{audit['approved_by']}`\n"
        if audit
        else "- source: created\n"
    )
    return (
        f"**@{bot.username}** (`{bot.bot_user_id}`)\n"
        f"- display name: {bot.display_name or '—'}\n"
        f"- description: {bot.description or '—'}\n"
        f"- registered locally: {bot.created_at}\n"
        f"{source}"
        f"- active tokens tracked by this manager: {len(active_tokens)}"
    )
