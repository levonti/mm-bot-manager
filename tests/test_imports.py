import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from mm_bot_manager.commands import CommandContext, dispatch
from mm_bot_manager.config import Settings
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.import_bots import process_mapping, read_mapping, run_import
from mm_bot_manager.imports import ImportError, approve_import, prepare_import, submit_imports
from mm_bot_manager.mm_client import MattermostError
from mm_bot_manager.repository import BotRepo, OperationRepo, PendingRequestRepo, TokenRepo
from tests.test_commands import FakeMM

BOT = "b" * 26
BOT2 = "c" * 26
ALICE = "a" * 26
BOB = "d" * 26
REMOTE_OWNER = "r" * 26
APPROVER = "p" * 26
MANAGER = "m" * 26


class ImportMM(FakeMM):
    def __init__(self):
        super().__init__()
        self.bots = {
            bot_id: {
                "user_id": bot_id,
                "username": name,
                "display_name": name.title(),
                "description": "Existing integration",
                "owner_id": REMOTE_OWNER,
                "delete_at": 0,
            }
            for bot_id, name in ((BOT, "legacy"), (BOT2, "legacy-two"))
        }
        self.users = {
            user_id: {"id": user_id, "username": name, "delete_at": 0, "roles": "system_user"}
            for user_id, name in (
                (ALICE, "alice"),
                (BOB, "bob"),
                (REMOTE_OWNER, "admin"),
                (APPROVER, "approver"),
                (MANAGER, "manager"),
            )
        }
        self.users.update(
            {
                bot_id: {
                    "id": bot_id,
                    "username": bot["username"],
                    "is_bot": True,
                    "roles": "system_user",
                    "delete_at": 0,
                }
                for bot_id, bot in self.bots.items()
            }
        )
        self.reads = []

    async def get_user(self, user_id):
        self._maybe_raise("get_user")
        self.reads.append(("user", user_id))
        await asyncio.sleep(0)  # Exercise interleaved approvals before the DB transaction.
        if user_id not in self.users:
            raise MattermostError(404, "not found")
        return self.users[user_id].copy()

    async def get_bot(self, bot_user_id):
        self._maybe_raise("get_bot")
        self.reads.append(("bot", bot_user_id))
        if bot_user_id not in self.bots:
            raise MattermostError(404, "not found")
        return self.bots[bot_user_id].copy()

    async def get_user_by_username(self, username):
        for user in self.users.values():
            if user["username"] == username:
                return user.copy()
        raise MattermostError(404, "not found")

    def assert_no_bot_mutations(self):
        assert not self.created and not self.patched and not self.disabled
        assert not self.tokens_created and not self.tokens_revoked


def import_context(conn, mm=None):
    mm = mm or ImportMM()
    return CommandContext(
        caller_user_id=ALICE,
        caller_username="alice",
        channel_id="dm-alice",
        bot_repo=BotRepo(conn),
        token_repo=TokenRepo(conn),
        pending_repo=PendingRequestRepo(conn),
        mm=mm,
        bot_mm=mm,
        manager_user_id=MANAGER,
        approver_ids=frozenset({APPROVER}),
        operation_repo=OperationRepo(conn),
        channel_type="D",
    )


@pytest.fixture()
def env():
    conn = connect(":memory:")
    init_schema(conn)
    yield import_context(conn)
    conn.close()


def approver(ctx):
    return replace(ctx, caller_user_id=APPROVER, caller_username="approver")


async def request_import(ctx, bot_id=BOT, owner_id=ALICE):
    plan = await prepare_import(ctx, bot_id, owner_id, "https://git.example/team/project", 2)
    return submit_imports(ctx, [plan])[0]


def table_count(ctx, table):
    return ctx.bot_repo.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


