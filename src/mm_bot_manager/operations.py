"""Journal irreversible API calls before sending them; compensate incomplete results."""

from typing import TYPE_CHECKING, NoReturn

from .db import transaction
from .i18n import Message, UserFacingError
from .logging import get_logger
from .metadata import validate_metadata
from .mm_client import MattermostError
from .permissions import is_self_approval

if TYPE_CHECKING:
    from .commands.context import CommandContext
    from .repository import PendingRequestRecord

log = get_logger(__name__)


class OperationError(UserFacingError, RuntimeError):
    """Safe, credential-free message that can be returned to the caller."""


def _resolver(ctx: CommandContext, request_id: str) -> str:
    request = ctx.pending_repo.get(request_id)
    return request.resolved_by if request and request.resolved_by else ctx.caller_user_id


def _uncertain(ctx: CommandContext, operation_id: str, request_id: str | None) -> None:
    try:
        with transaction(ctx.operation_repo.conn):
            ctx.operation_repo.update(operation_id, "needs_review")
            if request_id:
                ctx.pending_repo.resolve(
                    request_id,
                    status="needs_review",
                    resolved_by=_resolver(ctx, request_id),
                    expected_status="provisioning",
                )
    except Exception:
        # The original durable intent remains even if the database is unavailable.
        log.error("operation_journal_unavailable", operation_id=operation_id)
    log.error("operation_requires_review", operation_id=operation_id)


async def compensate(
    ctx: CommandContext,
    operation_id: str,
    kind: str,
    remote_id: str,
    request_id: str | None = None,
) -> bool:
    try:
        try:
            if kind == "bot_create":
                await ctx.mm.disable_bot(remote_id)
            else:
                await ctx.mm.revoke_user_access_token(remote_id)
        except MattermostError as exc:
            if exc.status != 404:  # An absent resource is already safe.
                raise
        with transaction(ctx.operation_repo.conn):
            if kind == "token_create":
                ctx.token_repo.mark_revoked(remote_id)
            else:
                ctx.bot_repo.mark_deleted(remote_id)
            ctx.operation_repo.update(operation_id, "compensated", remote_id)
            if request_id:
                ctx.pending_repo.resolve(
                    request_id,
                    status="failed",
                    resolved_by=_resolver(ctx, request_id),
                    expected_status="provisioning",
                )
                ctx.pending_repo.resolve(
                    request_id,
                    status="failed",
                    resolved_by=_resolver(ctx, request_id),
                    expected_status="needs_review",
                )
        return True
    except Exception:
        # Do not log exception bodies: a remote error may echo credentials.
        log.error("compensation_failed", operation_id=operation_id, kind=kind)
        return False


def _failed_creation(
    ctx: CommandContext,
    operation_id: str,
    request_id: str | None,
    exc: BaseException,
) -> NoReturn:
    if isinstance(exc, MattermostError) and 400 <= exc.status < 500 and exc.status != 408:
        with transaction(ctx.operation_repo.conn):
            ctx.operation_repo.update(operation_id, "compensated")
            if request_id:
                ctx.pending_repo.release(request_id)
        raise exc
    _uncertain(ctx, operation_id, request_id)
    if not isinstance(exc, Exception):
        raise exc
    raise OperationError(
        "operations.the_api_outcome_is_uncertain_operation_it", v0=operation_id
    ) from None


async def create_managed_bot(
    ctx: CommandContext,
    *,
    username: str,
    display_name: str | None,
    description: str | None,
    git_repository: str | None = None,
    criticality: int | None = None,
    request: PendingRequestRecord | None = None,
) -> str:
    if request is not None and is_self_approval(ctx, request):
        raise OperationError("approve.self_forbidden")
    metadata = validate_metadata(
        request.git_repository if request else git_repository,
        request.criticality if request else criticality,
    )
    request_id = request.request_id if request else None
    with transaction(ctx.operation_repo.conn):
        if ctx.operation_repo.active_for("bot_create", username):
            raise OperationError("operations.an_unfinished_operation_for_this_bot_requires")
        if request_id and not ctx.pending_repo.claim(request_id, ctx.caller_user_id):
            raise OperationError(
                "deny.request_is_already_being_processed_or_resolved", v0=request_id
            )
        operation_id = ctx.operation_repo.start("bot_create", username, request_id)
    try:
        bot = await ctx.mm.create_bot(
            username=username,
            display_name=display_name,
            description=description,
        )
        bot_user_id: str = bot["user_id"]
    except BaseException as exc:
        _failed_creation(ctx, operation_id, request_id, exc)

    try:
        ctx.operation_repo.update(operation_id, "remote_created", bot_user_id)
        with transaction(ctx.operation_repo.conn):
            ctx.bot_repo.insert(
                bot_user_id=bot_user_id,
                username=username,
                display_name=display_name,
                description=description,
                owner_user_id=request.requester_user_id if request else ctx.caller_user_id,
                owner_username=request.requester_username if request else ctx.caller_username,
                git_repository=metadata.git_repository,
                criticality=metadata.criticality,
            )
            if request_id and not ctx.pending_repo.resolve(
                request_id,
                status="approved",
                resolved_by=ctx.caller_user_id,
                expected_status="provisioning",
            ):
                raise OperationError("operations.the_approval_state_changed_during_provisioning")
            ctx.operation_repo.update(operation_id, "completed")
    except BaseException as exc:
        disabled = await compensate(ctx, operation_id, "bot_create", bot_user_id, request_id)
        if not isinstance(exc, Exception):
            raise
        outcome = (
            Message("operations.the_incomplete_bot_was_disabled")
            if disabled
            else Message("operations.disabling_the_incomplete_bot_requires_operator_attention")
        )
        raise OperationError(
            "operations.bot_creation_was_not_completed_operation", v0=operation_id, v1=outcome
        ) from None
    return bot_user_id


