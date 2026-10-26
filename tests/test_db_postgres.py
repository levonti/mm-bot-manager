"""
Repository tests against a real PostgreSQL 15 container.

Requires Docker. The container is started once per session and shared across
all tests for speed. Each test gets a clean slate via a transaction that is
rolled back after the test.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from testcontainers.postgres import PostgresContainer

from mm_bot_manager.commands import CommandContext, dispatch
from mm_bot_manager.db import DbConn, connect_postgres, init_schema
from mm_bot_manager.operations import recover_operations
from mm_bot_manager.repository import (
    BotRepo,
    OperationRepo,
    PendingRequestRepo,
    PostRepo,
    TokenRepo,
)
from mm_bot_manager.runtime import instance_lock
from tests.test_commands import FakeMM

POSTGRES_IMAGE = "postgres:15-alpine"


@pytest.fixture(scope="session")
def pg_dsn():
    if dsn := os.environ.get("MM_TEST_POSTGRES_DSN"):
        yield dsn
        return
    with PostgresContainer(POSTGRES_IMAGE) as pg:
        # get_connection_url() returns postgresql+psycopg://... (SQLAlchemy style);
        # psycopg.connect() only accepts plain postgresql:// URIs.
        url = pg.get_connection_url()
        dsn = url.split("+")[0] + "://" + url.split("://")[1]
        yield dsn


@pytest.fixture(scope="session")
def pg_conn(pg_dsn):
    conn = connect_postgres(pg_dsn)
    init_schema(conn)
    try:
        yield conn
    finally:
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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
        git_repository="https://git.example/team/project",
        criticality=2,
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


@pytest.fixture()
def isolated_pg(pg_dsn):
    """Use a fresh schema so service transactions can really commit and roll back."""
    conn = connect_postgres(pg_dsn)
    schema = f"hardening_{uuid4().hex}"
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path TO {schema}")
    init_schema(conn)
    try:
        yield conn, schema
    finally:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.close()


def parallel_connections(dsn, schema, action):
    def run(_):
        other = connect_postgres(dsn)
        try:
            other.execute(f"SET search_path TO {schema}")
            return action(other)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(run, range(2)))


def test_post_claim_is_atomic_across_postgres_connections(isolated_pg, pg_dsn):
    _, schema = isolated_pg
    results = parallel_connections(pg_dsn, schema, lambda c: PostRepo(c).claim("same-post"))
    assert sorted(results) == [False, True]


def test_approval_claim_is_atomic_across_postgres_connections(isolated_pg, pg_dsn):
    conn, schema = isolated_pg
    pending = PendingRequestRepo(conn)
    pending.create(
        request_id="req",
        username="demo-bot",
        display_name=None,
        description=None,
        requester_user_id="alice",
        requester_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    results = parallel_connections(
        pg_dsn, schema, lambda c: PendingRequestRepo(c).claim("req", "a")
    )
    assert sorted(results) == [False, True]
    assert not pending.resolve("req", status="denied", resolved_by="b")
    assert pending.get("req").status == "provisioning"


def test_postgres_instance_lock_is_exclusive_and_released(isolated_pg, pg_dsn):
    conn, _ = isolated_pg
    other = connect_postgres(pg_dsn)
    try:
        with (
            instance_lock(conn, "unused"),
            pytest.raises(RuntimeError, match="Another"),
            instance_lock(other, "unused"),
        ):
            pytest.fail("second instance acquired the lock")
        with instance_lock(other, "unused"):
            pass
    finally:
        other.close()


async def test_postgres_compensation_and_restart_recovery(isolated_pg, monkeypatch):
    conn, _ = isolated_pg
    mm = FakeMM()
    ctx = CommandContext(
        caller_user_id="alice",
        caller_username="alice",
        channel_id="dm-alice",
        bot_repo=BotRepo(conn),
        token_repo=TokenRepo(conn),
        pending_repo=PendingRequestRepo(conn),
        mm=mm,
        bot_mm=mm,
        manager_user_id="mgr",
        approver_ids=frozenset(),
        operation_repo=OperationRepo(conn),
        channel_type="D",
    )
    await dispatch(ctx, "!create demo-bot --repo https://git.example/team/project --criticality 2")
    original = ctx.token_repo.record

    def failing_record(**kwargs):
        original(**kwargs)
        raise RuntimeError("write failed before commit")

    monkeypatch.setattr(ctx.token_repo, "record", failing_record)
    mm.raise_on["revoke_user_access_token"] = RuntimeError("temporary API failure")
    await dispatch(ctx, "!token create demo-bot")
    assert ctx.token_repo.get("tok-0") is None  # The failed transaction really rolled back.
    assert ctx.operation_repo.unfinished()[0].remote_id == "tok-0"
    assert await recover_operations(ctx) == 0
    assert mm.tokens_revoked == ["tok-0"]
    assert not ctx.operation_repo.unfinished()


async def test_postgres_approval_failure_rolls_back_ownership(isolated_pg, monkeypatch):
    conn, _ = isolated_pg
    mm = FakeMM()
    ctx = CommandContext(
        caller_user_id="alice",
        caller_username="alice",
        channel_id="dm-alice",
        bot_repo=BotRepo(conn),
        token_repo=TokenRepo(conn),
        pending_repo=PendingRequestRepo(conn),
        mm=mm,
        bot_mm=mm,
        manager_user_id="mgr",
        approver_ids=frozenset({"approver"}),
        operation_repo=OperationRepo(conn),
        channel_type="D",
    )
    ctx.pending_repo.create(
        request_id="req",
        username="demo-bot",
        display_name=None,
        description=None,
        requester_user_id="alice",
        requester_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    resolve = ctx.pending_repo.resolve

    def fail_approval(*args, **kwargs):
        if kwargs["status"] == "approved":
            raise RuntimeError("write failed")
        return resolve(*args, **kwargs)

    monkeypatch.setattr(ctx.pending_repo, "resolve", fail_approval)
    await dispatch(replace(ctx, caller_user_id="approver"), "!approve req")
    assert mm.disabled == ["bot-0"]
    assert not ctx.bot_repo.list_by_owner("alice")
    assert ctx.pending_repo.get("req").status == "failed"


async def test_postgres_import_is_atomic_and_retains_audit(isolated_pg, monkeypatch):
    from mm_bot_manager.imports import approve_import
    from tests.test_imports import (
        ALICE,
        APPROVER,
        BOT,
        approver,
        import_context,
        request_import,
        table_count,
    )

    conn, _ = isolated_pg
    ctx = import_context(conn)
    request = await request_import(ctx)
    original = ctx.bot_repo.insert

    def fail_after_insert(**kwargs):
        original(**kwargs)
        raise RuntimeError("write failed")

    monkeypatch.setattr(ctx.bot_repo, "insert", fail_after_insert)
    with pytest.raises(RuntimeError, match="write failed"):
        await approve_import(approver(ctx), request)
    assert ctx.pending_repo.get(request.request_id).status == "pending"
    assert table_count(ctx, "bot") == table_count(ctx, "bot_import") == 0
    ctx.mm.assert_no_bot_mutations()
    monkeypatch.setattr(ctx.bot_repo, "insert", original)
    await approve_import(approver(ctx), request)
    assert ctx.bot_repo.get(BOT).owner_user_id == ALICE
    audit = conn.execute("SELECT * FROM bot_import").fetchone()
    assert audit["approved_by"] == APPROVER and audit["submitted_by"] == ALICE
    assert ctx.pending_repo.get(request.request_id).status == "approved"
    ctx.mm.assert_no_bot_mutations()


def test_postgres_import_schema_upgrade_preserves_legacy_data(isolated_pg):
    from tests.test_imports import assert_legacy_upgrade

    conn, _ = isolated_pg
    assert_legacy_upgrade(conn)


async def test_postgres_bulk_preview_cannot_write(isolated_pg, pg_dsn):
    from mm_bot_manager.import_bots import process_mapping
    from tests.test_imports import ALICE, BOT, import_context

    _, schema = isolated_pg
    conn = connect_postgres(pg_dsn)
    try:
        conn.execute(f'SET search_path TO "{schema}"')
        conn.execute("SET default_transaction_read_only = on")
        ctx = import_context(conn)
        plans, ids = await process_mapping(
            ctx, [(BOT, ALICE, "https://git.example/team/project", 2)], submit=False
        )
        assert len(plans) == 1 and not ids
        with pytest.raises(Exception, match="read-only"):
            conn.execute("DELETE FROM bot")
        ctx.mm.assert_no_bot_mutations()
    finally:
        conn.close()


def test_postgres_inventory_constraints_prevent_sql_bypass(isolated_pg):
    from tests.test_registry import assert_constraints

    conn, _ = isolated_pg
    assert_constraints(conn)


def test_postgres_legacy_backfill_is_atomic_and_audited(isolated_pg, monkeypatch):
    from tests.test_registry import assert_backfill

    conn, _ = isolated_pg
    assert_backfill(conn, monkeypatch)


async def test_postgres_catalog_filters_and_literal_wildcards(isolated_pg):
    from tests.test_imports import approver, import_context
    from tests.test_registry import seed

    conn, _ = isolated_pg
    ctx = import_context(conn)
    seed(ctx, criticality=1, description="SQL_100% literal")
    seed(ctx, 1, criticality=2, description="another")
    assert "1 matching bot" in await dispatch(approver(ctx), "!catalog --search 100%")
    assert "1 matching bot" in await dispatch(
        approver(ctx), "!catalog --criticality 2 --owner alice"
    )
    assert "0 matching bots" in await dispatch(approver(ctx), "!catalog --search invalid")


def test_postgres_backfill_preview_accepts_old_schema_in_read_only_session(isolated_pg, pg_dsn):
    from mm_bot_manager.metadata_migration import preview_backfill
    from tests.test_registry import backfill_rows, make_legacy

    conn, schema = isolated_pg
    make_legacy(conn)
    preview = connect_postgres(pg_dsn)
    try:
        preview.execute(f'SET search_path TO "{schema}"')
        preview.execute("SET default_transaction_read_only = on")
        assert preview_backfill(preview, backfill_rows()) == 0
        with pytest.raises(Exception, match="read-only"):
            preview.execute("UPDATE bot SET username = 'changed'")
    finally:
        preview.close()


async def test_postgres_language_and_catalog_format_survive_reconnect(isolated_pg, pg_dsn):
    from mm_bot_manager.repository import PreferenceRepo
    from tests.test_imports import ALICE, BOB, import_context
    from tests.test_registry import seed

    conn, schema = isolated_pg
    ctx = replace(import_context(conn), catalog_viewer_ids=frozenset({ALICE}))
    await dispatch(ctx, "!language ru")
    seed(ctx)
    output = await dispatch(ctx, "!catalog --format table")
    assert "\n\n|" in output
    other = connect_postgres(pg_dsn)
    try:
        other.execute(f'SET search_path TO "{schema}"')
        init_schema(other)
        assert PreferenceRepo(other).get(ALICE) == ("ru", "table")
        assert PreferenceRepo(other).get(BOB) == ("auto", "cards")
        other_ctx = replace(import_context(other), catalog_viewer_ids=frozenset({ALICE}))
        assert "\n\n|" in await dispatch(other_ctx, "!catalog")
    finally:
        other.close()


def test_preference_upgrade_preserves_bots_and_rejects_invalid_codes(isolated_pg):
    from mm_bot_manager.db import transaction
    from mm_bot_manager.repository import PreferenceRepo
    from tests.test_imports import import_context
    from tests.test_registry import seed

    conn, _ = isolated_pg
    ctx = import_context(conn)
    bot = seed(ctx)
    conn.execute("DROP TABLE user_preference")
    init_schema(conn)
    assert ctx.bot_repo.get(bot.bot_user_id) == bot
    assert PreferenceRepo(conn).get("owner") == ("auto", "cards")
    with pytest.raises(Exception, match="constraint"), transaction(conn):
        conn.execute(
            "INSERT INTO user_preference (user_id, language) VALUES ('owner', 'unsupported')"
        )


@pytest.fixture()
def previous_pg(pg_dsn):
    """A previous-version database, created without the new initializer."""
    conn = connect_postgres(pg_dsn)
    schema = f"upgrade_{uuid4().hex}"
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path TO {schema}")
    try:
        yield conn, schema
    finally:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.close()


async def test_postgres_previous_release_auto_upgrade_and_update(previous_pg):
    from mm_bot_manager.repository import PreferenceRepo
    from tests.test_imports import ALICE, BOT, approver
    from tests.test_updates import assert_upgrade_preserves_data, propose

    conn, _ = previous_pg
    ctx = assert_upgrade_preserves_data(conn)
    PreferenceRepo(conn).language(ALICE, "en")
    req = await propose(ctx, "--criticality 1")
    assert "Approved" in await dispatch(approver(ctx), f"!approve {req.request_id}")
    assert ctx.bot_repo.get(BOT).criticality == 1
    assert ctx.token_repo.get("keep-token").revoked_at is None
    assert ctx.pending_repo.get("old-pending").status == "pending"


def test_postgres_auto_upgrade_failure_rolls_back_and_can_retry(previous_pg, monkeypatch):
    from tests.test_imports import BOT
    from tests.test_updates import install_previous_schema, seed_previous_version

    conn, _ = previous_pg
    install_previous_schema(conn)
    seed_previous_version(conn)
    execute = conn.execute

    def fail(sql, params=()):
        if "CREATE OR REPLACE FUNCTION advance_bot_revision" in sql:
            raise RuntimeError("migration failure")
        return execute(sql, params)

    monkeypatch.setattr(conn, "execute", fail)
    with pytest.raises(RuntimeError, match="migration failure"):
        init_schema(conn)
    columns = {
        row["column_name"]
        for row in execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'bot'"
        )
    }
    assert "revision" not in columns
    assert execute("SELECT to_regclass('bot_update_request') AS name").fetchone()["name"] is None
    assert TokenRepo(conn).get("keep-token").revoked_at is None
    monkeypatch.setattr(conn, "execute", execute)
    init_schema(conn)
    assert BotRepo(conn).get(BOT).revision == 0


async def test_postgres_uncertain_rename_survives_reconnect(isolated_pg, pg_dsn, monkeypatch):
    from mm_bot_manager.updates import UpdateRepo
    from tests.test_imports import BOT, approver, import_context
    from tests.test_updates import UpdateMM, propose, seed_bot

    conn, schema = isolated_pg
    mm = UpdateMM()
    ctx = import_context(conn, mm)
    seed_bot(ctx)
    req = await propose(ctx, "--username renamed --criticality 1")
    patch = mm.patch_user

    async def timeout(*args, **kwargs):
        await patch(*args, **kwargs)
        raise TimeoutError("private")

    monkeypatch.setattr(mm, "patch_user", timeout)
    assert "needs review" in await dispatch(approver(ctx), f"!approve {req.request_id}")
    other = connect_postgres(pg_dsn)
    try:
        other.execute(f'SET search_path TO "{schema}"')
        init_schema(other)
        reopened = import_context(other, mm)
        assert await recover_operations(reopened) == 0
        bot = reopened.bot_repo.get(BOT)
        assert bot.username == "renamed" and bot.criticality == 1
        assert UpdateRepo(other).active(BOT) is None
        assert len(mm.patched) == 1
        with pytest.raises(Exception, match="immutable"):
            other.execute(
                "UPDATE bot_update_request SET old_values = '{}' WHERE request_id = ?",
                (req.request_id,),
            )
    finally:
        other.close()


async def test_postgres_revision_invalidates_aba_proposal(isolated_pg):
    from tests.test_imports import BOT, approver, import_context
    from tests.test_updates import UpdateMM, propose, seed_bot

    conn, _ = isolated_pg
    ctx = import_context(conn, UpdateMM())
    seed_bot(ctx)
    req = await propose(ctx)
    ctx.bot_repo.patch(BOT, description="different")
    ctx.bot_repo.patch(BOT, description="Existing integration")
    assert ctx.bot_repo.get(BOT).revision == 2
    assert "stale" in await dispatch(approver(ctx), f"!approve {req.request_id}")
    ctx.mm.assert_no_bot_mutations()


def test_postgres_only_one_update_proposal_across_connections(isolated_pg, pg_dsn):
    import asyncio

    from mm_bot_manager.updates import UpdateError, submit_update
    from tests.test_imports import BOT, import_context
    from tests.test_updates import UpdateMM, seed_bot

    conn, schema = isolated_pg
    seed_bot(import_context(conn, UpdateMM()))

    def submit(other):
        ctx = import_context(other, UpdateMM())
        try:
            asyncio.run(submit_update(ctx, ctx.bot_repo.get(BOT), {"criticality": 1}))
        except UpdateError as exc:
            assert exc.key == "updates.busy"
            return False
        return True

    assert sorted(parallel_connections(pg_dsn, schema, submit)) == [False, True]
    assert conn.execute("SELECT COUNT(*) AS n FROM bot_update_request").fetchone()["n"] == 1


async def test_feedback_postgres_lifecycle_and_persistent_outbox(isolated_pg):
    from mm_bot_manager.feedback import FeedbackRepo
    from mm_bot_manager.feedback_delivery import deliver_once
    from tests.test_feedback import feedback_context

    conn, _ = isolated_pg
    ctx = feedback_context(conn)
    repo = FeedbackRepo(conn)
    steps = [
        ("alice", '!feedback new Unclosed "quote\n--literal'),
        ("support", "!feedback take FB-1"),
        ("support", "!feedback set-status FB-1 waiting Details?"),
        ("alice", "!feedback reply FB-1 More context"),
        ("support", "!feedback set-status FB-1 resolved Fixed"),
        ("alice", "!feedback reopen FB-1 Reproduced"),
    ]
    for index, (actor, text) in enumerate(steps):
        result = await dispatch(
            replace(ctx, caller_user_id=actor, caller_username=actor, post_id=f"feedback-{index}"),
            text,
        )
        assert "FB-1" in result
    assert repo.get(1).status == "new"
    assert repo.get(1).assignee_id is None
    assert repo.get(1).version == 6
    init_schema(conn)
    assert len(repo.history(1, 0, 100)) == 6
    while repo.due():
        await deliver_once(ctx)
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM feedback_outbox WHERE state = 'pending'"
        ).fetchone()["n"]
        == 0
    )
    assert ctx.bot_mm.posts


def test_feedback_concurrent_assignment_has_one_winner(isolated_pg, pg_dsn):
    from threading import Barrier

    from mm_bot_manager.feedback import FeedbackRepo
    from mm_bot_manager.i18n import UserFacingError
    from tests.test_feedback import HANDLERS

    conn, schema = isolated_pg
    ticket = FeedbackRepo(conn).create("alice", "Issue", "source-new", HANDLERS)
    barrier = Barrier(2)

    def take(actor):
        other = connect_postgres(pg_dsn)
        try:
            other.execute(f"SET search_path TO {schema}")
            repo = FeedbackRepo(other)
            snapshot = repo.get(ticket.number)
            barrier.wait(timeout=10)
            try:
                repo.change(snapshot, "take", actor, "", "take-" + actor, HANDLERS)
            except UserFacingError as exc:
                assert exc.key == "feedback.conflict"
                return False
            return True
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, ["support", "backup"]))
    assert sorted(results) == [False, True]
    assert FeedbackRepo(conn).get(ticket.number).version == 2
    assert len(FeedbackRepo(conn).history(ticket.number, 0, 100)) == 2


def test_feedback_postgres_rollback_and_idempotent_replay(isolated_pg, monkeypatch):
    from mm_bot_manager.feedback import FeedbackRepo
    from tests.test_feedback import HANDLERS

    conn, _ = isolated_pg
    repo = FeedbackRepo(conn)
    ticket = repo.create("alice", "Issue", "post-1", HANDLERS)
    assert repo.create("alice", "Issue", "post-1", HANDLERS) == ticket
    original = FeedbackRepo._record

    def fail(self, *args):
        original(self, *args)
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(FeedbackRepo, "_record", fail)
    with pytest.raises(RuntimeError):
        repo.change(ticket, "reply", "alice", "Must roll back", "post-2", HANDLERS)
    assert repo.get(ticket.number) == ticket
    assert len(repo.history(ticket.number, 0, 100)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM feedback_outbox").fetchone()["n"] == 2