async def test_chat_import_requires_separate_approval_and_preserves_remote_tokens(env):
    env.mm.remote_tokens["old-token"] = {"id": "old-token", "description": "existing"}
    reply = await dispatch(
        env, "!import @legacy --repo https://git.example/team/project --criticality 2"
    )
    assert "mandatory approval" in reply
    assert table_count(env, "bot") == 0
    request = env.pending_repo.get(
        env.bot_repo.conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[0]
    )
    assert request.kind == "import"
    assert request.target_bot_user_id == BOT and request.remote_owner_id == REMOTE_OWNER
    assert "Existing bot import" in env.mm.posts[0]["message"]
    assert "not authorised" in await dispatch(env, f"!approve {request.request_id}")
    assert "imported" in await dispatch(approver(env), f"!approve {request.request_id}")
    bot = env.bot_repo.get(BOT)
    assert bot.username == "legacy"  # No -bot suffix or remote rename required.
    assert bot.owner_user_id == ALICE
    audit = env.bot_repo.conn.execute("SELECT * FROM bot_import").fetchone()
    assert audit["source"] == "imported" and audit["submitted_by"] == ALICE
    assert audit["approved_by"] == APPROVER and audit["imported_at"]
    assert audit["request_id"] == request.request_id and audit["remote_owner_id"] == REMOTE_OWNER
    assert "Other tokens may exist" in await dispatch(env, "!token list legacy")
    assert "not owned" in await dispatch(replace(env, caller_user_id=BOB), "!info legacy")
    assert "legacy" in await dispatch(env, "!list")
    assert env.mm.remote_tokens == {"old-token": {"id": "old-token", "description": "existing"}}
    env.mm.assert_no_bot_mutations()
    assert table_count(env, "operation") == 0
    # Imported bots use the existing owner-scoped management commands after approval.
    assert "Minted token" in await dispatch(env, "!token create legacy")
    assert "secret-" in env.mm.posts[-1]["message"]


@pytest.mark.parametrize("channel", [None, "O", "P", "G"])
async def test_import_and_approval_are_dm_only(env, channel):
    request = await request_import(env)
    ctx = replace(env, channel_type=channel)
    assert "direct message" in await dispatch(
        ctx, "!import legacy --repo https://git.example/team/project --criticality 2"
    )
    assert "direct message" in await dispatch(approver(ctx), f"!approve {request.request_id}")
    assert env.pending_repo.get(request.request_id).status == "pending"
    assert table_count(env, "bot") == 0
    env.mm.assert_no_bot_mutations()


async def test_empty_approvers_never_enable_import(env):
    ctx = replace(env, approver_ids=frozenset())
    assert "MM_APPROVERS" in await dispatch(
        ctx, "!import legacy --repo https://git.example/team/project --criticality 2"
    )
    with pytest.raises(ImportError, match="MM_APPROVERS"):
        await prepare_import(ctx, BOT, ALICE, "https://git.example/team/project", 2)
    with pytest.raises(ImportError, match="MM_APPROVERS"):
        submit_imports(ctx, [])
    assert table_count(env, "pending_bot_request") == 0
    assert not env.mm.reads


@pytest.mark.parametrize(
    "command", ["!import", "!import legacy extra", "!import legacy --owner bob"]
)
async def test_import_rejects_ambiguous_input(env, command):
    assert "Usage:" in await dispatch(env, command)
    assert not env.mm.reads


@pytest.mark.parametrize("name", ["../admin", "legacy?foo", "legacy/name", "UPPER"])
async def test_import_rejects_invalid_username(env, name):
    assert "Invalid" in await dispatch(env, "!import " + name)
    assert not env.mm.reads


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("bot", "user_id", BOT2),
        ("user", "id", BOT2),
        ("user", "is_bot", False),
        ("bot", "delete_at", 123),
        ("user", "delete_at", 123),
        ("bot", "system_owned", True),
        ("user", "roles", "system_user system_admin"),
        ("user", "roles", "custom_elevated_role"),
        ("bot", "username", "renamed"),
        ("bot", "owner_id", "plugin.manifest.id"),
        ("bot", "owner_id", "z" * 26),
        ("remote_owner", "is_bot", True),
        ("remote_owner", "delete_at", 123),
        ("owner", "delete_at", 123),
        ("owner", "is_bot", True),
        ("owner", "username", ""),
    ],
)
async def test_ineligible_import_fails_closed_without_remote_mutations(env, target, field, value):
    account = {
        "bot": env.mm.bots[BOT],
        "user": env.mm.users[BOT],
        "remote_owner": env.mm.users[REMOTE_OWNER],
        "owner": env.mm.users[ALICE],
    }[target]
    account[field] = value
    with pytest.raises((ImportError, MattermostError)):
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2)
    assert table_count(env, "pending_bot_request") == 0
    env.mm.assert_no_bot_mutations()


