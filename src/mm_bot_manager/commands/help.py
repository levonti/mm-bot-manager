from ..i18n import tr
from .context import CommandContext
from .parser import ParsedArgs

HELP_TEXT = tr("en", "help.text")


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    return ctx.tr("help.text")
