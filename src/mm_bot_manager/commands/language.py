from dataclasses import replace

from ..localization import user_language
from ..repository import PreferenceRepo
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if args.flags or len(args.positional) > 1:
        return ctx.tr("language.usage")
    preferences = PreferenceRepo(ctx.bot_repo.conn)
    if args.positional:
        choice = args.positional[0]
        if choice not in {"ru", "en", "de", "auto"}:
            return ctx.tr("language.usage")
        if ctx.channel_type != "D":
            return ctx.tr("language.dm")
        preferences.language(ctx.caller_user_id, choice)
        if choice == "auto" and ctx.language_resolver is not None:
            ctx.language_resolver.invalidate(ctx.caller_user_id)
        ctx = replace(ctx, language=await user_language(ctx, ctx.caller_user_id))
    preference, _ = preferences.get(ctx.caller_user_id)
    return ctx.tr(
        "language.changed" if args.positional else "language.current",
        preference=ctx.tr(f"language.{preference}"),
        effective=ctx.tr(f"language.{ctx.language}"),
    )
