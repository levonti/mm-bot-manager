import json

import httpx
import pytest
import respx

from mm_bot_manager import recovery
from mm_bot_manager.config import Settings
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.repository import OperationRepo, PendingRequestRepo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    cfg = Settings(
        _env_file=None,
        mm_url="https://mm.example",
        mm_token="admin-test",
        mm_bot_token="bot-test",
        db_path=str(tmp_path / "db"),
    )
    monkeypatch.setattr(recovery, "load_settings", lambda: cfg)
    conn = connect(cfg.db_path)
    init_schema(conn)
    yield conn
    conn.close()


def test_acknowledging_reviewed_absence_releases_request(db):
    pending = PendingRequestRepo(db)
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
    assert pending.claim("req", "approver")
    repo = OperationRepo(db)
    op = repo.start("bot_create", "demo-bot", "req")
    repo.update(op, "needs_review")
    recovery.acknowledge_missing(db, op)
    assert not repo.unfinished()
    assert pending.get("req").status == "pending"


@pytest.mark.parametrize("status,remote_id", [("started", None), ("needs_review", "bot-id")])
def test_operator_cannot_acknowledge_known_resource_or_unreviewed_state(db, status, remote_id):
    repo = OperationRepo(db)
    op = repo.start("bot_create", "demo-bot")
    repo.update(op, status, remote_id)
    with pytest.raises(ValueError, match="Only uncertain"):
        recovery.acknowledge_missing(db, op)
    assert repo.unfinished()


def test_missing_operation_is_rejected(db):
    with pytest.raises(ValueError, match="not found"):
        recovery.acknowledge_missing(db, "does-not-exist")


@respx.mock
async def test_operator_can_attach_only_matching_bot_and_disable_it(db):
    repo = OperationRepo(db)
    op = repo.start("bot_create", "demo-bot")
    repo.update(op, "needs_review")
    respx.get("https://mm.example/api/v4/users/username/demo-bot").mock(
        return_value=httpx.Response(200, json={"id": "bot-id", "is_bot": True})
    )
    disable = respx.post("https://mm.example/api/v4/bots/bot-id/disable").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    assert await recovery.recover(db, operation_id=op, remote_id="bot-id") == 0
    assert disable.called
    assert not repo.unfinished()


@respx.mock
async def test_attach_rejects_non_bot_or_wrong_id_without_mutation(db):
    repo = OperationRepo(db)
    op = repo.start("bot_create", "demo-bot")
    repo.update(op, "needs_review")
    respx.get("https://mm.example/api/v4/users/username/demo-bot").mock(
        return_value=httpx.Response(200, json={"id": "another-user", "is_bot": False})
    )
    with pytest.raises(ValueError, match="supplied ID"):
        await recovery.recover(db, operation_id=op, remote_id="bot-id")
    assert repo.unfinished()[0].remote_id is None


def test_recovery_cli_lists_sanitized_journal(db, monkeypatch, capsys):
    op = OperationRepo(db).start("bot_create", "demo-bot")
    monkeypatch.setattr("sys.argv", ["recovery", "list"])
    assert recovery.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["operation_id"] == op
    assert set(rows[0]) == {"operation_id", "kind", "resource", "request_id", "status", "remote_id"}


def test_recovery_cli_requires_explicit_review_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["recovery", "acknowledge-missing", "op-id"])
    with pytest.raises(SystemExit) as exc:
        recovery.main()
    assert exc.value.code == 2


def test_recovery_cli_acknowledges_reviewed_missing_resource(db, monkeypatch):
    repo = OperationRepo(db)
    op = repo.start("bot_create", "demo-bot")
    repo.update(op, "needs_review")
    monkeypatch.setattr(
        "sys.argv", ["recovery", "acknowledge-missing", op, "--reviewed-no-resource"]
    )
    assert recovery.main() == 0
    assert not repo.unfinished()


@respx.mock
def test_recovery_cli_retry_empty_journal_needs_no_api_calls(db, monkeypatch):
    monkeypatch.setattr("sys.argv", ["recovery", "retry"])
    assert recovery.main() == 0


@respx.mock
async def test_operator_retry_resolves_configured_reviewers_for_update_recovery(db, monkeypatch):
    from mm_bot_manager.updates import UpdateRepo
    from tests.test_imports import ALICE, APPROVER, BOT, import_context
    from tests.test_updates import UpdateMM, propose, seed_bot

    cfg = recovery.load_settings()
    cfg.mm_approvers = "approver"
    ctx = import_context(db, UpdateMM())
    seed_bot(ctx)
    req = await propose(ctx, "--criticality 1")
    assert ctx.pending_repo.claim(req.request_id, APPROVER)
    respx.get("https://mm.example/api/v4/users/username/approver").mock(
        return_value=httpx.Response(200, json={"id": APPROVER})
    )
    for user_id in (ALICE, APPROVER):
        respx.get(f"https://mm.example/api/v4/users/{user_id}").mock(
            return_value=httpx.Response(200, json={"id": user_id, "delete_at": 0, "is_bot": False})
        )
    assert await recovery.recover(db) == 0
    assert ctx.pending_repo.get(req.request_id).status == "approved"
    assert ctx.bot_repo.get(BOT).criticality == 1
    assert UpdateRepo(db).active(BOT) is None


async def test_operator_list_includes_sanitized_update_journal(db, monkeypatch, capsys):
    from tests.test_imports import APPROVER, import_context
    from tests.test_updates import REPO, UpdateMM, propose, seed_bot

    ctx = import_context(db, UpdateMM())
    seed_bot(ctx)
    req = await propose(ctx)
    assert ctx.pending_repo.claim(req.request_id, APPROVER)
    monkeypatch.setattr("sys.argv", ["recovery", "list"])
    assert recovery.main() == 0
    output = capsys.readouterr().out
    assert REPO not in output
    assert json.loads(output)[0]["kind"] == "bot_update"
    assert json.loads(output)[0]["request_id"] == req.request_id


@respx.mock
async def test_recovery_without_updates_does_not_depend_on_approver_resolution(db):
    # A removed approver must not block cleanup of abandoned creations/tokens.
    recovery.load_settings().mm_approvers = "deleted-user"
    assert await recovery.recover(db) == 0
    assert not respx.calls
