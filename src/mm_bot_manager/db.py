import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any, Literal

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_preference (
    user_id TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'auto' CHECK (language IN ('auto', 'en', 'ru')),
    catalog_format TEXT NOT NULL DEFAULT 'cards' CHECK (catalog_format IN ('cards', 'table'))
);

CREATE TABLE IF NOT EXISTS bot (
    bot_user_id    TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    display_name   TEXT,
    description    TEXT,
    owner_user_id  TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_owner    ON bot(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_bot_username ON bot(username);

CREATE TABLE IF NOT EXISTS token (
    token_id    TEXT PRIMARY KEY,
    bot_user_id TEXT NOT NULL REFERENCES bot(bot_user_id),
    description TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_token_bot ON token(bot_user_id);

CREATE TABLE IF NOT EXISTS pending_bot_request (
    request_id          TEXT PRIMARY KEY,
    username            TEXT NOT NULL,
    display_name        TEXT,
    description         TEXT,
    requester_user_id   TEXT NOT NULL,
    requester_username  TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    resolved_at         TEXT,
    resolved_by         TEXT,
    deny_reason         TEXT,
    kind                TEXT NOT NULL DEFAULT 'create',
    target_bot_user_id  TEXT,
    remote_owner_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_status ON pending_bot_request(status);

CREATE TABLE IF NOT EXISTS bot_import (
    bot_user_id TEXT PRIMARY KEY REFERENCES bot(bot_user_id),
    request_id TEXT NOT NULL UNIQUE REFERENCES pending_bot_request(request_id),
    source TEXT NOT NULL DEFAULT 'imported' CHECK (source = 'imported'),
    imported_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    remote_owner_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_submission (
    request_id TEXT PRIMARY KEY REFERENCES pending_bot_request(request_id),
    submitted_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('bot_create', 'token_create')),
    resource TEXT NOT NULL,
    request_id TEXT REFERENCES pending_bot_request(request_id),
    status TEXT NOT NULL CHECK (status IN
        ('started', 'remote_created', 'recorded', 'completed', 'compensated', 'needs_review')),
    remote_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_active ON operation(kind, resource)
    WHERE status IN ('started', 'remote_created', 'recorded', 'needs_review');

CREATE TABLE IF NOT EXISTS processed_post (
    post_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'interrupted')),
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS metadata_audit (
    audit_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL CHECK (record_type IN ('bot', 'request')),
    record_id TEXT NOT NULL,
    old_git_repository TEXT,
    old_criticality INTEGER,
    git_repository TEXT NOT NULL CHECK (length(trim(git_repository)) > 0),
    criticality INTEGER NOT NULL CHECK (criticality BETWEEN 1 AND 3),
    changed_by TEXT NOT NULL,
    source TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_update_request (
    request_id TEXT PRIMARY KEY REFERENCES pending_bot_request(request_id),
    bot_user_id TEXT NOT NULL REFERENCES bot(bot_user_id),
    old_values TEXT NOT NULL,
    new_values TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_update_active ON bot_update_request(bot_user_id)
    WHERE active = 1;

CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_SCHEMA_STMTS = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]


class DbConn:
    """Unified connection adapter for SQLite and PostgreSQL.

    Translates ``?`` parameter placeholders to ``%s`` for PostgreSQL so
    repositories can use a single SQL style.
    """

    def __init__(self, inner: Any, *, dialect: Literal["sqlite", "postgres"]) -> None:
        self._inner = inner
        self.dialect: Literal["sqlite", "postgres"] = dialect

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._inner.execute(self._sql(sql), params)

    def ping(self) -> bool:
        try:
            row = self.execute("SELECT 1").fetchone()
        except Exception:
            return False
        return row is not None

    def close(self) -> None:
        self._inner.close()


def connect(db_path: str) -> DbConn:
    if db_path != ":memory:":
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    raw = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    return DbConn(raw, dialect="sqlite")


def connect_postgres(
    dsn: str | None = None,
    *,
    host: str | None = None,
    port: int = 5432,
    dbname: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> DbConn:
    """Connect to PostgreSQL 15+ by DSN or discrete parameters."""
    import psycopg  # type: ignore[import-not-found, unused-ignore]
    from psycopg.rows import dict_row  # type: ignore[import-not-found, unused-ignore]

    if dsn is not None:
        raw = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    else:
        if not all((host, dbname, user, password)):
            raise ValueError("host, dbname, user and password are required without a DSN")
        raw = psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            row_factory=dict_row,
            autocommit=True,
        )
    return DbConn(raw, dialect="postgres")


def init_schema(conn: DbConn) -> None:
    with transaction(conn):
        for stmt in _SCHEMA_STMTS:
            conn.execute(stmt)
        if conn.dialect == "sqlite":
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(pending_bot_request)")}
        else:
            columns = {
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'pending_bot_request'"
                )
            }
        for name, definition in (
            ("kind", "TEXT NOT NULL DEFAULT 'create'"),
            ("target_bot_user_id", "TEXT"),
            ("remote_owner_id", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE pending_bot_request ADD COLUMN {name} {definition}")
        for table in ("bot", "pending_bot_request"):
            if conn.dialect == "sqlite":
                names = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            else:
                names = {
                    r["column_name"]
                    for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = ?",
                        (table,),
                    )
                }
            for name, definition in (
                ("git_repository", "TEXT CHECK (length(trim(git_repository)) > 0)"),
                ("criticality", "INTEGER CHECK (criticality BETWEEN 1 AND 3)"),
            ):
                if name not in names:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            _require_metadata(conn, table)
        _migrate_updates(conn)


def _migrate_updates(conn: DbConn) -> None:
    """Transactional, additive upgrade from the previous release on both databases."""
    if conn.dialect == "sqlite":
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(bot)")}
    else:
        columns = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'bot'"
            )
        }
    if "revision" not in columns:
        conn.execute("ALTER TABLE bot ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    fields = (
        "username",
        "display_name",
        "description",
        "git_repository",
        "criticality",
        "owner_user_id",
        "deleted_at",
    )
    if conn.dialect == "sqlite":
        changed = " OR ".join(f"NEW.{f} IS NOT OLD.{f}" for f in fields)
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS bot_revision AFTER UPDATE OF {', '.join(fields)} "
            f"ON bot WHEN {changed} BEGIN UPDATE bot SET revision = OLD.revision + 1 "
            "WHERE bot_user_id = NEW.bot_user_id; END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS immutable_bot_update BEFORE UPDATE OF "
            "request_id, bot_user_id, old_values, new_values ON bot_update_request "
            "BEGIN SELECT RAISE(ABORT, 'Update request payload is immutable'); END"
        )
    else:
        changed = " OR ".join(f"NEW.{f} IS DISTINCT FROM OLD.{f}" for f in fields)
        conn.execute(
            "CREATE OR REPLACE FUNCTION advance_bot_revision() RETURNS trigger AS $$ "
            f"BEGIN IF {changed} THEN NEW.revision = OLD.revision + 1; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute("DROP TRIGGER IF EXISTS bot_revision ON bot")
        conn.execute(
            f"CREATE TRIGGER bot_revision BEFORE UPDATE OF {', '.join(fields)} ON bot "
            "FOR EACH ROW EXECUTE FUNCTION advance_bot_revision()"
        )
        conn.execute(
            "CREATE OR REPLACE FUNCTION immutable_bot_update() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Update request payload is immutable'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute("DROP TRIGGER IF EXISTS immutable_bot_update ON bot_update_request")
        conn.execute(
            "CREATE TRIGGER immutable_bot_update BEFORE UPDATE OF "
            "request_id, bot_user_id, old_values, new_values ON bot_update_request "
            "FOR EACH ROW EXECUTE FUNCTION immutable_bot_update()"
        )
    conn.execute(
        "INSERT INTO schema_migration (version, applied_at) VALUES (1, ?) "
        "ON CONFLICT (version) DO NOTHING",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )


def _require_metadata(conn: DbConn, table: str) -> None:
    # Existing rows retain NULL until explicit backfill. New rows cannot omit attributes;
    # updates cannot clear them. Status-only recovery/denial of legacy requests still works.
    invalid = (
        "NEW.git_repository IS NULL OR length(trim(NEW.git_repository)) = 0 "
        "OR NEW.criticality IS NULL OR NEW.criticality NOT IN (1, 2, 3)"
    )
    if conn.dialect == "sqlite":
        invalid += " OR typeof(NEW.criticality) != 'integer'"
        for name, event in (
            ("insert", "INSERT"),
            ("update", "UPDATE OF git_repository, criticality"),
        ):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS require_{table}_metadata_{name} "
                f"BEFORE {event} ON {table} WHEN {invalid} "
                "BEGIN SELECT RAISE(ABORT, 'Required bot metadata is missing or invalid'); END"
            )
    else:
        conn.execute(
            "CREATE OR REPLACE FUNCTION require_bot_metadata() RETURNS trigger AS $$ "
            f"BEGIN IF {invalid} THEN "
            "RAISE EXCEPTION 'Required bot metadata is missing or invalid'; "
            "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        for name, event in (
            ("insert", "INSERT"),
            ("update", "UPDATE OF git_repository, criticality"),
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS require_{table}_metadata_{name} ON {table}")
            conn.execute(
                f"CREATE TRIGGER require_{table}_metadata_{name} BEFORE {event} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION require_bot_metadata()"
            )


@contextmanager
def transaction(conn: DbConn) -> Iterator[DbConn]:
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        with suppress(Exception):
            conn.execute("ROLLBACK")
        raise
