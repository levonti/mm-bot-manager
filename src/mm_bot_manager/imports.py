"""Import existing bots using remote reads and atomic local transactions only.

Do not use creation compensation here: an imported bot predates this manager.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from .db import transaction
from .metadata import validate_metadata
from .repository import BotRecord, PendingRequestRecord, _now_iso

if TYPE_CHECKING:
    from .commands.context import CommandContext


class ImportError(ValueError):
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
        raise ImportError("Expected an immutable Mattermost user ID (26 lowercase letters/digits).")
    return value


def check_local(ctx: CommandContext, plan: ImportPlan) -> BotRecord | None:
    existing = ctx.bot_repo.get(plan.bot_user_id)
    if existing is not None:
        if existing.deleted_at is not None:
            raise ImportError("This bot was removed locally; explicit recovery is required.")
        if existing.owner_user_id != plan.owner_user_id:
            raise ImportError(
                "This bot is already managed by another owner; transfer is not import."
            )
        if existing.username != plan.username:
            raise ImportError("The local username differs; reconcile the existing record first.")
        if (existing.git_repository, existing.criticality) != (
            plan.git_repository,
            plan.criticality,
        ):
            raise ImportError("Local metadata differs; use an audited update or legacy backfill.")
    named = ctx.bot_repo.get_by_username(plan.username)
    if named is not None and named.bot_user_id != plan.bot_user_id:
        raise ImportError("This username is already bound to a different local bot ID.")
    return existing


async def prepare_import(
    ctx: CommandContext,
    bot_user_id: str,
    owner_user_id: str,
    git_repository: str,
    criticality: int | str,
) -> ImportPlan:
    if not ctx.approver_ids:
        raise ImportError("Import requires configured MM_APPROVERS and explicit approval.")
    metadata = validate_metadata(git_repository, criticality)
    _id(bot_user_id)
    _id(owner_user_id)
    if bot_user_id == ctx.manager_user_id:
        raise ImportError("The manager itself cannot be imported.")
    bot = await ctx.mm.get_bot(bot_user_id)
    user = await ctx.mm.get_user(bot_user_id)
    if bot.get("user_id") != bot_user_id or user.get("id") != bot_user_id:
        raise ImportError("Remote bot identity does not match the requested ID.")
    if user.get("is_bot") is not True or bot.get("delete_at") != 0 or user.get("delete_at") != 0:
        raise ImportError("Only active bot accounts can be imported.")
    username = bot.get("username")
    if (
        not isinstance(username, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", username)
        or user.get("username") != username
    ):
        raise ImportError("Remote bot username is invalid or inconsistent.")
    if (
        bot.get("system_owned") is True
        or username in {"system", "mattermost", "mattermost-advisor", "playbooks", "boards"}
        or set(str(user.get("roles", "")).split()) != {"system_user"}
    ):
        raise ImportError("System, protected, or elevated bot accounts cannot be imported.")
    remote_owner_id = bot.get("owner_id", "")
    if not isinstance(remote_owner_id, str):
        raise ImportError("The bot must have a human remote owner.")
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
            raise ImportError("Both the remote and assigned owner must be active human users.")
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
        raise ImportError("Import requires configured MM_APPROVERS and explicit approval.")
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
                    raise ImportError("A conflicting import request is pending; deny it first.")
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
        raise ImportError("Import approval is only accepted in a direct message to this manager.")
    if ctx.caller_user_id not in ctx.approver_ids:
        raise ImportError("Only configured approvers may approve imports.")
    if request.kind != "import" or not request.target_bot_user_id:
        raise ImportError("Invalid import request; no bot will be created.")
    metadata = validate_metadata(request.git_repository, request.criticality)
    plan = await prepare_import(
        ctx,
        request.target_bot_user_id,
        request.requester_user_id,
        metadata.git_repository,
        metadata.criticality,
    )
    if plan.username != request.username or plan.remote_owner_id != request.remote_owner_id:
        raise ImportError("Remote username or ownership changed; deny and submit a new request.")
    with transaction(ctx.bot_repo.conn):
        # CAS and insert share a transaction: denial, retries, and DB failures cannot half-apply.
        if not ctx.pending_repo.resolve(
            request.request_id, status="approved", resolved_by=ctx.caller_user_id
        ):
            raise ImportError("Request is already being processed or resolved.")
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
                raise ImportError("Missing submission audit; import was rolled back.")
    return plan.bot_user_id


async def notify_import(ctx: CommandContext, request: PendingRequestRecord) -> None:
    notice = (
        f"**Existing bot import request** (ID: `{request.request_id}`)\n"
        f"- bot: `@{request.username}` (`{request.target_bot_user_id}`)\n"
        f"- assigned owner: @{request.requester_username} (`{request.requester_user_id}`)\n"
        f"- remote owner: `{request.remote_owner_id}`\n"
        f"- repository: {request.git_repository}\n"
        f"- criticality: {request.criticality} (1 critical, 2 important, 3 auxiliary)\n"
        f"- submitted by: `{ctx.caller_user_id}`\n\n"
        "Approval grants bot management and token issuance. Existing tokens stay unchanged.\n"
        f"Reply `!approve {request.request_id}` or `!deny {request.request_id} [reason]`."
    )
    for approver_id in ctx.approver_ids:
        try:
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, approver_id)
            await ctx.bot_mm.create_post(channel_id=dm["id"], message=notice)
        except Exception:
            pass  # Durable request remains available by ID even if notification fails.