async def test_protected_names_and_manager_are_excluded(env):
    env.mm.bots[BOT]["username"] = env.mm.users[BOT]["username"] = "playbooks"
    with pytest.raises(ImportError, match="protected"):
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2)
    with pytest.raises(ImportError, match="manager"):
        await prepare_import(
            replace(env, manager_user_id=BOT), BOT, ALICE, "https://git.example/team/project", 2
        )
    with pytest.raises(ImportError, match="immutable"):
        await prepare_import(env, "../../users", ALICE, "https://git.example/team/project", 2)


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_server_validation_errors_do_not_create_requests(env, status):
    env.mm.raise_on["get_bot"] = MattermostError(status, "unavailable")
    assert "Could not request import" in await dispatch(
        env, "!import legacy --repo https://git.example/team/project --criticality 2"
    )
    assert table_count(env, "pending_bot_request") == 0
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize("drift", ["rename", "owner", "disabled", "role", "assigned_owner"])
async def test_approval_revalidates_snapshot_and_active_accounts(env, drift):
    request = await request_import(env)
    if drift == "rename":
        env.mm.bots[BOT]["username"] = env.mm.users[BOT]["username"] = "renamed"
    elif drift == "owner":
        env.mm.bots[BOT]["owner_id"] = BOB
    elif drift == "disabled":
        env.mm.users[BOT]["delete_at"] = 10
    elif drift == "role":
        env.mm.users[BOT]["roles"] = "system_user system_admin"
    else:
        env.mm.users[ALICE]["delete_at"] = 10
    assert "Could not import" in await dispatch(approver(env), f"!approve {request.request_id}")
    assert env.pending_repo.get(request.request_id).status == "pending"
    assert table_count(env, "bot") == 0
    env.mm.assert_no_bot_mutations()


async def test_idempotent_submission_and_reimport_preserve_original_audit(env):
    request = await request_import(env)
    assert (await request_import(env)).request_id == request.request_id
    assert table_count(env, "pending_bot_request") == 1
    await approve_import(approver(env), request)
    audit = dict(env.bot_repo.conn.execute("SELECT * FROM bot_import").fetchone())
    assert "already managed" in await dispatch(
        env, "!import legacy --repo https://git.example/team/project --criticality 2"
    )
    assert dict(env.bot_repo.conn.execute("SELECT * FROM bot_import").fetchone()) == audit
    with pytest.raises(ImportError, match="another owner"):
        await prepare_import(env, BOT, BOB, "https://git.example/team/project", 2)
    env.bot_repo.mark_deleted(BOT)
    with pytest.raises(ImportError, match="removed locally"):
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2)
    env.mm.assert_no_bot_mutations()


async def test_pending_conflicts_require_denial_before_replacement(env):
    request = await request_import(env)
    plan = await prepare_import(env, BOT, BOB, "https://git.example/team/project", 2)
    with pytest.raises(ImportError, match="conflicting"):
        submit_imports(env, [plan])
    reply = await dispatch(approver(env), f"!deny {request.request_id} wrong owner")
    assert "Denied" in reply and "import request" in env.mm.posts[-1]["message"]
    assert "already" in await dispatch(approver(env), f"!approve {request.request_id}")
    assert (await request_import(env, owner_id=BOB)).requester_user_id == BOB
    env.mm.assert_no_bot_mutations()


async def test_concurrent_approvals_bind_only_once(env):
    request = await request_import(env)
    results = await asyncio.gather(
        dispatch(approver(env), f"!approve {request.request_id}"),
        dispatch(approver(env), f"!approve {request.request_id}"),
    )
    assert sum("imported" in r for r in results) == 1
    assert table_count(env, "bot") == table_count(env, "bot_import") == 1
    assert env.pending_repo.get(request.request_id).status == "approved"
    env.mm.assert_no_bot_mutations()


async def test_denial_during_remote_revalidation_wins(env):
    request = await request_import(env)
    original = env.mm.get_bot

    async def deny_then_read(bot_id):
        await dispatch(approver(env), f"!deny {request.request_id}")
        return await original(bot_id)

    env.mm.get_bot = deny_then_read
    with pytest.raises(ImportError, match="already"):
        await approve_import(approver(env), request)
    assert table_count(env, "bot") == 0
    assert env.pending_repo.get(request.request_id).status == "denied"
    env.mm.assert_no_bot_mutations()


