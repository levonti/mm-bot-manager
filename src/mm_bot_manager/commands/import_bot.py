import re

from ..imports import ImportError, notify_import, prepare_import, submit_imports
from ..mm_client import MattermostError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if ctx.channel_type != "D":
        return "Import requests are only accepted in a direct message to this manager."
    if len(args.positional) != 1 or args.flags:
        return "Usage: `!import <existing-bot-username>` (in a direct message)"
    if not ctx.approver_ids:
        return "Import requires configured MM_APPROVERS and explicit approval."
    username = args.positional[0].removeprefix("@")
    # Usernames are path segments; do not accept slashes, query strings or traversal.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", username):
        return "Invalid bot username."
    try:
        user = await ctx.mm.get_user_by_username(username)
        plan = await prepare_import(ctx, user["id"], ctx.caller_user_id)
        if plan.username != username:
            raise ImportError("The remote username changed; submit a new request.")
        requests = submit_imports(ctx, [plan])
    except (ImportError, MattermostError) as exc:
        return f"Could not request import: {exc}"
    if not requests:
        return f"Bot `@{username}` is already managed by you."
    request = requests[0]
    await notify_import(ctx, request)
    return (
        f"Import of `@{username}` submitted for mandatory approval "
        f"(request ID: `{request.request_id}`). Existing tokens remain unchanged."
    )
