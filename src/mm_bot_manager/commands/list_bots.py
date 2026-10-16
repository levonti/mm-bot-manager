from ..metadata import cell
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    bots = ctx.bot_repo.list_by_owner(ctx.caller_user_id)
    if not bots:
        return ctx.tr("list.empty")
    lines = [ctx.tr("list.header"), "|---|---|---|---|"]
    for bot in bots:
        critical = (
            f"{bot.criticality} ({ctx.tr(f'criticality.{bot.criticality}')})"
            if bot.criticality is not None
            else ctx.tr("metadata.missing")
        )
        lines.append(
            f"| `@{bot.username}` | {cell(bot.description or '—')} "
            f"| {cell(bot.git_repository or ctx.tr('metadata.missing'))} | {critical} |"
        )
    return "\n".join(lines)