async def test_database_failure_rolls_back_import_and_approval_without_compensation(
    env, monkeypatch
):
    request = await request_import(env)
    original = env.bot_repo.insert

    def fail_after_insert(**kwargs):
        original(**kwargs)
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(env.bot_repo, "insert", fail_after_insert)
    with pytest.raises(RuntimeError, match="disk failure"):
        await approve_import(approver(env), request)
    assert table_count(env, "bot") == table_count(env, "bot_import") == 0
    assert env.pending_repo.get(request.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()
    monkeypatch.setattr(env.bot_repo, "insert", original)
    await approve_import(approver(env), request)
    assert env.pending_repo.get(request.request_id).status == "approved"


async def test_missing_audit_and_unknown_request_kind_never_create_remote_bot(env):
    request = await request_import(env)
    env.bot_repo.conn.execute("DELETE FROM import_submission")
    with pytest.raises(ImportError, match="audit"):
        await approve_import(approver(env), request)
    assert table_count(env, "bot") == 0
    env.bot_repo.conn.execute("UPDATE pending_bot_request SET kind = 'unknown'")
    assert "Unknown request kind" in await dispatch(approver(env), f"!approve {request.request_id}")
    assert env.pending_repo.get(request.request_id).status == "pending"
    env.mm.assert_no_bot_mutations()


async def test_bulk_dry_run_and_atomic_submission(env):
    rows = [
        (BOT, ALICE, "https://git.example/team/project", 2),
        (BOT2, BOB, "https://git.example/team/project", 2),
    ]
    plans, ids = await process_mapping(env, rows, submit=False)
    assert len(plans) == 2 and not ids
    assert table_count(env, "pending_bot_request") == table_count(env, "bot") == 0
    assert not env.mm.posts
    _, ids = await process_mapping(env, rows, submit=True)
    assert len(ids) == 2 and table_count(env, "bot") == 0
    assert env.pending_repo.get(ids[1]).requester_user_id == BOB
    assert env.pending_repo.get(ids[1]).kind == "import"
    assert (
        env.bot_repo.conn.execute(
            "SELECT submitted_by FROM import_submission WHERE request_id = ?", (ids[1],)
        ).fetchone()[0]
        == ALICE
    )
    for request_id in ids:
        await approve_import(approver(env), env.pending_repo.get(request_id))
    assert env.bot_repo.get(BOT).owner_user_id == ALICE
    assert env.bot_repo.get(BOT2).owner_user_id == BOB
    env.mm.assert_no_bot_mutations()


async def test_bulk_invalid_last_row_leaves_no_partial_requests_or_notifications(env):
    env.mm.users[BOT2]["delete_at"] = 1
    with pytest.raises(ImportError):
        await process_mapping(
            env,
            [
                (BOT, ALICE, "https://git.example/team/project", 2),
                (BOT2, BOB, "https://git.example/team/project", 2),
            ],
            submit=True,
        )
    assert table_count(env, "pending_bot_request") == 0
    assert not env.mm.posts
    env.mm.assert_no_bot_mutations()


async def test_bulk_transaction_rolls_back_earlier_rows_on_conflict(env):
    await request_import(env, BOT2, ALICE)
    plans = [
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2),
        await prepare_import(env, BOT2, BOB, "https://git.example/team/project", 2),
    ]
    with pytest.raises(ImportError, match="conflicting"):
        submit_imports(env, plans)
    assert table_count(env, "pending_bot_request") == table_count(env, "import_submission") == 1
    env.mm.assert_no_bot_mutations()


@pytest.mark.parametrize(
    "contents",
    [
        "bot,owner\nfoo,bar\n",
        "bot_user_id,owner_user_id,git_repository,criticality\n",
        "bot_user_id,owner_user_id,git_repository,criticality\nfoo\n",
        "bot_user_id,owner_user_id,git_repository,criticality\nfoo,bar,extra\n",
        "bot_user_id,owner_user_id,git_repository,criticality\nfoo,\n",
        "bot_user_id,owner_user_id,git_repository,criticality\nfoo,bar\nfoo,other\n",
        'bot_user_id,owner_user_id,git_repository,criticality\n"unterminated',
    ],
)
def test_bulk_csv_rejects_malformed_or_ambiguous_rows(tmp_path, contents):
    import csv

    path = tmp_path / "mapping.csv"
    path.write_text(contents)
    with pytest.raises((ImportError, csv.Error)):
        read_mapping(path)


def test_bulk_csv_accepts_bom_and_rejects_excessive_batch(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        f"\ufeffbot_user_id,owner_user_id,git_repository,criticality\n{BOT},{ALICE},https://git.example/team/project,2\n"
    )
    assert read_mapping(path) == [(BOT, ALICE, "https://git.example/team/project", 2)]
    path.write_text(
        "bot_user_id,owner_user_id,git_repository,criticality\n"
        + "".join(f"b{i},a,https://git.example/team/project,2\n" for i in range(1001))
    )
    with pytest.raises(ImportError, match="1000"):
        read_mapping(path)


