import re

from ..imports import ImportError, notify_import, prepare_import, submit_imports
from ..metadata import MetadataError, validate_metadata
from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.channel_type != "D":
        return ctx.tr("import_bot.import_requests_are_only_accepted_in_a")
    if len(args.positional) != 1 or args.flags.keys() - {"repo", "criticality"}:
        return ctx.tr("import_bot.usage_import_username_repo_url_criticality_in")
    if not ctx.approver_ids:
        return ctx.tr("import_bot.import_requires_configured_mm_approvers_and_explicit")
    username = args.positional[0].removeprefix("@")
    # Usernames are path segments; do not accept slashes, query strings or traversal.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", username):
        return ctx.tr("import_bot.invalid_bot_username")
    try:
        metadata = validate_metadata(args.take_flag("repo"), args.take_flag("criticality"))
        user = await ctx.mm.get_user_by_username(username)
        plan = await prepare_import(
            ctx, user["id"], ctx.caller_user_id, metadata.git_repository, metadata.criticality
        )
        if plan.username != username:
            raise ImportError("import_bot.the_remote_username_changed_submit_a_new")
        requests = submit_imports(ctx, [plan])
    except (ImportError, MetadataError, MattermostError) as exc:
        return ctx.tr("import_bot.could_not_request_import", v0=ctx.error(exc))
    if not requests:
        return ctx.tr("import_bot.bot_is_already_managed_by_you", v0=username)
    request = requests[0]
    await notify_import(ctx, request)
    return ctx.tr(
        "import_bot.import_of_submitted_for_mandatory_approval_request",
        v0=username,
        v1=request.request_id,
    )
