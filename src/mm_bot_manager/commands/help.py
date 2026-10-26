from ..i18n import tr
from ..permissions import can_read_catalog, is_approver
from .context import CommandContext
from .parser import ParsedArgs

HELP_TEXT = tr("en", "help.text")


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    sections = [ctx.tr("help.text")]
    if ctx.channel_type == "D":
        sections.append(ctx.tr("images.help"))
        if can_read_catalog(ctx):
            sections.append(ctx.tr("help.catalog"))
        if is_approver(ctx):
            sections.append(ctx.tr("help.approvers"))
    return "\n\n".join(sections)
