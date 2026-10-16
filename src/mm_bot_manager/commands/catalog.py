from ..metadata import cell, validate_criticality
from ..repository import CatalogRepo
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.channel_type != "D":
        return "The bot catalog is only available in a direct message to this manager."
    if ctx.caller_user_id not in ctx.approver_ids | ctx.catalog_viewer_ids:
        return "You are not authorised to view the global catalog. Use !list for your own bots."
    if args.positional or args.flags.keys() - {"criticality", "owner", "search", "page"}:
        return (
            "Usage: `!catalog [--criticality <1|2|3>] [--owner <username>] "
            "[--search <text>] [--page <n>]`"
        )
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
    except ValueError:
        return "Invalid catalog filter: criticality 1-3, positive page, nonempty owner/search."
    bots, total = CatalogRepo(ctx.bot_repo.conn).page(
        criticality=criticality,
        owner=owner.removeprefix("@") if owner else None,
        search=search,
        page=page,
    )
    pages = max(1, (total + 9) // 10)
    lines = [
        f"**Bot catalog** — {total} matching bots; page {page}/{pages}.",
        "Criticality: 1 critical · 2 important · 3 auxiliary. Status reflects the local registry.",
        "| bot | purpose | owner | repository | criticality | source |",
        "|---|---|---|---|---|---|",
    ]
    for bot in bots:
        lines.append(
            "| "
            + " | ".join(
                cell(v)
                for v in (
                    bot["username"],
                    bot["description"] or "—",
                    bot["owner_username"],
                    bot["git_repository"] or "METADATA REQUIRED",
                    bot["criticality"] or "REQUIRED",
                    bot["source"],
                )
            )
            + " |"
        )
    if not bots:
        lines.append("No bots on this page.")
    return "\n".join(lines)
