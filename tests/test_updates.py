"""Approval boundaries, durable update recovery and upgrade acceptance tests."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from mm_bot_manager.commands import dispatch
from mm_bot_manager.config import Settings
from mm_bot_manager.dates import format_datetime
from mm_bot_manager.db import connect, init_schema, transaction
from mm_bot_manager.i18n import catalog
from mm_bot_manager.mm_client import MattermostError
from mm_bot_manager.operations import recover_operations
from mm_bot_manager.repository import (
    BotRepo,
    OperationRepo,
    PendingRequestRepo,
    PostRepo,
    PreferenceRepo,
    TokenRepo,
)
from mm_bot_manager.updates import UpdateRepo, snapshot
from tests.test_imports import ALICE, APPROVER, BOB, BOT, ImportMM, approver, import_context

REPO = "https://git.example/team/project"
PREVIOUS_SCHEMA = Path(__file__).parent / "fixtures" / "previous_schema.sql"


class UpdateMM(ImportMM):
    async def patch_user(self, user_id, *, username):
        await super().patch_user(user_id, username=username)
        self.users[user_id]["username"] = username
        self.bots[user_id]["username"] = username

    async def patch_bot(self, bot_user_id, **changes):
        await super().patch_bot(bot_user_id, **changes)
        self.bots[bot_user_id].update(changes)


def seed_bot(ctx):
    remote = ctx.mm.bots[BOT]
    return ctx.bot_repo.insert(
        bot_user_id=BOT,
        username=remote["username"],
        display_name=remote["display_name"],
        description=remote["description"],
        owner_user_id=ALICE,
        owner_username="alice",
        git_repository=REPO,
        criticality=2,
    )


@pytest.fixture()
def env():
    conn = connect(":memory:")
    init_schema(conn)
    ctx = import_context(conn, UpdateMM())
    seed_bot(ctx)
    yield ctx
    conn.close()


def pending(ctx):
    row = ctx.bot_repo.conn.execute(
        "SELECT request_id FROM pending_bot_request WHERE kind = 'update' AND status = 'pending'"
    ).fetchone()
    assert row is not None
    return ctx.pending_repo.get(row["request_id"])


async def propose(ctx, flags="--username renamed"):
    reply = await dispatch(ctx, "!update legacy " + flags)
    assert "mandatory approval" in reply
    return pending(ctx)


@pytest.mark.parametrize(
    "flags,field,value",
    [
        ("--username renamed", "username", "renamed"),
        ("--repo https://git.example/team/new", "git_repository", "https://git.example/team/new"),
        ("--criticality 1", "criticality", 1),
    ],
)
async def test_key_fields_wait_for_approval_and_preserve_identity_and_tokens(
    env, flags, field, value
):
    before = env.bot_repo.get(BOT)
    env.token_repo.record(token_id="existing-token", bot_user_id=BOT, description="keep")
    req = await propose(env, flags)
    assert env.bot_repo.get(BOT) == before
    env.mm.assert_no_bot_mutations()
    assert req.target_bot_user_id == BOT and req.kind == "update"
    assert "Approved update" in await dispatch(approver(env), f"!approve {req.request_id}")
    after = env.bot_repo.get(BOT)
    assert getattr(after, field) == value and after.owner_user_id == ALICE
    assert env.token_repo.list_active_for_bot(BOT)[0].token_id == "existing-token"
    assert not env.mm.disabled and not env.mm.tokens_revoked
    assert UpdateRepo(env.bot_repo.conn).active(BOT) is None
    calls = len(env.mm.patched)
    assert "already" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert len(env.mm.patched) == calls


async def test_mixed_command_waits_in_full_and_audits_exact_proposal(env):
    before = snapshot(env.bot_repo.get(BOT))
    req = await propose(
        env,
        '--username renamed --description "New purpose" '
        '--display-name "New title" --repo https://git.example/team/new --criticality 1',
    )
    assert snapshot(env.bot_repo.get(BOT)) == before
    assert "Existing integration" in env.mm.posts[0]["message"]
    assert "New purpose" in env.mm.posts[0]["message"]
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")
    record = UpdateRepo(env.bot_repo.conn).get(req.request_id)
    assert record.old == before
    bot = env.bot_repo.get(BOT)
    assert (bot.username, bot.description, bot.display_name, bot.criticality) == (
        "renamed",
        "New purpose",
        "New title",
        1,
    )
    assert len(env.mm.patched) == 2
    audit = env.bot_repo.conn.execute("SELECT * FROM metadata_audit").fetchone()
    assert audit["changed_by"] == APPROVER and audit["source"] == f"approval:{req.request_id}"
    resolved = env.pending_repo.get(req.request_id)
    assert resolved.resolved_by == APPROVER and resolved.resolved_at
    assert env.mm.posts[-1]["channel_id"].endswith(ALICE)


@pytest.mark.parametrize("flags", ["--username renamed", "--criticality 1", "--repo " + REPO])
async def test_empty_approvers_block_key_updates(env, flags):
    ctx = replace(env, approver_ids=frozenset())
    assert "MM_APPROVERS" in await dispatch(ctx, "!update legacy " + flags)
    assert env.bot_repo.conn.execute("SELECT COUNT(*) FROM bot_update_request").fetchone()[0] == 0
    env.mm.assert_no_bot_mutations()


async def test_cosmetic_update_is_immediate_without_approvers(env):
    reply = await dispatch(
        replace(env, approver_ids=frozenset()), '!update legacy --description "New"'
    )
    assert "Updated bot" in reply
    assert env.bot_repo.get(BOT).description == "New"
    assert env.mm.bots[BOT]["description"] == "New"


@pytest.mark.parametrize("channel", [None, "", "O", "P", "G"])
async def test_update_submission_and_approval_are_dm_only(env, channel):
    assert "direct message" in await dispatch(
        replace(env, channel_type=channel), "!update legacy --criticality 1"
    )
    req = await propose(env)
    assert "direct message" in await dispatch(
        replace(approver(env), channel_type=channel), f"!approve {req.request_id}"
    )
    assert env.pending_repo.get(req.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize("kind", ["create", "import", "update"])
async def test_self_approval_is_forbidden_for_every_request_kind(env, kind):
    owner = replace(env, approver_ids=frozenset({ALICE, APPROVER}))
    if kind == "update":
        req = await propose(owner)
    else:
        req = env.pending_repo.create(
            request_id=kind,
            username="another-bot",
            display_name=None,
            description=None,
            requester_user_id=ALICE,
            requester_username="alice",
            git_repository=REPO,
            criticality=2,
            kind=kind,
        )
    assert "Self-approval" in await dispatch(owner, f"!approve {req.request_id}")
    assert env.pending_repo.get(req.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()


async def test_import_submitter_cannot_approve_on_behalf_of_another_owner(env):
    req = env.pending_repo.create(
        request_id="bulk",
        username="other",
        display_name=None,
        description=None,
        requester_user_id=BOB,
        requester_username="bob",
        git_repository=REPO,
        criticality=1,
        kind="import",
    )
    env.bot_repo.conn.execute(
        "INSERT INTO import_submission (request_id, submitted_by) VALUES (?, ?)",
        (req.request_id, APPROVER),
    )
    assert "Self-approval" in await dispatch(approver(env), "!approve bulk")
    env.mm.assert_no_bot_mutations()


async def test_approver_proposal_for_another_owner_needs_another_reviewer(env):
    proposer = approver(env)
    assert "not owned" in await dispatch(proposer, "!update legacy --username renamed")
    req = await propose(proposer, "--criticality 1")
    assert "Self-approval" in await dispatch(proposer, f"!approve {req.request_id}")
    reviewer = replace(proposer, caller_user_id=BOB, approver_ids=frozenset({APPROVER, BOB}))
    assert "Approved" in await dispatch(reviewer, f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).criticality == 1


async def test_removed_requester_permission_invalidates_proposal(env):
    req = await propose(approver(env), "--criticality 1")
    reviewer = replace(env, caller_user_id=BOB, approver_ids=frozenset({BOB}))
    assert "stale" in await dispatch(reviewer, f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).criticality == 2
    assert env.pending_repo.get(req.request_id).status == "stale"


async def test_denial_does_not_change_bot_and_releases_proposal_slot(env):
    req = await propose(env)
    before = env.bot_repo.get(BOT)
    assert "Denied" in await dispatch(approver(env), f"!deny {req.request_id} wrong name")
    assert env.bot_repo.get(BOT) == before
    assert UpdateRepo(env.bot_repo.conn).active(BOT) is None
    assert "wrong name" in env.mm.posts[-1]["message"]
    assert "already" in await dispatch(approver(env), f"!approve {req.request_id}")
    await propose(env)
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize("change", ["profile", "aba", "owner", "delete"])
async def test_stale_local_snapshot_cannot_be_applied(env, change):
    req = await propose(env)
    if change in {"profile", "aba"}:
        env.bot_repo.patch(BOT, description="changed")
        if change == "aba":
            env.bot_repo.patch(BOT, description="Existing integration")
    elif change == "owner":
        env.bot_repo.conn.execute(
            "UPDATE bot SET owner_user_id = ? WHERE bot_user_id = ?", (BOB, BOT)
        )
    else:
        env.bot_repo.mark_deleted(BOT)
    assert "stale" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.pending_repo.get(req.request_id).status == "stale"
    assert UpdateRepo(env.bot_repo.conn).active(BOT) is None
    env.mm.assert_no_bot_mutations()


async def test_duplicate_proposals_are_blocked_and_payload_is_immutable(env):
    req = await propose(env)
    assert req.request_id in await dispatch(env, "!update legacy --criticality 1")
    with pytest.raises(Exception, match="immutable"), transaction(env.bot_repo.conn):
        env.bot_repo.conn.execute(
            "UPDATE bot_update_request SET new_values = '{}' WHERE request_id = ?",
            (req.request_id,),
        )
    assert len(env.mm.posts) == 1
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize(
    "flags", ["--username", '--username "two words"', "--criticality 4", "--repo", "--unexpected x"]
)
async def test_invalid_update_never_creates_proposal(env, flags):
    await dispatch(env, "!update legacy " + flags)
    assert UpdateRepo(env.bot_repo.conn).active(BOT) is None
    env.mm.assert_no_bot_mutations()


async def test_remote_drift_before_approval_never_gets_overwritten(env):
    req = await propose(env)
    env.mm.users[BOT]["username"] = "changed-externally"
    assert "differs" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.pending_repo.get(req.request_id).status == "stale"
    env.mm.assert_no_bot_mutations()


async def test_concurrent_approval_denial_update_and_delete_are_excluded(env, monkeypatch):
    req = await propose(env)
    started, release = asyncio.Event(), asyncio.Event()
    patch = env.mm.patch_user

    async def paused(*args, **kwargs):
        started.set()
        await release.wait()
        return await patch(*args, **kwargs)

    monkeypatch.setattr(env.mm, "patch_user", paused)
    first = asyncio.create_task(dispatch(approver(env), f"!approve {req.request_id}"))
    await started.wait()
    try:
        assert "already" in await dispatch(approver(env), f"!approve {req.request_id}")
        assert "already" in await dispatch(approver(env), f"!deny {req.request_id}")
        assert "already" in await dispatch(env, "!update legacy --description hi")
        assert "already" in await dispatch(env, "!delete legacy")
    finally:
        release.set()
    assert "Approved" in await first
    assert len(env.mm.patched) == 1 and not env.mm.disabled


async def test_applied_remote_write_then_timeout_recovers_without_duplicate_write(env, monkeypatch):
    req = await propose(env)
    patch = env.mm.patch_user

    async def timeout(*args, **kwargs):
        await patch(*args, **kwargs)
        raise TimeoutError("transport-secret")

    monkeypatch.setattr(env.mm, "patch_user", timeout)
    reply = await dispatch(approver(env), f"!approve {req.request_id}")
    assert "needs review" in reply and "transport-secret" not in reply
    assert env.pending_repo.get(req.request_id).status == "needs_review"
    assert env.bot_repo.get(BOT).username == "legacy"
    assert await recover_operations(env) == 0
    assert env.bot_repo.get(BOT).username == "renamed"
    assert env.pending_repo.get(req.request_id).status == "approved"
    assert len(env.mm.patched) == 1


async def test_timeout_without_remote_change_requires_fresh_approval(env):
    req = await propose(env)
    env.mm.raise_on["patch_user"] = TimeoutError("private")
    assert "needs review" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert await recover_operations(env) == 0
    reopened = env.pending_repo.get(req.request_id)
    assert reopened.status == "pending" and reopened.resolved_by is None
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")


async def test_partial_remote_update_recovers_only_remaining_fields(env):
    req = await propose(env, '--username renamed --description "new purpose" --criticality 1')
    env.mm.raise_on["patch_user"] = MattermostError(503, "private")
    assert "needs review" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.mm.bots[BOT]["description"] == "new purpose"
    assert env.bot_repo.get(BOT).description == "Existing integration"
    assert await recover_operations(env) == 0
    assert len(env.mm.patched) == 2
    assert env.bot_repo.get(BOT).username == "renamed"
    assert env.bot_repo.get(BOT).criticality == 1
    assert env.pending_repo.get(req.request_id).resolved_by == APPROVER


async def test_unknown_remote_state_stays_locked_until_operator_reconciles(env):
    req = await propose(env)
    env.mm.raise_on["patch_user"] = TimeoutError()
    await dispatch(approver(env), f"!approve {req.request_id}")
    env.mm.users[BOT]["username"] = "unknown-third-state"
    assert await recover_operations(env) == 1
    assert env.pending_repo.get(req.request_id).status == "needs_review"
    assert req.request_id in await dispatch(env, "!update legacy --description hi")
    assert req.request_id in await dispatch(env, "!delete legacy")
    assert not env.mm.patched and not env.mm.disabled
    env.mm.users[BOT]["username"] = "legacy"
    assert await recover_operations(env) == 0
    assert "Denied" in await dispatch(approver(env), f"!deny {req.request_id}")


async def test_failed_local_finalization_rolls_back_then_recovers(env):
    req = await propose(env, "--username renamed --criticality 1")
    conn = env.bot_repo.conn
    conn.execute(
        "CREATE TRIGGER fail_update BEFORE UPDATE OF username ON bot "
        "BEGIN SELECT RAISE(ABORT, 'private-write-error'); END"
    )
    assert "needs review" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).criticality == 2
    assert conn.execute("SELECT COUNT(*) FROM metadata_audit").fetchone()[0] == 0
    assert env.pending_repo.get(req.request_id).status == "needs_review"
    conn.execute("DROP TRIGGER fail_update")
    assert await recover_operations(env) == 0
    assert env.bot_repo.get(BOT).criticality == 1
    assert conn.execute("SELECT COUNT(*) FROM metadata_audit").fetchone()[0] == 1
    assert len(env.mm.patched) == 1


async def test_cancelled_update_leaves_durable_intent_and_releases_worker_lock(env, monkeypatch):
    req = await propose(env)
    started = asyncio.Event()

    async def interrupted(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(env.mm, "patch_user", interrupted)
    task = asyncio.create_task(dispatch(approver(env), f"!approve {req.request_id}"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.pending_repo.get(req.request_id).status == "needs_review"
    assert await recover_operations(env) == 0
    assert "Denied" in await dispatch(approver(env), f"!deny {req.request_id}")


@pytest.mark.parametrize("reviewer", [ALICE, BOB])
async def test_recovery_rejects_self_or_removed_approver(env, reviewer):
    req = await propose(env, "--criticality 1")
    assert env.pending_repo.claim(req.request_id, reviewer)
    assert await recover_operations(env) == 1
    assert env.bot_repo.get(BOT).criticality == 2
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("role", ["user", "catalog", "approver"])
@pytest.mark.parametrize("channel", ["D", "O", "P", "G", None])
async def test_help_visibility_matches_role_and_channel(env, language, role, channel):
    ctx = replace(
        env,
        channel_type=channel,
        approver_ids=frozenset({ALICE}) if role == "approver" else env.approver_ids,
        catalog_viewer_ids=frozenset({ALICE}) if role == "catalog" else frozenset(),
    )
    PreferenceRepo(ctx.bot_repo.conn).language(ALICE, language)
    for command in ("!help", "!", ""):
        help_text = await dispatch(ctx, command)
        assert ("!approve" in help_text) == (role == "approver" and channel == "D")
        assert ("!deny" in help_text) == (role == "approver" and channel == "D")
        assert ("!catalog" in help_text) == (role in {"approver", "catalog"} and channel == "D")
        assert "!update" in help_text and "!language" in help_text


@pytest.mark.parametrize(
    "language,expected", [("ru", "22.10.2026 14:35:08"), ("en", "2026-10-22 14:35:08")]
)
async def test_info_formats_registration_and_import_times(env, language, expected):
    conn = env.bot_repo.conn
    PreferenceRepo(conn).language(ALICE, language)
    date = "2026-10-22T11:35:08+00:00"
    conn.execute("UPDATE bot SET created_at = ? WHERE bot_user_id = ?", (date, BOT))
    req = env.pending_repo.create(
        request_id="imported",
        username="legacy",
        display_name=None,
        description=None,
        requester_user_id=ALICE,
        requester_username="alice",
        git_repository=REPO,
        criticality=2,
        kind="import",
    )
    conn.execute(
        "INSERT INTO bot_import (bot_user_id, request_id, imported_at, submitted_by, "
        "approved_by, remote_owner_id) VALUES (?, ?, ?, ?, ?, ?)",
        (BOT, req.request_id, date, ALICE, APPROVER, BOB),
    )
    text = await dispatch(replace(env, display_timezone="Europe/Moscow"), "!info legacy")
    assert text.count(expected + " (UTC+03:00)") == 2
    assert date not in text


@pytest.mark.parametrize("value", [None, "", "old", "2026-10-22T11:35:08"])
def test_missing_or_ambiguous_dates_have_localized_fallback(value):
    for language in ("ru", "en"):
        assert format_datetime(value, language) == catalog(language)["date.unknown"]


@pytest.mark.parametrize(
    "value,offset,time",
    [
        ("2026-03-29T00:30:00Z", "UTC+01:00", "01:30:00"),
        ("2026-03-29T01:30:00Z", "UTC+02:00", "03:30:00"),
        ("2026-10-22T11:35:08Z", "UTC+05:30", "17:05:08"),
    ],
)
def test_timezone_conversion_handles_dst_and_fractional_offsets(value, offset, time):
    zone = "Asia/Kolkata" if offset == "UTC+05:30" else "Europe/Berlin"
    text = format_datetime(value, "en", zone)
    assert offset in text and time in text


@pytest.mark.parametrize("zone", ["", "../Europe/Moscow", "Unknown/Timezone"])
def test_invalid_timezone_configuration_fails_before_startup(zone):
    with pytest.raises(ValueError, match="MM_DISPLAY_TIMEZONE"):
        Settings(
            _env_file=None,
            mm_url="https://mm.example",
            mm_token="test",
            mm_bot_token="test",
            mm_display_timezone=zone,
        )


def install_previous_schema(conn):
    # Real previous schema, rather than building it with the new release's initializer.
    sql = PREVIOUS_SCHEMA.read_text()
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def seed_previous_version(conn):
    ctx = import_context(conn, UpdateMM())
    # BotRepo from the new version only inserts old columns; no new schema is involved.
    seed_bot(ctx)
    ctx.token_repo.record(token_id="keep-token", bot_user_id=BOT, description="keep")
    ctx.pending_repo.create(
        request_id="old-pending",
        username="new-bot",
        display_name=None,
        description=None,
        requester_user_id=ALICE,
        requester_username="alice",
        git_repository=REPO,
        criticality=1,
    )
    ctx.pending_repo.create(
        request_id="old-import",
        username="legacy",
        display_name=None,
        description=None,
        requester_user_id=ALICE,
        requester_username="alice",
        git_repository=REPO,
        criticality=2,
        kind="import",
        target_bot_user_id=BOT,
    )
    ctx.pending_repo.resolve("old-import", status="approved", resolved_by=APPROVER)
    conn.execute(
        "INSERT INTO import_submission (request_id, submitted_by) VALUES (?, ?)",
        ("old-import", ALICE),
    )
    conn.execute(
        "INSERT INTO bot_import (bot_user_id, request_id, imported_at, submitted_by, "
        "approved_by, remote_owner_id) VALUES (?, ?, ?, ?, ?, ?)",
        (BOT, "old-import", "2026-10-20T12:00:00+00:00", ALICE, APPROVER, BOB),
    )
    conn.execute(
        "INSERT INTO metadata_audit (audit_id, record_type, record_id, old_git_repository, "
        "old_criticality, git_repository, criticality, changed_by, source, changed_at) "
        "VALUES ('old-audit', 'bot', ?, ?, 3, ?, 2, ?, 'chat', '2026-10-21T12:00:00+00:00')",
        (BOT, REPO, REPO, ALICE),
    )
    operation = OperationRepo(conn).start("token_create", BOT)
    OperationRepo(conn).update(operation, "completed", "keep-token")
    PostRepo(conn).claim("old-post")
    PostRepo(conn).finish("old-post")
    PreferenceRepo(conn).language(ALICE, "ru")
    return ctx


def assert_upgrade_preserves_data(conn):
    install_previous_schema(conn)
    seed_previous_version(conn)
    tables = (
        "bot",
        "token",
        "pending_bot_request",
        "user_preference",
        "metadata_audit",
        "bot_import",
        "import_submission",
        "operation",
        "processed_post",
    )
    before = {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables
    }
    init_schema(conn)
    init_schema(conn)
    for table in tables:
        after = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        if table == "bot":
            assert after[0].pop("revision") == 0
        assert after == before[table]
    assert conn.execute("SELECT COUNT(*) AS n FROM schema_migration").fetchone()["n"] == 1
    ctx = import_context(conn, UpdateMM())
    assert ctx.bot_repo.get(BOT).revision == 0
    return ctx


async def test_previous_sqlite_version_migrates_automatically_and_reopens(tmp_path):
    path = str(tmp_path / "previous.sqlite")
    conn = connect(path)
    assert_upgrade_preserves_data(conn)
    conn.close()
    conn = connect(path)
    try:
        init_schema(conn)
        ctx = import_context(conn, UpdateMM())
        PreferenceRepo(conn).language(ALICE, "en")
        req = await propose(ctx, "--criticality 1")
        assert "Approved" in await dispatch(approver(ctx), f"!approve {req.request_id}")
        assert ctx.token_repo.get("keep-token").revoked_at is None
        assert ctx.pending_repo.get("old-pending").status == "pending"
    finally:
        conn.close()


def test_failed_auto_migration_rolls_back_without_partial_schema_or_data_loss(
    tmp_path, monkeypatch
):
    conn = connect(str(tmp_path / "previous.sqlite"))
    install_previous_schema(conn)
    seed_previous_version(conn)
    execute = conn.execute

    def fail(sql, params=()):
        if "CREATE TRIGGER IF NOT EXISTS bot_revision" in sql:
            raise RuntimeError("simulated migration failure")
        return execute(sql, params)

    monkeypatch.setattr(conn, "execute", fail)
    try:
        with pytest.raises(RuntimeError, match="migration failure"):
            init_schema(conn)
        assert "revision" not in {row["name"] for row in execute("PRAGMA table_info(bot)")}
        assert not execute(
            "SELECT name FROM sqlite_master WHERE name = 'bot_update_request'"
        ).fetchall()
        assert TokenRepo(conn).get("keep-token").revoked_at is None
        assert PendingRequestRepo(conn).get("old-pending").status == "pending"
        monkeypatch.setattr(conn, "execute", execute)
        init_schema(conn)
        assert BotRepo(conn).get(BOT).username == "legacy"
    finally:
        conn.close()


@pytest.mark.parametrize("user_id", [ALICE, APPROVER])
@pytest.mark.parametrize("field", ["delete_at", "is_bot"])
async def test_inactive_or_bot_requester_and_reviewer_invalidate_update(env, user_id, field):
    req = await propose(env, "--criticality 1")
    env.mm.users[user_id][field] = True
    assert "stale" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.pending_repo.get(req.request_id).status == "stale"
    assert env.bot_repo.get(BOT).criticality == 2
    env.mm.assert_no_bot_mutations()


async def test_account_lookup_failure_leaves_update_pending_without_mutation(env):
    req = await propose(env)
    env.mm.raise_on["get_user"] = MattermostError(503, "private")
    text = await dispatch(approver(env), f"!approve {req.request_id}")
    assert "remains pending" in text and "private" not in text
    assert env.pending_repo.get(req.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()


async def test_missing_journal_fails_closed(env):
    req = await propose(env)
    env.bot_repo.conn.execute(
        "DELETE FROM bot_update_request WHERE request_id = ?", (req.request_id,)
    )
    assert "journal is missing" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.pending_repo.get(req.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()


async def test_uncertain_update_recovers_from_reopened_sqlite_database(tmp_path, monkeypatch):
    path = str(tmp_path / "updates.sqlite")
    conn = connect(path)
    init_schema(conn)
    mm = UpdateMM()
    ctx = import_context(conn, mm)
    seed_bot(ctx)
    req = await propose(ctx, "--username renamed --criticality 1")
    patch = mm.patch_user

    async def timeout(*args, **kwargs):
        await patch(*args, **kwargs)
        raise TimeoutError()

    monkeypatch.setattr(mm, "patch_user", timeout)
    assert "needs review" in await dispatch(approver(ctx), f"!approve {req.request_id}")
    conn.close()
    reopened = connect(path)
    try:
        init_schema(reopened)
        recovered = import_context(reopened, mm)
        assert await recover_operations(recovered) == 0
        assert recovered.bot_repo.get(BOT).username == "renamed"
        assert recovered.bot_repo.get(BOT).criticality == 1
        assert recovered.pending_repo.get(req.request_id).resolved_by == APPROVER
        assert len(mm.patched) == 1
    finally:
        reopened.close()


async def test_review_preserves_whitespace_and_rejects_truncation(env):
    req = await propose(env, '--username renamed --description "line  one\nline two"')
    message = env.mm.posts[0]["message"]
    assert "line  one" in message and "line two" in message and "\\\\n" in message
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).description == "line  one\nline two"
    reply = await dispatch(
        env, '!update renamed --criticality 1 --description "' + "x" * 13000 + '"'
    )
    assert "too large to review" in reply
    assert UpdateRepo(env.bot_repo.conn).active(BOT) is None
    assert env.bot_repo.get(BOT).criticality == 2


async def test_notification_failure_preserves_durable_proposal(env):
    env.mm.raise_on["create_post"] = RuntimeError("private")
    req = await propose(env, "--criticality 1")
    assert req.status == "pending"
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).criticality == 1


async def test_approved_update_uses_real_http_client_and_immutable_bot_paths(env):
    import json

    import httpx
    import respx

    from mm_bot_manager.mm_client import MattermostClient

    users = {key: value.copy() for key, value in env.mm.users.items()}
    bot = env.mm.bots[BOT].copy()
    with respx.mock() as routes:
        for user_id in (ALICE, APPROVER, BOT):
            routes.get(f"https://mm.example/api/v4/users/{user_id}").mock(
                side_effect=lambda request, user_id=user_id: httpx.Response(
                    200, json=users[user_id]
                )
            )
        routes.get(f"https://mm.example/api/v4/bots/{BOT}").mock(
            side_effect=lambda request: httpx.Response(200, json=bot)
        )

        def patch_user(request):
            payload = json.loads(request.content)
            assert payload == {"username": "renamed"}
            users[BOT].update(payload)
            return httpx.Response(200, json=users[BOT])

        def patch_bot(request):
            payload = json.loads(request.content)
            assert payload == {"description": "New purpose"}
            bot.update(payload)
            return httpx.Response(200, json=bot)

        rename = routes.put(f"https://mm.example/api/v4/users/{BOT}/patch").mock(
            side_effect=patch_user
        )
        profile = routes.put(f"https://mm.example/api/v4/bots/{BOT}").mock(side_effect=patch_bot)
        async with MattermostClient("https://mm.example/api/v4", "test-admin") as client:
            ctx = replace(env, mm=client)
            req = await propose(
                ctx, '--username renamed --description "New purpose" --criticality 1'
            )
            assert not rename.called and not profile.called
            assert "Approved" in await dispatch(approver(ctx), f"!approve {req.request_id}")
        assert rename.call_count == profile.call_count == 1
        assert env.bot_repo.get(BOT).username == "renamed"
        assert env.bot_repo.get(BOT).description == "New purpose"
        assert env.bot_repo.get(BOT).criticality == 1


def test_previous_release_remains_readable_before_automatic_migration(tmp_path):
    path = tmp_path / "previous.sqlite"
    conn = connect(str(path))
    try:
        install_previous_schema(conn)
        seed_previous_version(conn)
        before = path.read_bytes()
        # Read-only operator previews must not need to mutate the old schema first.
        assert BotRepo(conn).get(BOT).revision == 0
        assert BotRepo(conn).list_by_owner(ALICE)[0].username == "legacy"
        assert path.read_bytes() == before
        assert "revision" not in {row["name"] for row in conn.execute("PRAGMA table_info(bot)")}
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["create", "import", "update"])
async def test_direct_services_also_reject_self_approval(env, kind):
    from mm_bot_manager.imports import ImportError, approve_import
    from mm_bot_manager.operations import OperationError, create_managed_bot
    from mm_bot_manager.updates import UpdateError, approve_update

    ctx = replace(env, approver_ids=frozenset({ALICE, APPROVER}))
    if kind == "update":
        req = await propose(ctx)
        with pytest.raises(UpdateError, match="Self-approval"):
            await approve_update(ctx, req)
    else:
        req = ctx.pending_repo.create(
            request_id=kind,
            username="another-bot",
            display_name=None,
            description=None,
            requester_user_id=ALICE,
            requester_username="alice",
            git_repository=REPO,
            criticality=1,
            kind=kind,
            target_bot_user_id=BOT if kind == "import" else None,
        )
        if kind == "import":
            with pytest.raises(ImportError, match="Self-approval"):
                await approve_import(ctx, req)
        else:
            with pytest.raises(OperationError, match="Self-approval"):
                await create_managed_bot(
                    ctx, username=req.username, display_name=None, description=None, request=req
                )
    assert ctx.pending_repo.get(req.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()
