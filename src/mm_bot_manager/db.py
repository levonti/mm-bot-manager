import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, Literal

SCHEMA_SQL = """
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
    deny_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_status ON pending_bot_request(status);

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
    if conn.dialect == "sqlite":
        conn._inner.executescript(SCHEMA_SQL)
    else:
        conn.execute("BEGIN")
        try:
            for stmt in _SCHEMA_STMTS:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
