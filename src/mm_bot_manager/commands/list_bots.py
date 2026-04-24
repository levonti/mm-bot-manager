from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    bots = ctx.bot_repo.list_by_owner(ctx.caller_user_id)
    if not bots:
        return "You don't own any bots yet. Use `create <username>` to make one."

    lines = ["| username | display name | description | created |", "|---|---|---|---|"]
    for b in bots:
        dn = b.display_name or "—"
        desc = b.description or "—"
        lines.append(f"| `@{b.username}` | {dn} | {desc} | {b.created_at} |")
    return "\n".join(lines)
