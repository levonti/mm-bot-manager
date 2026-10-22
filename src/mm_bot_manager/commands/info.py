from ..dates import format_datetime
from ..metadata import cell
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not args.positional:
        return ctx.tr("info.usage")
    username = args.positional[0]
    bot = ctx.bot_repo.get_by_owner_and_username(ctx.caller_user_id, username)
    if bot is None:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)
    active_tokens = ctx.token_repo.list_active_for_bot(bot.bot_user_id)
    audit = ctx.bot_repo.conn.execute(
        "SELECT imported_at, submitted_by, approved_by FROM bot_import WHERE bot_user_id = ?",
        (bot.bot_user_id,),
    ).fetchone()
    critical = (
        f"{bot.criticality} ({ctx.tr(f'criticality.{bot.criticality}')})"
        if bot.criticality is not None
        else ctx.tr("metadata.missing")
    )
    values = [
        ("display_name", bot.display_name or "—"),
        ("purpose", bot.description or "—"),
        ("repository", bot.git_repository or ctx.tr("metadata.missing")),
        ("criticality", critical),
        ("registered", format_datetime(bot.created_at, ctx.language, ctx.display_timezone)),
        ("source", ctx.tr("source.imported" if audit else "source.created")),
    ]
    if audit:
        values.extend(
            [
                (
                    "imported_at",
                    format_datetime(audit["imported_at"], ctx.language, ctx.display_timezone),
                ),
                ("submitted_by", audit["submitted_by"]),
                ("approved_by", audit["approved_by"]),
            ]
        )
    values.append(("tokens", str(len(active_tokens))))
    return "\n".join(
        [
            f"**@{bot.username}** (`{bot.bot_user_id}`)",
            *(
                f"- {ctx.tr(f'label.{label}').lower()}: {cell(value, 2048)}"
                for label, value in values
            ),
        ]
    )