def settings_for(path):
    return Settings(
        _env_file=None,
        mm_url="https://mm.example",
        mm_token="admin-test-token",
        mm_bot_token="bot-test-token",
        mm_approvers="approver",
        db_path=str(path),
    )


def mock_read_api(mm):
    async def respond(request):
        suffix = request.url.path.removeprefix("/api/v4")
        if suffix == "/users/me":
            uid = MANAGER if request.headers["authorization"] == "Bearer bot-test-token" else ALICE
            return httpx.Response(200, json=mm.users[uid])
        if suffix.startswith("/users/username/"):
            user = await mm.get_user_by_username(suffix.rsplit("/", 1)[1])
            return httpx.Response(200, json=user)
        if suffix.startswith("/users/"):
            return httpx.Response(200, json=await mm.get_user(suffix.rsplit("/", 1)[1]))
        if suffix.startswith("/bots/"):
            return httpx.Response(200, json=await mm.get_bot(suffix.rsplit("/", 1)[1]))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return respx.get(url__startswith="https://mm.example/api/v4/").mock(side_effect=respond)


@pytest.mark.parametrize("existing_db", [False, True])
@respx.mock
async def test_cli_default_dry_run_uses_get_only_and_does_not_create_or_change_database(
    tmp_path, capsys, existing_db
):
    path = tmp_path / "runtime" / "registry.sqlite"
    settings = settings_for(path)
    if existing_db:
        conn = connect(str(path))
        init_schema(conn)
        conn.close()
        before = path.read_bytes()
    csv_path = tmp_path / "mapping.csv"
    csv_path.write_text(
        f"bot_user_id,owner_user_id,git_repository,criticality\n{BOT},{ALICE},https://git.example/team/project,2\n"
    )
    route = mock_read_api(ImportMM())
    assert await run_import(settings, csv_path) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run" and output["request_ids"] == []
    assert output["bots"][0]["bot_user_id"] == BOT
    assert all(call.request.method == "GET" for call in route.calls)
    if existing_db:
        assert path.read_bytes() == before
    else:
        assert not path.parent.exists()


@respx.mock
async def test_cli_submit_creates_only_pending_requests_and_is_recoverable_on_notification_error(
    tmp_path, capsys
):
    path = tmp_path / "registry.sqlite"
    csv_path = tmp_path / "mapping.csv"
    csv_path.write_text(
        f"bot_user_id,owner_user_id,git_repository,criticality\n{BOT},{BOB},https://git.example/team/project,2\n"
    )
    mock_read_api(ImportMM())
    notify = respx.post("https://mm.example/api/v4/channels/direct").mock(
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )
    assert await run_import(settings_for(path), csv_path, submit=True) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "submitted_for_approval" and len(output["request_ids"]) == 1
    assert notify.called
    conn = connect(str(path))
    try:
        ctx = import_context(conn)
        request = ctx.pending_repo.get(output["request_ids"][0])
        assert request.status == "pending" and request.requester_user_id == BOB
        assert table_count(ctx, "bot") == 0
        assert conn.execute("SELECT submitted_by FROM import_submission").fetchone()[0] == ALICE
    finally:
        conn.close()


