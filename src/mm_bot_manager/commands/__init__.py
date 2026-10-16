from collections.abc import Awaitable, Callable

from . import (
    approve,
    catalog,
    create_bot,
    delete_bot,
    deny,
    import_bot,
    info,
    list_bots,
    token_create,
    token_list,
    token_revoke,
    update_bot,
)
from . import (
    help as help_cmd,
)
from .context import CommandContext
from .parser import ParsedArgs, parse_command

Handler = Callable[[CommandContext, ParsedArgs], Awaitable[str]]

REGISTRY: dict[tuple[str, ...], Handler] = {
    ("help",): help_cmd.handle,
    ("create",): create_bot.handle,
    ("import",): import_bot.handle,
    ("list",): list_bots.handle,
    ("catalog",): catalog.handle,
    ("info",): info.handle,
    ("update",): update_bot.handle,
    ("delete",): delete_bot.handle,
    ("approve",): approve.handle,
    ("deny",): deny.handle,
    ("token", "create"): token_create.handle,
    ("token", "list"): token_list.handle,
    ("token", "revoke"): token_revoke.handle,
}


async def dispatch(ctx: CommandContext, raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))
    if not text.startswith("!"):
        return "All commands must start with `!` (e.g. `!help`, `!create mybot-bot`)."
    raw_text = text[1:]
    if not raw_text.strip():
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))
    try:
        argv = parse_command(raw_text)
    except ValueError as e:
        return f"Could not parse command: {e}"
    if not argv:
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))

    for prefix_len in (2, 1):
        if len(argv) >= prefix_len:
            key = tuple(argv[:prefix_len])
            if key in REGISTRY:
                rest = argv[prefix_len:]
                args = ParsedArgs.from_argv(rest)
                return await REGISTRY[key](ctx, args)
    return f"Unknown command: `{argv[0]}`. Send `!help` for a list of commands."


__all__ = ["CommandContext", "ParsedArgs", "dispatch"]
