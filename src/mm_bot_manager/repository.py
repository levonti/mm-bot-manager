from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .db import DbConn
from .metadata import BotMetadata, MetadataError, validate_metadata


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class BotRecord:
    bot_user_id: str
    username: str
    display_name: str | None
    description: str | None
    owner_user_id: str
    owner_username: str
    created_at: str
    deleted_at: str | None
    git_repository: str | None = None
    criticality: int | None = None
    revision: int = 0


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_id: str
    bot_user_id: str
    description: str | None
    created_at: str
    revoked_at: str | None


def _bot_from_row(row: Any) -> BotRecord:
    return BotRecord(
        bot_user_id=row["bot_user_id"],
        username=row["username"],
        display_name=row["display_name"],
        description=row["description"],
        owner_user_id=row["owner_user_id"],
        owner_username=row["owner_username"],
        created_at=row["created_at"],
        deleted_at=row["deleted_at"],
        git_repository=row["git_repository"],
        criticality=row["criticality"],
        # Read-only CLI previews can inspect the previous release before startup migration.
        revision=dict(row).get("revision", 0),
    )


def _token_from_row(row: Any) -> TokenRecord:
    return TokenRecord(
        token_id=row["token_id"],
        bot_user_id=row["bot_user_id"],
        description=row["description"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


class BotRepo:
    def __init__(self, conn: DbConn) -> None:
        self._conn = conn

    @property
    def conn(self) -> DbConn:
        return self._conn

    def insert(
        self,
        *,
        bot_user_id: str,
        username: str,
        display_name: str | None,
        description: str | None,
        owner_user_id: str,
        owner_username: str,
        git_repository: str,
        criticality: int,
    ) -> BotRecord:
        metadata = validate_metadata(git_repository, criticality)
        created_at = _now_iso()
        self._conn.execute(
            """
            INSERT INTO bot (bot_user_id, username, display_name, description,
                             owner_user_id, owner_username, created_at, git_repository, criticality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_user_id,
                username,
                display_name,
                description,
                owner_user_id,
                owner_username,
                created_at,
                metadata.git_repository,
                metadata.criticality,
            ),
        )
        return BotRecord(
            bot_user_id=bot_user_id,
            username=username,
            display_name=display_name,
            description=description,
            owner_user_id=owner_user_id,
            owner_username=owner_username,
            created_at=created_at,
            deleted_at=None,
            git_repository=metadata.git_repository,
            criticality=metadata.criticality,
        )

    def get_by_owner_and_username(self, owner_user_id: str, username: str) -> BotRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM bot
            WHERE owner_user_id = ? AND username = ? AND deleted_at IS NULL
            """,
            (owner_user_id, username),
        ).fetchone()
        return _bot_from_row(row) if row else None

    def get(self, bot_user_id: str) -> BotRecord | None:
        """Include deleted records: imports must never resurrect or reassign them."""
        row = self._conn.execute(
            "SELECT * FROM bot WHERE bot_user_id = ?", (bot_user_id,)
        ).fetchone()
        return _bot_from_row(row) if row else None

    def get_by_username(self, username: str) -> BotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM bot WHERE username = ? AND deleted_at IS NULL", (username,)
        ).fetchone()
        return _bot_from_row(row) if row else None

    def list_by_owner(self, owner_user_id: str) -> list[BotRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM bot
            WHERE owner_user_id = ? AND deleted_at IS NULL
            ORDER BY created_at ASC
            """,
            (owner_user_id,),
        ).fetchall()
        return [_bot_from_row(r) for r in rows]

    def patch(
        self,
        bot_user_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[str] = []
        if username is not None:
            sets.append("username = ?")
            args.append(username)
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(display_name)
        if description is not None:
            sets.append("description = ?")
            args.append(description)
        if not sets:
            return
        args.append(bot_user_id)
        self._conn.execute(
            f"UPDATE bot SET {', '.join(sets)} WHERE bot_user_id = ?",
            args,
        )

    def mark_deleted(self, bot_user_id: str) -> None:
        self._conn.execute(
            "UPDATE bot SET deleted_at = ? WHERE bot_user_id = ?",
            (_now_iso(), bot_user_id),
        )


@dataclass(frozen=True, slots=True)
class PendingRequestRecord:
    request_id: str
    username: str
    display_name: str | None
    description: str | None
    requester_user_id: str
    requester_username: str
    created_at: str
    status: str  # pending | provisioning | approved | denied | needs_review
    resolved_at: str | None
    resolved_by: str | None
    deny_reason: str | None
    kind: str = "create"
    target_bot_user_id: str | None = None
    remote_owner_id: str | None = None
    git_repository: str | None = None
    criticality: int | None = None


def _pending_from_row(row: Any) -> PendingRequestRecord:
    return PendingRequestRecord(
        request_id=row["request_id"],
        username=row["username"],
        display_name=row["display_name"],
        description=row["description"],
        requester_user_id=row["requester_user_id"],
        requester_username=row["requester_username"],
        created_at=row["created_at"],
        status=row["status"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        deny_reason=row["deny_reason"],
        kind=row["kind"],
        target_bot_user_id=row["target_bot_user_id"],
        remote_owner_id=row["remote_owner_id"],
        git_repository=row["git_repository"],
        criticality=row["criticality"],
    )


class PendingRequestRepo:
    def __init__(self, conn: DbConn) -> None:
        self._conn = conn

    def create(
        self,
        *,
        request_id: str,
        username: str,
        display_name: str | None,
        description: str | None,
        requester_user_id: str,
        requester_username: str,
        git_repository: str,
        criticality: int,
        kind: str = "create",
        target_bot_user_id: str | None = None,
        remote_owner_id: str | None = None,
    ) -> PendingRequestRecord:
        metadata = validate_metadata(git_repository, criticality)
        created_at = _now_iso()
        self._conn.execute(
            """
            INSERT INTO pending_bot_request
                (request_id, username, display_name, description,
                 requester_user_id, requester_username, created_at,
                 kind, target_bot_user_id, remote_owner_id, git_repository, criticality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                username,
                display_name,
                description,
                requester_user_id,
                requester_username,
                created_at,
                kind,
                target_bot_user_id,
                remote_owner_id,
                metadata.git_repository,
                metadata.criticality,
            ),
        )
        return PendingRequestRecord(
            request_id=request_id,
            username=username,
            display_name=display_name,
            description=description,
            requester_user_id=requester_user_id,
            requester_username=requester_username,
            created_at=created_at,
            status="pending",
            resolved_at=None,
            resolved_by=None,
            deny_reason=None,
            kind=kind,
            target_bot_user_id=target_bot_user_id,
            remote_owner_id=remote_owner_id,
            git_repository=metadata.git_repository,
            criticality=metadata.criticality,
        )

    def get(self, request_id: str) -> PendingRequestRecord | None:
        row = self._conn.execute(
            "SELECT * FROM pending_bot_request WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return _pending_from_row(row) if row else None

    def resolve(
        self,
        request_id: str,
        *,
        status: str,
        resolved_by: str,
        deny_reason: str | None = None,
        expected_status: str = "pending",
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE pending_bot_request
               SET status = ?, resolved_at = ?, resolved_by = ?, deny_reason = ?
             WHERE request_id = ? AND status = ?
            """,
            (status, _now_iso(), resolved_by, deny_reason, request_id, expected_status),
        )
        return bool(cursor.rowcount == 1)

    def claim(self, request_id: str, resolved_by: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE pending_bot_request SET status = 'provisioning', resolved_by = ? "
            "WHERE request_id = ? AND status = 'pending'",
            (resolved_by, request_id),
        )
        return bool(cursor.rowcount == 1)

    def release(self, request_id: str) -> None:
        self._conn.execute(
            "UPDATE pending_bot_request SET status = 'pending', resolved_by = NULL, "
            "resolved_at = NULL WHERE request_id = ? "
            "AND status IN ('provisioning', 'needs_review')",
            (request_id,),
        )


class TokenRepo:
    def __init__(self, conn: DbConn) -> None:
        self._conn = conn

    def record(self, *, token_id: str, bot_user_id: str, description: str | None) -> TokenRecord:
        created_at = _now_iso()
        self._conn.execute(
            """
            INSERT INTO token (token_id, bot_user_id, description, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_id, bot_user_id, description, created_at),
        )
        return TokenRecord(
            token_id=token_id,
            bot_user_id=bot_user_id,
            description=description,
            created_at=created_at,
            revoked_at=None,
        )

    def list_active_for_bot(self, bot_user_id: str) -> list[TokenRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM token
            WHERE bot_user_id = ? AND revoked_at IS NULL
            ORDER BY created_at ASC
            """,
            (bot_user_id,),
        ).fetchall()
        return [_token_from_row(r) for r in rows]

    def get(self, token_id: str) -> TokenRecord | None:
        row = self._conn.execute(
            "SELECT * FROM token WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        return _token_from_row(row) if row else None

    def mark_revoked(self, token_id: str) -> None:
        self._conn.execute(
            "UPDATE token SET revoked_at = ? WHERE token_id = ?",
            (_now_iso(), token_id),
        )


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    kind: str
    resource: str
    request_id: str | None
    status: str
    remote_id: str | None


class OperationRepo:
    """Durable intent and remote IDs only; never stores token values or reply bodies."""

    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def start(self, kind: str, resource: str, request_id: str | None = None) -> str:
        operation_id = uuid4().hex
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO operation (operation_id, kind, resource, request_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'started', ?, ?)",
            (operation_id, kind, resource, request_id, now, now),
        )
        return operation_id

    def update(self, operation_id: str, status: str, remote_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE operation SET status = ?, remote_id = COALESCE(?, remote_id), "
            "updated_at = ? WHERE operation_id = ?",
            (status, remote_id, _now_iso(), operation_id),
        )

    def unfinished(self) -> list[OperationRecord]:
        rows = self.conn.execute(
            "SELECT operation_id, kind, resource, request_id, status, remote_id FROM operation "
            "WHERE status NOT IN ('completed', 'compensated') ORDER BY created_at, operation_id"
        ).fetchall()
        return [OperationRecord(**dict(row)) for row in rows]

    def active_for(self, kind: str, resource: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM operation WHERE kind = ? AND resource = ? "
                "AND status NOT IN ('completed', 'compensated')",
                (kind, resource),
            ).fetchone()
            is not None
        )


class PostRepo:
    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def claim(self, post_id: str) -> bool:
        cursor = self.conn.execute(
            "INSERT INTO processed_post (post_id, status, created_at) "
            "VALUES (?, 'processing', ?) ON CONFLICT (post_id) DO NOTHING",
            (post_id, _now_iso()),
        )
        return bool(cursor.rowcount == 1)

    def finish(self, post_id: str, *, interrupted: bool = False) -> None:
        self.conn.execute(
            "UPDATE processed_post SET status = ?, finished_at = ? WHERE post_id = ?",
            ("interrupted" if interrupted else "completed", _now_iso(), post_id),
        )

    def interrupt_unfinished(self) -> int:
        cursor = self.conn.execute(
            "UPDATE processed_post SET status = 'interrupted', finished_at = ? "
            "WHERE status = 'processing'",
            (_now_iso(),),
        )
        return int(cursor.rowcount)


class MetadataRepo:
    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def set(
        self,
        record_type: str,
        record_id: str,
        metadata: BotMetadata,
        *,
        actor: str,
        source: str,
        only_missing: bool = False,
    ) -> None:
        """Call inside a transaction so data and audit cannot diverge."""
        if record_type not in {"bot", "request"}:
            raise MetadataError("repository.record_type_must_be_bot_or_request")
        table, key = (
            ("bot", "bot_user_id")
            if record_type == "bot"
            else ("pending_bot_request", "request_id")
        )
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)).fetchone()
        if row is None:
            raise MetadataError("repository.registry_record_not_found")
        if record_type == "request" and row["status"] != "pending":
            raise MetadataError("repository.only_pending_legacy_requests_may_be_backfilled")
        old_repo, old_criticality = row["git_repository"], row["criticality"]
        if (old_repo, old_criticality) == (metadata.git_repository, metadata.criticality):
            return
        if only_missing and old_repo is not None and old_criticality is not None:
            raise MetadataError("repository.backfill_cannot_replace_existing_metadata_use_an")
        self.conn.execute(
            f"UPDATE {table} SET git_repository = ?, criticality = ? WHERE {key} = ?",
            (metadata.git_repository, metadata.criticality, record_id),
        )
        self.conn.execute(
            "INSERT INTO metadata_audit (audit_id, record_type, record_id, old_git_repository, "
            "old_criticality, git_repository, criticality, changed_by, source, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record_type,
                record_id,
                old_repo,
                old_criticality,
                metadata.git_repository,
                metadata.criticality,
                actor,
                source,
                _now_iso(),
            ),
        )

    def missing(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for table, key, kind, condition in (
            ("bot", "bot_user_id", "bot", "1 = 1"),
            ("pending_bot_request", "request_id", "request", "status = 'pending'"),
        ):
            # SELECT * supports read-only inspection before additive schema migration.
            for raw in self.conn.execute(f"SELECT * FROM {table} WHERE {condition} ORDER BY {key}"):
                row = dict(raw)
                if row.get("git_repository") is not None and row.get("criticality") is not None:
                    continue
                records.append(
                    {
                        "record_type": kind,
                        "record_id": row[key],
                        "git_repository": row.get("git_repository"),
                        "criticality": row.get("criticality"),
                    }
                )
        return records


class CatalogRepo:
    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def page(
        self,
        *,
        criticality: int | None = None,
        owner: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = ["b.deleted_at IS NULL"]
        params: list[Any] = []
        if criticality is not None:
            conditions.append("b.criticality = ?")
            params.append(criticality)
        if owner is not None:
            conditions.append("b.owner_username = ?")
            params.append(owner)
        if search is not None:
            # Literal, case-insensitive search; '%' and '_' are not wildcards.
            conditions.append(
                "(LOWER(b.username) LIKE ? ESCAPE '!' OR "
                "LOWER(COALESCE(b.description, '')) LIKE ? ESCAPE '!' OR "
                "LOWER(COALESCE(b.git_repository, '')) LIKE ? ESCAPE '!')"
            )
            pattern = (
                "%" + search.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            )
            params.extend([pattern] * 3)
        where = " AND ".join(conditions)
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM bot b WHERE {where}", params
        ).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT b.*, CASE WHEN i.bot_user_id IS NULL "
            "THEN 'created' ELSE 'imported' END AS source "
            "FROM bot b LEFT JOIN bot_import i ON i.bot_user_id = b.bot_user_id "
            f"WHERE {where} ORDER BY "
            "CASE WHEN b.criticality IS NULL THEN 4 ELSE b.criticality END, "
            "b.username, b.bot_user_id LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        return [dict(row) for row in rows], total


class PreferenceRepo:
    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def get(self, user_id: str) -> tuple[str, str]:
        row = self.conn.execute(
            "SELECT language, catalog_format FROM user_preference WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row["language"], row["catalog_format"]) if row else ("auto", "cards")

    def language(self, user_id: str, language: str) -> None:
        if language not in {"auto", "en", "ru", "de"}:
            raise ValueError("Invalid language code")
        self.conn.execute(
            "INSERT INTO user_preference (user_id, language) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET language = excluded.language",
            (user_id, language),
        )

    def format(self, user_id: str, catalog_format: str) -> None:
        if catalog_format not in {"cards", "table"}:
            raise ValueError("Invalid catalog format")
        self.conn.execute(
            "INSERT INTO user_preference (user_id, catalog_format) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET catalog_format = excluded.catalog_format",
            (user_id, catalog_format),
        )
