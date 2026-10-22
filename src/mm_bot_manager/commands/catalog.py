from ..i18n import plural
from ..metadata import cell, validate_criticality
from ..permissions import can_read_catalog
from ..repository import CatalogRepo, PreferenceRepo
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.channel_type != "D":
        return ctx.tr("catalog.dm")
    if not can_read_catalog(ctx):
        return ctx.tr("catalog.denied")
    if args.positional or args.flags.keys() - {"criticality", "owner", "search", "page", "format"}:
        return ctx.tr("catalog.usage")
    preferences = PreferenceRepo(ctx.bot_repo.conn)
    try:
        page_text = args.take_flag("page")
        page = int(page_text) if page_text is not None else 1
        if not 1 <= page <= 100000:
            raise ValueError
        crit_text = args.take_flag("criticality")
        criticality = validate_criticality(crit_text) if crit_text is not None else None
        owner, search = args.take_flag("owner"), args.take_flag("search")
        if any(v is not None and (not v.strip() or len(v) > 200) for v in (owner, search)):
            raise ValueError
        chosen_format = args.take_flag("format") or preferences.get(ctx.caller_user_id)[1]
        if chosen_format not in {"cards", "table"} or args.take_flag("format") == "":
            raise ValueError
    except ValueError:
        return ctx.tr("catalog.invalid")
    bots, total = CatalogRepo(ctx.bot_repo.conn).page(
        criticality=criticality,
        owner=owner.removeprefix("@") if owner else None,
        search=search,
        page=page,
    )
    if "format" in args.flags:
        preferences.format(ctx.caller_user_id, chosen_format)
    pages = max(1, (total + 9) // 10)
    lines = [
        ctx.tr(
            "catalog.summary",
            count=plural(ctx.language, total, "catalog.count"),
            page=page,
            pages=pages,
        ),
        ctx.tr("catalog.legend"),
        ctx.tr("catalog.format", format=ctx.tr(f"format.{chosen_format}")),
        "",  # Preserve the fix: Mattermost needs a block boundary before tables.
    ]
    if not bots:
        return "\n".join([*lines, ctx.tr("catalog.empty")])
    if chosen_format == "table":
        lines.extend([ctx.tr("catalog.header"), "|---|---|---|---|---|---|"])
    for bot in bots:
        critical = (
            f"{bot['criticality']} — {ctx.tr(f'criticality.{bot["criticality"]}')}"
            if bot["criticality"] is not None
            else ctx.tr("metadata.missing")
        )
        values = (
            bot["username"],
            bot["description"] or "—",
            bot["owner_username"],
            bot["git_repository"] or ctx.tr("metadata.missing"),
            critical,
            ctx.tr(f"source.{bot['source']}"),
        )
        if chosen_format == "table":
            lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        else:
            lines.append(f"**{cell(bot['username'])}**")
            for label, value in zip(
                ("purpose", "owner", "repository", "criticality", "source"), values[1:], strict=True
            ):
                lines.append(f"- **{ctx.tr(f'label.{label}')}**: {cell(value)}")
            lines.append("")
    return "\n".join(lines)
