"""
Repository tests against a real PostgreSQL 15 container.

Requires Docker. The container is started once per session and shared across
all tests for speed. Each test gets a clean slate via a transaction that is
rolled back after the test.
"""

import pytest
from testcontainers.postgres import PostgresContainer

from mm_bot_manager.db import DbConn, connect_postgres, init_schema
from mm_bot_manager.repository import BotRepo, PendingRequestRepo, TokenRepo

POSTGRES_IMAGE = "postgres:15-alpine"


@pytest.fixture(scope="session")
def pg_conn() -> DbConn:  # type: ignore[return]
    with PostgresContainer(POSTGRES_IMAGE) as pg:
        # get_connection_url() returns postgresql+psycopg://... (SQLAlchemy style);
        # psycopg.connect() only accepts plain postgresql:// URIs.
        url = pg.get_connection_url()
        dsn = url.split("+")[0] + "://" + url.split("://")[1]
        conn = connect_postgres(dsn)
        init_schema(conn)
        yield conn
        conn.close()


@pytest.fixture()
def repos(pg_conn: DbConn) -> tuple[BotRepo, TokenRepo, PendingRequestRepo]:  # type: ignore[return]
    pg_conn.execute("BEGIN")
    yield BotRepo(pg_conn), TokenRepo(pg_conn), PendingRequestRepo(pg_conn)
    pg_conn.execute("ROLLBACK")


# ── BotRepo ───────────────────────────────────────────────────────────────────

def test_insert_and_fetch_bot(repos):
    bots, _, __ = repos
    b = bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name="Demo",
        description="d",
        owner_user_id="owner1",
        owner_username="alice",
    )
    assert b.bot_user_id == "u1"

    fetched = bots.get_by_owner_and_username("owner1", "demo")
    assert fetched is not None
    assert fetched.username == "demo"


def test_ownership_isolation(repos):
    bots, _, __ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="alice-id",
        owner_username="alice",
    )
    assert bots.get_by_owner_and_username("bob-id", "demo") is None
    assert bots.list_by_owner("bob-id") == []
    assert len(bots.list_by_owner("alice-id")) == 1


def test_patch_bot(repos):
    bots, _, __ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name="old",
        description="old",
        owner_user_id="o",
        owner_username="o",
    )
    bots.patch("u1", display_name="new", description="new-desc")
    fetched = bots.get_by_owner_and_username("o", "demo")
    assert fetched is not None
    assert fetched.display_name == "new"
    assert fetched.description == "new-desc"


def test_soft_delete(repos):
    bots, _, __ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="o",
        owner_username="o",
    )
    bots.mark_deleted("u1")
    assert bots.get_by_owner_and_username("o", "demo") is None


# ── TokenRepo ─────────────────────────────────────────────────────────────────

def test_token_lifecycle(repos):
    bots, tokens, _ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="o",
        owner_username="o",
    )
    t = tokens.record(token_id="tok1", bot_user_id="u1", description="ci")
    assert t.token_id == "tok1"
    assert len(tokens.list_active_for_bot("u1")) == 1

    tokens.mark_revoked("tok1")
    assert tokens.list_active_for_bot("u1") == []
    rec = tokens.get("tok1")
    assert rec is not None
    assert rec.revoked_at is not None


# ── PendingRequestRepo ────────────────────────────────────────────────────────

def test_pending_request_lifecycle(repos):
    _, __, pending = repos
    r = pending.create(
        request_id="req1",
        username="newbot",
        display_name="New Bot",
        description="desc",
        requester_user_id="user1",
        requester_username="alice",
    )
    assert r.status == "pending"

    fetched = pending.get("req1")
    assert fetched is not None
    assert fetched.username == "newbot"

    pending.resolve("req1", status="approved", resolved_by="approver1")
    resolved = pending.get("req1")
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.resolved_by == "approver1"
    assert resolved.resolved_at is not None


def test_pending_request_deny_with_reason(repos):
    _, __, pending = repos
    pending.create(
        request_id="req1",
        username="newbot",
        display_name=None,
        description=None,
        requester_user_id="user1",
        requester_username="alice",
    )
    pending.resolve("req1", status="denied", resolved_by="approver1", deny_reason="policy")
    r = pending.get("req1")
    assert r is not None
    assert r.status == "denied"
    assert r.deny_reason == "policy"


def test_get_nonexistent_returns_none(repos):
    bots, tokens, pending = repos
    assert bots.get_by_owner_and_username("nobody", "ghost") is None
    assert tokens.get("no-such-token") is None
    assert pending.get("no-such-request") is None