def assert_legacy_upgrade(conn):
    # Simulate an existing installation with preserved bots, tokens and creation requests.
    for name in ("insert", "update"):
        suffix = "" if conn.dialect == "sqlite" else " ON pending_bot_request"
        conn.execute(f"DROP TRIGGER require_pending_bot_request_metadata_{name}{suffix}")
    for name in ("kind", "target_bot_user_id", "remote_owner_id"):
        conn.execute(f"ALTER TABLE pending_bot_request DROP COLUMN {name}")
    conn.execute(
        "INSERT INTO pending_bot_request (request_id, username, requester_user_id, "
        "requester_username, created_at) VALUES ('old', 'existing-bot', 'alice', 'alice', 'then')"
    )
    BotRepo(conn).insert(
        bot_user_id="old-bot",
        username="existing-bot",
        display_name=None,
        description=None,
        owner_user_id="alice",
        owner_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    TokenRepo(conn).record(token_id="old-token", bot_user_id="old-bot", description="existing")
    init_schema(conn)
    init_schema(conn)  # Idempotent restart.
    old = PendingRequestRepo(conn).get("old")
    assert old.kind == "create" and old.target_bot_user_id is None and old.status == "pending"
    assert BotRepo(conn).get("old-bot").owner_user_id == "alice"
    assert TokenRepo(conn).get("old-token").revoked_at is None


def test_sqlite_additive_schema_upgrade_preserves_legacy_data():
    conn = connect(":memory:")
    init_schema(conn)
    try:
        assert_legacy_upgrade(conn)
    finally:
        conn.close()


async def test_import_cannot_reuse_conflicting_local_username(env):
    env.bot_repo.insert(
        bot_user_id=BOT2,
        username="legacy",
        display_name=None,
        description=None,
        owner_user_id=BOB,
        owner_username="bob",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    with pytest.raises(ImportError, match="different local bot ID"):
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2)
    env.mm.assert_no_bot_mutations()


async def test_import_refuses_stale_local_name_and_unauthorised_service_approval(env):
    request = await request_import(env)
    with pytest.raises(ImportError, match="Only configured"):
        await approve_import(env, request)
    with pytest.raises(ImportError, match="Invalid import"):
        await approve_import(approver(env), replace(request, target_bot_user_id=None))
    await approve_import(approver(env), request)
    env.bot_repo.patch(BOT, username="old-name")
    with pytest.raises(ImportError, match="local username differs"):
        await prepare_import(env, BOT, ALICE, "https://git.example/team/project", 2)


async def test_repeated_approval_handles_same_owner_binding_without_replacing_audit(env):
    request = await request_import(env)
    env.bot_repo.insert(
        bot_user_id=BOT,
        username="legacy",
        display_name=None,
        description=None,
        owner_user_id=ALICE,
        owner_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    await approve_import(approver(env), request)
    assert env.pending_repo.get(request.request_id).status == "approved"
    assert table_count(env, "bot") == 1
    env.mm.assert_no_bot_mutations()


async def test_empty_approvers_and_username_lookup_errors_reject_cli_and_chat(env, tmp_path):
    assert "Could not request import" in await dispatch(
        env, "!import absent --repo https://git.example/team/project --criticality 2"
    )
    settings = settings_for(tmp_path / "registry.sqlite").model_copy(update={"mm_approvers": ""})
    csv_path = tmp_path / "mapping.csv"
    csv_path.write_text(
        f"bot_user_id,owner_user_id,git_repository,criticality\n{BOT},{ALICE},https://git.example/team/project,2\n"
    )
    with respx.mock, pytest.raises(ImportError, match="MM_APPROVERS"):
        await run_import(settings, csv_path)
    assert not (tmp_path / "registry.sqlite").exists()


@pytest.mark.parametrize("mode", ["default", "dry_run", "submit"])
def test_cli_entrypoint_defaults_to_preview_and_requires_explicit_submit(
    tmp_path, monkeypatch, mode
):
    from mm_bot_manager import import_bots

    calls = []
    settings = settings_for(tmp_path / "db.sqlite")

    async def run_fake(settings_arg, path, *, submit=False):
        calls.append((settings_arg, path, submit))
        return 0

    flags = {"default": [], "dry_run": ["--dry-run"], "submit": ["--submit"]}[mode]
    monkeypatch.setattr("sys.argv", ["mm-bot-manager-import", "mapping.csv", *flags])
    monkeypatch.setattr(import_bots, "load_settings", lambda: settings)
    monkeypatch.setattr(import_bots, "run_import", run_fake)
    with pytest.raises(SystemExit) as exc:
        import_bots.run()
    assert exc.value.code == 0
    assert calls == [(settings, Path("mapping.csv"), mode == "submit")]


@pytest.mark.parametrize(
    "error,expected",
    [
        (ImportError("bad mapping"), "bad mapping"),
        (MattermostError(403, "credential-secret"), "HTTP 403"),
        (RuntimeError("postgresql://credential-secret"), "instance lock"),
        (OSError("credential-secret"), "instance lock"),
    ],
)
def test_cli_errors_do_not_disclose_connection_or_server_secrets(
    tmp_path, monkeypatch, capsys, error, expected
):
    from mm_bot_manager import import_bots

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("sys.argv", ["mm-bot-manager-import", "mapping.csv"])
    monkeypatch.setattr(import_bots, "load_settings", lambda: settings_for(tmp_path / "db"))
    monkeypatch.setattr(import_bots, "run_import", fail)
    with pytest.raises(SystemExit) as exc:
        import_bots.run()
    assert exc.value.code == 2
    output = capsys.readouterr().err
    assert expected in output and "credential-secret" not in output
