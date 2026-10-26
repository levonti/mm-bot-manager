from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..localization import user_language
from . import (
    approve,
    bot_image,
    catalog,
    create_bot,
    delete_bot,
    deny,
    feedback,
    import_bot,
    info,
    language,
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
    ("avatar",): bot_image.avatar,
    ("icon",): bot_image.icon,
    ("help",): help_cmd.handle,
    ("create",): create_bot.handle,
    ("import",): import_bot.handle,
    ("list",): list_bots.handle,
    ("catalog",): catalog.handle,
    ("language",): language.handle,
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
    ctx = replace(ctx, language=await user_language(ctx, ctx.caller_user_id))
    text = raw_text.strip()
    if not text:
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))
    if not text.startswith("!"):
        return ctx.tr("__init__.all_commands_must_start_with_e_g")
    # Whitespace includes newlines and tabs; split once and leave the free-text tail intact.
    words = text.split(maxsplit=1)
    if words[0] == "!feedback":
        return await feedback.handle(ctx, words[1] if len(words) > 1 else "")
    raw_text = text[1:]
    if not raw_text.strip():
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))
    try:
        argv = parse_command(raw_text)
    except ValueError:
        return ctx.tr("__init__.could_not_parse_command")
    if not argv:
        return await help_cmd.handle(ctx, ParsedArgs(positional=[], flags={}))

    for prefix_len in (2, 1):
        if len(argv) >= prefix_len:
            key = tuple(argv[:prefix_len])
            if key in REGISTRY:
                rest = argv[prefix_len:]
                args = ParsedArgs.from_argv(rest)
                return await REGISTRY[key](ctx, args)
    return ctx.tr("__init__.unknown_command_send_help_for_a_list", v0=argv[0])


__all__ = ["CommandContext", "ParsedArgs", "dispatch"]
