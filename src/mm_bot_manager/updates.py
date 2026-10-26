"""Durable update proposals and reconciliation of uncertain Mattermost writes.

The proposal is also the update journal. Claim it before any API mutation and keep
its immutable before/after snapshots until both the local data and resolution commit.
Creation compensation must never be used for an existing bot.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from secrets import token_hex
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from .db import DbConn, transaction
from .i18n import UserFacingError, tr
from .localization import user_language
from .logging import get_logger
from .metadata import cell, validate_metadata
from .permissions import is_approver, is_self_approval
from .repository import BotRecord, MetadataRepo, PendingRequestRecord
from .validation import validate_bot_fields

if TYPE_CHECKING:
    from .commands.context import CommandContext

log = get_logger(__name__)
REMOTE_FIELDS = ("username", "display_name", "description")
SNAPSHOT_FIELDS = (*REMOTE_FIELDS, "git_repository", "criticality", "owner_user_id", "revision")
_locks: WeakKeyDictionary[DbConn, dict[str, asyncio.Lock]] = WeakKeyDictionary()


class UpdateError(UserFacingError):
    pass


@asynccontextmanager
async def bot_mutation(ctx: CommandContext, bot_user_id: str) -> AsyncIterator[None]:
    # The process has the exclusive DB instance lock; also serialize its async workers.
    locks = _locks.setdefault(ctx.bot_repo.conn, {})
    lock = locks.setdefault(bot_user_id, asyncio.Lock())
    if lock.locked():
        raise UpdateError("updates.mutation_busy")
    async with lock:
        try:
            yield
        finally:
            locks.pop(bot_user_id, None)


@dataclass(frozen=True, slots=True)
class UpdateRecord:
    request_id: str
    bot_user_id: str
    old: dict[str, Any]
    new: dict[str, Any]

    @property
    def remote_fields(self) -> list[str]:
        return [field for field in REMOTE_FIELDS if self.old[field] != self.new[field]]


def snapshot(bot: BotRecord) -> dict[str, Any]:
    values = {field: getattr(bot, field) for field in SNAPSHOT_FIELDS}
    for field in ("display_name", "description"):
        values[field] = values[field] or ""
    return values


class UpdateRepo:
    def __init__(self, conn: DbConn):
        self.conn = conn

    def get(self, request_id: str) -> UpdateRecord | None:
        row = self.conn.execute(
            "SELECT * FROM bot_update_request WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        return UpdateRecord(
            row["request_id"],
            row["bot_user_id"],
            json.loads(row["old_values"]),
            json.loads(row["new_values"]),
        )

    def active(self, bot_user_id: str) -> PendingRequestRecord | None:
        row = self.conn.execute(
            "SELECT r.request_id FROM bot_update_request u JOIN pending_bot_request r "
            "ON r.request_id = u.request_id WHERE u.bot_user_id = ? AND u.active = 1",
            (bot_user_id,),
        ).fetchone()
        if row is None:
            return None
        from .repository import PendingRequestRepo

        return PendingRequestRepo(self.conn).get(row["request_id"])

    def finish(self, request_id: str) -> None:
        self.conn.execute(
            "UPDATE bot_update_request SET active = 0 WHERE request_id = ?", (request_id,)
        )

    def unfinished(self) -> list[UpdateRecord]:
        rows = self.conn.execute(
            "SELECT u.request_id FROM bot_update_request u JOIN pending_bot_request r "
            "ON r.request_id = u.request_id WHERE u.active = 1 "
            "AND r.status IN ('provisioning', 'needs_review') ORDER BY r.created_at, r.request_id"
        ).fetchall()
        return [record for row in rows if (record := self.get(row["request_id"])) is not None]


def require_idle(ctx: CommandContext, bot_user_id: str) -> None:
    request = UpdateRepo(ctx.bot_repo.conn).active(bot_user_id)
    if request and request.status in {"provisioning", "needs_review"}:
        raise UpdateError("updates.busy", request_id=request.request_id)


def _lock_bot(ctx: CommandContext, bot_user_id: str) -> None:
    # SQLite's write transaction and PostgreSQL's row lock serialize claim/finalization.
    suffix = " FOR UPDATE" if ctx.bot_repo.conn.dialect == "postgres" else ""
    ctx.bot_repo.conn.execute(
        "SELECT bot_user_id FROM bot WHERE bot_user_id = ?" + suffix, (bot_user_id,)
    ).fetchone()


def _current(ctx: CommandContext, record: UpdateRecord) -> BotRecord:
    bot = ctx.bot_repo.get(record.bot_user_id)
    if bot is None or bot.deleted_at is not None or snapshot(bot) != record.old:
        raise UpdateError("updates.stale")
    return bot


def _requester_allowed(
    ctx: CommandContext, req: PendingRequestRecord, record: UpdateRecord
) -> bool:
    return req.requester_user_id == record.old["owner_user_id"] or (
        req.requester_user_id in ctx.approver_ids and not record.remote_fields
    )


def _diff(record: UpdateRecord, language: str) -> str:
    labels = {
        "git_repository": "repository",
        "username": "username",
        "display_name": "display_name",
        "description": "purpose",
        "criticality": "criticality",
    }

    def value(raw: Any) -> str:
        # JSON makes whitespace/clearing explicit; preserve every character for review.
        text = json.dumps(raw, ensure_ascii=False)
        for char in ("\\", "|", "`", "[", "]", "*", "_", "<", ">", "@"):
            text = text.replace(char, "\\" + char)
        return text

    return "\n".join(
        f"- {tr(language, f'label.{label}')}: "
        f"{value(record.old[field])} → {value(record.new[field])}"
        for field, label in labels.items()
        if record.old[field] != record.new[field]
    )


async def submit_update(ctx: CommandContext, bot: BotRecord, changes: dict[str, Any]) -> str:
    validate_bot_fields(changes)
    if not ctx.approver_ids:
        raise UpdateError("updates.approvers_required")
    old = snapshot(bot)
    new = old | changes
    if new == old:
        return ctx.tr("updates.unchanged")
    metadata = validate_metadata(new["git_repository"], new["criticality"])
    request_id = token_hex(8)
    record = UpdateRecord(request_id, bot.bot_user_id, old, new)
    if any(len(_diff(record, language).encode("utf-8")) > 12000 for language in ("en", "ru")):
        raise UpdateError("updates.review_too_large")
    with transaction(ctx.bot_repo.conn):
        _lock_bot(ctx, bot.bot_user_id)
        _current(ctx, UpdateRecord(request_id, bot.bot_user_id, old, new))
        active = UpdateRepo(ctx.bot_repo.conn).active(bot.bot_user_id)
        if active:
            raise UpdateError("updates.busy", request_id=active.request_id)
        request = ctx.pending_repo.create(
            request_id=request_id,
            username=bot.username,
            display_name=bot.display_name,
            description=bot.description,
            requester_user_id=ctx.caller_user_id,
            requester_username=ctx.caller_username,
            git_repository=metadata.git_repository,
            criticality=metadata.criticality,
            kind="update",
            target_bot_user_id=bot.bot_user_id,
        )
        ctx.bot_repo.conn.execute(
            "INSERT INTO bot_update_request "
            "(request_id, bot_user_id, old_values, new_values) VALUES (?, ?, ?, ?)",
            (request_id, bot.bot_user_id, json.dumps(old), json.dumps(new)),
        )
    for reviewer in ctx.approver_ids - {ctx.caller_user_id}:
        try:
            language = await user_language(ctx, reviewer)
            dm = await ctx.bot_mm.create_direct_channel(ctx.manager_user_id, reviewer)
            await ctx.bot_mm.create_post(
                channel_id=dm["id"],
                message=tr(
                    language,
                    "notice.update",
                    request_id=request.request_id,
                    username=cell(bot.username),
                    bot_id=bot.bot_user_id,
                    requester=cell(ctx.caller_username),
                    diff=_diff(record, language),
                ),
            )
        except Exception:
            log.warning("update_notification_failed", request_id=request_id)
    return ctx.tr("updates.submitted", request_id=request_id)


async def _remote(ctx: CommandContext, record: UpdateRecord) -> dict[str, str]:
    user = await ctx.mm.get_user(record.bot_user_id)
    bot = await ctx.mm.get_bot(record.bot_user_id)
    if (
        user.get("id") != record.bot_user_id
        or not user.get("is_bot")
        or user.get("delete_at")
        or bot.get("user_id") != record.bot_user_id
        or bot.get("delete_at")
    ):
        raise UpdateError("updates.remote_changed")
    return {
        "username": user["username"],
        "display_name": bot.get("display_name") or "",
        "description": bot.get("description") or "",
    }


def _remote_keys(record: UpdateRecord) -> set[str]:
    return {"username", *record.remote_fields}


def _matches(remote: dict[str, str], values: dict[str, Any], record: UpdateRecord) -> bool:
    return all(remote[key] == values[key] for key in _remote_keys(record))


async def _apply_remote(ctx: CommandContext, record: UpdateRecord) -> None:
    # Recovery can also reach this path using proposals saved by older versions.
    validate_bot_fields({field: record.new[field] for field in record.remote_fields})
    remote = await _remote(ctx, record)
    if any(remote[key] not in (record.old[key], record.new[key]) for key in _remote_keys(record)):
        raise UpdateError("updates.remote_changed")
    profile = {
        field: record.new[field]
        for field in ("display_name", "description")
        if field in record.remote_fields and remote[field] != record.new[field]
    }
    if profile:
        await ctx.mm.patch_bot(record.bot_user_id, **profile)
    if remote["username"] != record.new["username"]:
        await ctx.mm.patch_user(record.bot_user_id, username=record.new["username"])
    if not _matches(await _remote(ctx, record), record.new, record):
        raise UpdateError("updates.remote_changed")


def _finish(ctx: CommandContext, record: UpdateRecord, reviewer: str) -> None:
    with transaction(ctx.bot_repo.conn):
        _lock_bot(ctx, record.bot_user_id)
        _current(ctx, record)
        req = ctx.pending_repo.get(record.request_id)
        if (
            req is None
            or req.status not in {"provisioning", "needs_review"}
            or req.resolved_by != reviewer
        ):
            raise UpdateError("updates.stale")
        metadata = validate_metadata(record.new["git_repository"], record.new["criticality"])
        MetadataRepo(ctx.bot_repo.conn).set(
            "bot",
            record.bot_user_id,
            metadata,
            actor=reviewer,
            source=f"approval:{record.request_id}",
        )
        ctx.bot_repo.patch(
            record.bot_user_id,
            **{
                field: record.new[field]
                for field in REMOTE_FIELDS
                if record.old[field] != record.new[field]
            },
        )
        if not ctx.pending_repo.resolve(
            record.request_id,
            status="approved",
            resolved_by=reviewer,
            expected_status=req.status,
        ):
            raise UpdateError("updates.stale")
        UpdateRepo(ctx.bot_repo.conn).finish(record.request_id)


def _review(ctx: CommandContext, request_id: str) -> None:
    ctx.bot_repo.conn.execute(
        "UPDATE pending_bot_request SET status = 'needs_review' "
        "WHERE request_id = ? AND status = 'provisioning'",
        (request_id,),
    )


async def approve_update(ctx: CommandContext, req: PendingRequestRecord) -> str:
    if ctx.channel_type != "D":
        raise UpdateError("updates.dm")
    if not is_approver(ctx):
        raise UpdateError("approve.you_are_not_authorised_to_approve_bot")
    if is_self_approval(ctx, req):
        raise UpdateError("approve.self_forbidden")
    record = UpdateRepo(ctx.bot_repo.conn).get(req.request_id)
    if record is None:
        raise UpdateError("updates.missing_journal")
    validate_bot_fields({field: record.new[field] for field in record.remote_fields})
    async with bot_mutation(ctx, record.bot_user_id):
        try:
            await _people(ctx, req, ctx.caller_user_id)
        except UpdateError:
            with transaction(ctx.bot_repo.conn):
                if ctx.pending_repo.resolve(
                    req.request_id, status="stale", resolved_by=ctx.caller_user_id
                ):
                    UpdateRepo(ctx.bot_repo.conn).finish(req.request_id)
            raise
        except Exception:
            raise UpdateError("updates.people_unavailable") from None
        return await _approve_update(ctx, req, record)


async def _people(ctx: CommandContext, req: PendingRequestRecord, reviewer: str) -> None:
    for user_id in (req.requester_user_id, reviewer):
        user = await ctx.mm.get_user(user_id)
        if user.get("id") != user_id or user.get("delete_at") or user.get("is_bot"):
            raise UpdateError("updates.stale")


async def _approve_update(
    ctx: CommandContext, req: PendingRequestRecord, record: UpdateRecord
) -> str:
    with transaction(ctx.bot_repo.conn):
        _lock_bot(ctx, record.bot_user_id)
        try:
            _current(ctx, record)
            if not _requester_allowed(ctx, req, record):
                raise UpdateError("updates.stale")
        except UpdateError:
            if ctx.pending_repo.resolve(
                req.request_id, status="stale", resolved_by=ctx.caller_user_id
            ):
                UpdateRepo(ctx.bot_repo.conn).finish(req.request_id)
            stale = True
        else:
            stale = False
            if not ctx.pending_repo.claim(req.request_id, ctx.caller_user_id):
                raise UpdateError(
                    "deny.request_is_already_being_processed_or_resolved", v0=req.request_id
                )
    if stale:
        raise UpdateError("updates.stale")
    try:
        if record.remote_fields:
            if not _matches(await _remote(ctx, record), record.old, record):
                with transaction(ctx.bot_repo.conn):
                    ctx.pending_repo.resolve(
                        req.request_id,
                        status="stale",
                        resolved_by=ctx.caller_user_id,
                        expected_status="provisioning",
                    )
                    UpdateRepo(ctx.bot_repo.conn).finish(req.request_id)
                raise UpdateError("updates.remote_changed")
            await _apply_remote(ctx, record)
        _finish(ctx, record, ctx.caller_user_id)
    except BaseException as exc:
        _review(ctx, req.request_id)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("updates.needs_review", request_id=req.request_id) from None
    return str(record.new["username"])


async def recover_updates(ctx: CommandContext) -> int:
    """Reconcile under the exclusive instance lock before processing new commands."""
    unresolved = 0
    for record in UpdateRepo(ctx.bot_repo.conn).unfinished():
        try:
            req = ctx.pending_repo.get(record.request_id)
            if (
                req is None
                or req.resolved_by not in ctx.approver_ids
                or req.resolved_by == req.requester_user_id
                or not _requester_allowed(ctx, req, record)
            ):
                raise UpdateError("updates.stale")
            await _people(ctx, req, req.resolved_by)
            _current(ctx, record)
            if record.remote_fields:
                remote = await _remote(ctx, record)
                if _matches(remote, record.old, record):
                    # No observed remote change: require a fresh approval before retrying.
                    ctx.pending_repo.release(record.request_id)
                    continue
                await _apply_remote(ctx, record)
            _finish(ctx, record, req.resolved_by)
        except Exception:
            _review(ctx, record.request_id)
            unresolved += 1
            log.error("update_requires_review", request_id=record.request_id)
    return unresolved