async def mint_and_deliver_token(
    ctx: CommandContext,
    *,
    bot_user_id: str,
    username: str,
    description: str,
) -> str:
    if ctx.channel_type != "D":
        raise OperationError("token_create.tokens_can_only_be_created_in_a")
    with transaction(ctx.operation_repo.conn):
        if ctx.operation_repo.active_for("token_create", bot_user_id):
            raise OperationError(
                "operations.an_unfinished_token_operation_requires_operator_review"
            )
        operation_id = ctx.operation_repo.start("token_create", bot_user_id)
    marker = f"[mm-bot-manager:{operation_id}]"
    try:
        resp = await ctx.mm.create_user_access_token(bot_user_id, f"{description} {marker}")
        token_id: str = resp["id"]
        token_value: str = resp["token"]
    except BaseException as exc:
        _failed_creation(ctx, operation_id, None, exc)

    try:
        ctx.operation_repo.update(operation_id, "remote_created", token_id)
        with transaction(ctx.operation_repo.conn):
            ctx.token_repo.record(
                token_id=token_id,
                bot_user_id=bot_user_id,
                description=description,
            )
            ctx.operation_repo.update(operation_id, "recorded")
        await ctx.bot_mm.create_post(
            channel_id=ctx.channel_id,
            message=(
                ctx.tr(
                    "operations.minted_token_for_token_id_store_this",
                    v0=username,
                    v1=token_id,
                    v2=token_value,
                )
            ),
        )
        ctx.operation_repo.update(operation_id, "completed")
    except BaseException as exc:
        revoked = await compensate(ctx, operation_id, "token_create", token_id)
        if not isinstance(exc, Exception):
            raise
        outcome = (
            Message("operations.the_token_was_revoked")
            if revoked
            else Message("operations.revocation_requires_operator_attention")
        )
        raise OperationError(
            "operations.token_issuance_did_not_complete_operation", v0=operation_id, v1=outcome
        ) from None
    return ctx.tr("operations.minted_token_for_and_sent_it_in", v0=username, v1=token_id)


async def recover_operations(ctx: CommandContext) -> int:
    """Run once at startup under the exclusive instance lock, before accepting events."""
    unresolved = 0
    for op in ctx.operation_repo.unfinished():
        if op.remote_id:
            if not await compensate(ctx, op.operation_id, op.kind, op.remote_id, op.request_id):
                unresolved += 1
            continue
        if op.kind == "token_create":
            # The marker recovers the crash window between the POST and saving its ID.
            try:
                marker = f"[mm-bot-manager:{op.operation_id}]"
                tokens = await ctx.mm.list_user_access_tokens(op.resource)
                matches = [t for t in tokens if str(t.get("description", "")).endswith(marker)]
                if matches:
                    for token in matches:
                        try:
                            await ctx.mm.revoke_user_access_token(token["id"])
                        except MattermostError as exc:
                            if exc.status != 404:
                                raise
                        ctx.token_repo.mark_revoked(token["id"])
                    ctx.operation_repo.update(op.operation_id, "compensated")
                    continue
            except Exception:
                log.error("token_recovery_failed", operation_id=op.operation_id)
        # Never adopt or disable a bot merely because its username matches an intent.
        _uncertain(ctx, op.operation_id, op.request_id)
        unresolved += 1
    from .updates import recover_updates

    return unresolved + await recover_updates(ctx)
