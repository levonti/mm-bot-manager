"""Import existing bots using remote reads and atomic local transactions only.

Do not use creation compensation here: an imported bot predates this manager.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from .db import transaction
from .i18n import UserFacingError, tr
from .localization import user_language
from .metadata import validate_metadata
from .permissions import is_self_approval
from .repository import BotRecord, PendingRequestRecord, _now_iso

if TYPE_CHECKING:
    from .commands.context import CommandContext


class ImportError(UserFacingError, ValueError):
    """A request is unsafe, stale, or no longer eligible for import."""


@dataclass(frozen=True, slots=True)
class ImportPlan:
    bot_user_id: str
    username: str
    display_name: str | None
    description: str | None
    remote_owner_id: str
    owner_user_id: str
    owner_username: str
    git_repository: str
    criticality: int


def _id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]{26}", value):
        raise ImportError("imports.expected_an_immutable_mattermost_user_id_lowercase")
    return value


def check_local(ctx: CommandContext, plan: ImportPlan) -> BotRecord | None:
    existing = ctx.bot_repo.get(plan.bot_user_id)
    if existing is not None:
        if existing.deleted_at is not None:
            raise ImportError("imports.this_bot_was_removed_locally_explicit_recovery")
        if existing.owner_user_id != plan.owner_user_id:
            raise ImportError("imports.this_bot_is_already_managed_by_another")
        if existing.username != plan.username:
            raise ImportError("imports.the_local_username_differs_reconcile_the_existing")
        if (existing.git_repository, existing.criticality) != (
            plan.git_repository,
            plan.criticality,
        ):
            raise ImportError("imports.local_metadata_differs_use_an_audited_update")
    named = ctx.bot_repo.get_by_username(plan.username)
    if named is not None and named.bot_user_id != plan.bot_user_id:
        raise ImportError("imports.this_username_is_already_bound_to_a")
    return existing


async def prepare_import(
    ctx: CommandContext,
    bot_user_id: str,
    owner_user_id: str,
    git_repository: str,
    criticality: int | str,
) -> ImportPlan:
    if not ctx.approver_ids:
        raise ImportError("import_bot.import_requires_configured_mm_approvers_and_explicit")
    metadata = validate_metadata(git_repository, criticality)
    _id(bot_user_id)
    _id(owner_user_id)
    if bot_user_id == ctx.manager_user_id:
        raise ImportError("imports.the_manager_itself_cannot_be_imported")
    bot = await ctx.mm.get_bot(bot_user_id)
    user = await ctx.mm.get_user(bot_user_id)
    if bot.get("user_id") != bot_user_id or user.get("id") != bot_user_id:
        raise ImportError("imports.remote_bot_identity_does_not_match_the")
    if user.get("is_bot") is not True or bot.get("delete_at") != 0 or user.get("delete_at") != 0:
        raise ImportError("imports.only_active_bot_accounts_can_be_imported")
    username = bot.get("username")
    if (
        not isinstance(username, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", username)
        or user.get("username") != username
    ):
        raise ImportError("imports.remote_bot_username_is_invalid_or_inconsistent")
    if (
        bot.get("system_owned") is True
        or username in {"system", "mattermost", "mattermost-advisor", "playbooks", "boards"}
        or set(str(user.get("roles", "")).split()) != {"system_user"}
    ):
        raise ImportError("imports.system_protected_or_elevated_bot_accounts_cannot")
    remote_owner_id = bot.get("owner_id", "")
    if not isinstance(remote_owner_id, str):
        raise ImportError("imports.the_bot_must_have_a_human_remote")
    _id(remote_owner_id)  # Plugin IDs are not user IDs; also verify the actual user below.
    remote_owner = await ctx.mm.get_user(remote_owner_id)
    owner = await ctx.mm.get_user(owner_user_id)
    for account, expected_id in ((remote_owner, remote_owner_id), (owner, owner_user_id)):
        if (
            account.get("id") != expected_id
            or account.get("is_bot", False) is not False
            or account.get("delete_at") != 0
            or not isinstance(account.get("username"), str)
            or not account["username"]
        ):
            raise ImportError("imports.both_the_remote_and_assigned_owner_must")
    plan = ImportPlan(
        bot_user_id=bot_user_id,
        username=username,
        display_name=bot.get("display_name"),
        description=bot.get("description"),
        remote_owner_id=remote_owner_id,
        owner_user_id=owner_user_id,
        owner_username=owner["username"],
        git_repository=metadata.git_repository,
        criticality=metadata.criticality,
    )
    check_local(ctx, plan)
    return plan


def submit_imports(ctx: CommandContext, plans: list[ImportPlan]) -> list[PendingRequestRecord]:
    """Persist a validated batch atomically. Caller holds the instance lock in CLI."""
    if not ctx.approver_ids:
        raise ImportError("import_bot.import_requires_configured_mm_approvers_and_explicit")
    requests: list[PendingRequestRecord] = []
    with transaction(ctx.bot_repo.conn):
        for plan in plans:
            if check_local(ctx, plan) is not None:
                continue  # Same owner: an idempotent no-op, with original provenance intact.
            row = ctx.bot_repo.conn.execute(
                "SELECT request_id, requester_user_id, username, remote_owner_id, "
                "git_repository, criticality "
                "FROM pending_bot_request WHERE kind = 'import' "
                "AND target_bot_user_id = ? AND status = 'pending'",
                (plan.bot_user_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["requester_user_id"] != plan.owner_user_id
                    or row["username"] != plan.username
                    or row["remote_owner_id"] != plan.remote_owner_id
                    or row["git_repository"] != plan.git_repository
                    or row["criticality"] != plan.criticality
                ):
                    raise ImportError("imports.a_conflicting_import_request_is_pending_deny")
                existing = ctx.pending_repo.get(row["request_id"])
                assert existing is not None
                requests.append(existing)
                continue
            request = ctx.pending_repo.create(
                request_id=uuid4().hex,
                username=plan.username,
                display_name=plan.display_name,
                description=plan.description,
                requester_user_id=plan.owner_user_id,
                requester_username=plan.owner_username,
                kind="import",
                target_bot_user_id=plan.bot_user_id,
                remote_owner_id=plan.remote_owner_id,
                git_repository=plan.git_repository,
                criticality=plan.criticality,
            )
            ctx.bot_repo.conn.execute(
                "INSERT INTO import_submission (request_id, submitted_by) VALUES (?, ?)",
                (request.request_id, ctx.caller_user_id),
            )
            requests.append(request)
    return requests


async def approve_import(ctx: CommandContext, request: PendingRequestRecord) -> str:
    if ctx.channel_type != "D":
        raise ImportError("imports.import_approval_is_only_accepted_in_a")
    if ctx.caller_user_id not in ctx.approver_ids:
        raise ImportError("imports.only_configured_approvers_may_approve_imports")
    if is_self_approval(ctx, request):
        raise ImportError("approve.self_forbidden")
    if request.kind != "import" or not request.target_bot_user_id:
        raise ImportError("imports.invalid_import_request_no_bot_will_be")
    metadata = validate_metadata(request.git_repository, request.criticality)
    plan = await prepare_import(
        ctx,
        request.target_bot_user_id,
        request.requester_user_id,
        metadata.git_repository,
        metadata.criticality,
    )
    if plan.username != request.username or plan.remote_owner_id != request.remote_owner_id:
        raise ImportError("imports.remote_username_or_ownership_changed_deny_and")
    with transaction(ctx.bot_repo.conn):
        # CAS and insert share a transaction: denial, retries, and DB failures cannot half-apply.
        if not ctx.pending_repo.resolve(
            request.request_id, status="approved", resolved_by=ctx.caller_user_id
        ):
            raise ImportError("imports.request_is_already_being_processed_or_resolved")
        if check_local(ctx, plan) is None:
            ctx.bot_repo.insert(
                bot_user_id=plan.bot_user_id,
                username=plan.username,
                display_name=plan.display_name,
                description=plan.description,
                owner_user_id=plan.owner_user_id,
                owner_username=plan.owner_username,
                git_repository=plan.git_repository,
                criticality=plan.criticality,
            )
            ctx.bot_repo.conn.execute(
                "INSERT INTO bot_import (bot_user_id, request_id, imported_at, submitted_by, "
                "approved_by, remote_owner_id) "
                "SELECT ?, request_id, ?, submitted_by, ?, ? FROM import_submission "
                "WHERE request_id = ?",
                (
                    plan.bot_user_id,
                    _now_iso(),
                    ctx.caller_user_id,
                    plan.remote_owner_id,
                    request.request_id,
                ),
            )
            audit = ctx.bot_repo.conn.execute(
                "SELECT bot_user_id FROM bot_import WHERE bot_user_id = ?", (plan.bot_user_id,)
            ).fetchone()
            if audit is None:
                raise ImportError("imports.missing_submission_audit_import_was_rolled_back")
    return plan.bot_user_id


async def notify_import(ctx: CommandContext, request: PendingRequestRecord) -> None:
    for approver_id in ctx.approver_ids:
        try:
            language = await user_language(ctx, approver_id)
            notice = tr(
                language,
                "notice.import",
                request_id=request.request_id,
                username=request.username,
                bot_id=request.target_bot_user_id,
                owner=request.requester_username,
                owner_id=request.requester_user_id,
                remote_owner=request.remote_owner_id,
                repository=request.git_repository,
                criticality=(
                    f"{request.criticality} ({tr(language, f'criticality.{request.criticality}')})"
                ),
                submitter=ctx.caller_user_id,
            )
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, approver_id)
            await ctx.bot_mm.create_post(channel_id=dm["id"], message=notice)
        except Exception:
            pass  # Durable request remains available by ID even if notification fails.
