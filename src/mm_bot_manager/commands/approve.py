from ..i18n import tr
from ..imports import ImportError, approve_import
from ..localization import user_language
from ..metadata import MetadataError, validate_metadata
from ..mm_client import MattermostError
from ..operations import OperationError, create_managed_bot
from ..permissions import is_approver, is_self_approval
from ..updates import UpdateError, approve_update
from ..validation import BotValidationError
from .context import CommandContext
from .parser import ParsedArgs


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    if not is_approver(ctx):
        return ctx.tr("approve.you_are_not_authorised_to_approve_bot")
    if not args.positional:
        return ctx.tr("approve.usage_approve_request_id")

    request_id = args.positional[0]
    req = ctx.pending_repo.get(request_id)
    if req is None:
        return ctx.tr("approve.request_not_found", v0=request_id)
    if req.status != "pending":
        return ctx.tr(
            "approve.request_is_already", v0=request_id, v1=ctx.tr(f"status.{req.status}")
        )
    if is_self_approval(ctx, req):
        return ctx.tr("approve.self_forbidden")
    try:
        validate_metadata(req.git_repository, req.criticality)
    except MetadataError:
        return ctx.tr("approve.request_metadata_is_incomplete_backfill_it_before")

    if req.kind == "update":
        try:
            username = await approve_update(ctx, req)
        except (UpdateError, BotValidationError) as exc:
            return ctx.error(exc)
        try:
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
            await ctx.bot_mm.create_post(
                channel_id=dm["id"],
                message=tr(
                    await user_language(ctx, req.requester_user_id),
                    "notice.update_approved",
                    request_id=request_id,
                    username=username,
                ),
            )
        except Exception:
            pass
        return ctx.tr("updates.approved", username=username, request_id=request_id)
    if req.kind == "import":
        try:
            bot_user_id = await approve_import(ctx, req)
        except (ImportError, MattermostError) as exc:
            return ctx.tr("approve.could_not_import_bot", v0=ctx.error(exc))
        try:
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
            await ctx.bot_mm.create_post(
                channel_id=dm["id"],
                message=(
                    tr(
                        await user_language(ctx, req.requester_user_id),
                        "notice.import_approved",
                        username=req.username,
                        approver=ctx.caller_username,
                        request_id=request_id,
                        bot_id=bot_user_id,
                    )
                ),
            )
        except Exception:
            pass
        return ctx.tr("approve.approved_bot_imported_id", v0=req.username, v1=bot_user_id)
    if req.kind != "create":
        return ctx.tr("approve.unknown_request_kind_no_bot_will_be")

    try:
        bot_user_id = await create_managed_bot(
            ctx,
            username=req.username,
            display_name=req.display_name,
            description=req.description,
            request=req,
        )
    except (OperationError, BotValidationError) as e:
        return ctx.tr("approve.could_not_create_bot", v0=ctx.error(e))
    except MattermostError as e:
        return ctx.tr("approve.could_not_create_bot", v0=ctx.error(e))

    try:
        dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, req.requester_user_id)
        await ctx.bot_mm.create_post(
            channel_id=dm["id"],
            message=(
                tr(
                    await user_language(ctx, req.requester_user_id),
                    "notice.creation_approved",
                    username=req.username,
                    approver=ctx.caller_username,
                    request_id=request_id,
                    bot_id=bot_user_id,
                )
            ),
        )
    except Exception:
        pass

    return ctx.tr("approve.approved_bot_created_id", v0=req.username, v1=bot_user_id)
