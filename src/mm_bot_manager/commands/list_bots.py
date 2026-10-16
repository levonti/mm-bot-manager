from ..metadata import cell
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    bots = ctx.bot_repo.list_by_owner(ctx.caller_user_id)
    if not bots:
        return (
            "You don't own any bots yet. "
            "Use `!create <username> --repo <url> --criticality <1|2|3>` to make one."
        )

    lines = ["| username | description | repository | criticality |", "|---|---|---|---|"]
    for b in bots:
        desc = b.description or "—"
        lines.append(
            f"| `@{b.username}` | {cell(desc)} | {cell(b.git_repository or 'METADATA REQUIRED')} "
            f"| {b.criticality or 'REQUIRED'} |"
        )
    return "\n".join(lines)
