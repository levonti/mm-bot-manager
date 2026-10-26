from ..version import version_text
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if args.positional or args.flags:
        return "Usage: !version"
    return version_text()
