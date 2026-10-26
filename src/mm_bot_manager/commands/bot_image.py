import asyncio
import re

import httpx

from ..i18n import UserFacingError
from ..images import AVATAR_LIMIT, normalize_image
from ..mm_client import MattermostError
from ..updates import bot_mutation, require_idle
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs, *, kind: str) -> str:
    if len(args.positional) != 2 or args.flags or args.positional[0] not in {"set", "reset"}:
        return ctx.tr("images.usage", kind=kind)
    if ctx.channel_type != "D":
        return ctx.tr("images.dm")
    action, username = args.positional
    bot = ctx.bot_repo.get_by_username(username)
    if bot is None or bot.owner_user_id != ctx.caller_user_id:
        return ctx.tr("delete_bot.bot_not_found_or_not_owned_by", v0=username)
    if (action == "set" and len(ctx.file_ids) != 1) or (action == "reset" and ctx.file_ids):
        return ctx.tr("images.attachment")
    writing = False
    try:
        async with bot_mutation(ctx, bot.bot_user_id):
            require_idle(ctx, bot.bot_user_id)
            if ctx.operation_repo.active_for("bot_create", bot.username) or (
                ctx.operation_repo.active_for("token_create", bot.bot_user_id)
            ):
                raise UserFacingError("operations.an_unfinished_operation_for_this_bot_requires")
            if action == "set":
                file_id = ctx.file_ids[0]
                if not re.fullmatch(r"[a-z0-9]{26}", file_id) or not ctx.post_id:
                    raise UserFacingError("images.attachment")
                info = await ctx.bot_mm.get_file_info(file_id)
                limit = AVATAR_LIMIT
                if (
                    info.get("id") != file_id
                    or info.get("user_id") != ctx.caller_user_id
                    or info.get("post_id") != ctx.post_id
                ):
                    raise UserFacingError("images.attachment")
                size = info.get("size")
                if type(size) is not int or not 0 < size <= limit:
                    raise UserFacingError("images.invalid")
                data = await ctx.bot_mm.download_image(file_id, limit)
                normalized = await asyncio.to_thread(normalize_image, data)
                writing = True
                await ctx.mm.set_profile_image(bot.bot_user_id, normalized)
            else:
                writing = True
                await ctx.mm.reset_profile_image(bot.bot_user_id)
    except UserFacingError as exc:
        return ctx.error(exc)
    except MattermostError as exc:
        if writing and exc.status >= 500:
            return ctx.tr("images.uncertain")
        return ctx.tr("images.failed", error=ctx.error(exc))
    except httpx.TransportError:
        return ctx.tr("images.uncertain" if writing else "images.unavailable")
    return ctx.tr("images.done", kind=kind, action=action, username=username)


async def avatar(ctx: CommandContext, args: ParsedArgs) -> str:
    return await handle(ctx, args, kind="avatar")


async def icon(ctx: CommandContext, args: ParsedArgs) -> str:
    return await handle(ctx, args, kind="icon")
