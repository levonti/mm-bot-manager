"""Regression tests for the security and failure cases found during the review."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from mm_bot_manager.commands import dispatch
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.dispatcher import CommandContextFactory, MessageRouter, handle_event
from mm_bot_manager.mm_client import MattermostError
from mm_bot_manager.operations import recover_operations
from mm_bot_manager.repository import BotRepo, PendingRequestRepo, TokenRepo
from tests.test_commands import FakeMM


@pytest.fixture()
def manager(tmp_path):
    path = str(tmp_path / "manager.sqlite3")
    conn = connect(path)
    init_schema(conn)
    mm = FakeMM()

    def factory(connection=conn):
        return CommandContextFactory(
            BotRepo(connection),
            TokenRepo(connection),
            PendingRequestRepo(connection),
            mm,
            mm,
            "mgr",
            frozenset({"approver-a", "approver-b"}),
        )

    f = factory()
    ctx = f.build(
        caller_user_id="alice", caller_username="alice", channel_id="dm-alice", channel_type="D"
    )
    # Most tests exercise immediate creation; approval tests opt in explicitly.
    ctx = replace(ctx, approver_ids=frozenset())
    yield SimpleNamespace(
        conn=conn,
        mm=mm,
        factory=f,
        new_factory=factory,
        ctx=ctx,
        path=path,
        router=MessageRouter("mgr", "manager"),
    )
    conn.close()


def event(post_id="post-1", *, channel_type="D", message="!token create demo-bot"):
    return {
        "event": "posted",
        "data": {
            "channel_type": channel_type,
            "sender_name": "@alice",
            "post": json.dumps(
                {"id": post_id, "user_id": "alice", "channel_id": "dm-alice", "message": message}
            ),
        },
    }


async def create_bot(m):
    reply = await dispatch(m.ctx, "!create demo-bot")
    assert "Created bot" in reply


@pytest.mark.parametrize("channel_type", ["O", "P", "G", None, ""])
async def test_token_creation_rejected_outside_dm(manager, channel_type):
    m = manager
    await create_bot(m)
    reply = await dispatch(replace(m.ctx, channel_type=channel_type), "!token create demo-bot")
    assert "direct message" in reply
    assert m.mm.tokens_created == []
    assert m.mm.posts == []


async def test_public_mention_cannot_mint_or_publish_token(manager):
    m = manager
    await create_bot(m)
    await handle_event(
        event(channel_type="O", message="@manager !token create demo-bot"), m.router, m.factory
    )
    assert not m.mm.tokens_created
    assert "direct message" in m.mm.posts[-1]["message"]
    assert "secret-" not in m.mm.posts[-1]["message"]


async def test_secret_only_in_dm_never_in_journal_or_logs(manager, capsys):
    m = manager
    await create_bot(m)
    await handle_event(event(), m.router, m.factory)
    secret_posts = [p for p in m.mm.posts if "secret-tok-0" in p["message"]]
    assert len(secret_posts) == 1
    assert secret_posts[0]["channel_id"] == "dm-alice"
    assert "remains in your Mattermost history" in secret_posts[0]["message"]
    assert "secret-tok-0" not in m.mm.posts[-1]["message"]
    dump = "\n".join(m.conn._inner.iterdump())
    assert "secret-tok-0" not in dump
    assert not m.ctx.operation_repo.unfinished()
    assert "secret-tok-0" not in capsys.readouterr().out


async def test_failed_delivery_revokes_token_and_does_not_log_remote_error(manager, capsys):
    m = manager
    await create_bot(m)
    m.mm.raise_on["create_post"] = httpx.ReadTimeout("remote error echo: secret-tok-0")
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "revoked" in reply
    assert m.mm.tokens_revoked == ["tok-0"]
    assert m.ctx.token_repo.get("tok-0").revoked_at is not None
    assert m.ctx.operation_repo.unfinished() == []
    assert not m.mm.posts
    assert "secret-tok-0" not in capsys.readouterr().out


async def test_delivery_accepted_then_timeout_still_revokes(manager, monkeypatch):
    m = manager
    await create_bot(m)
    original = m.mm.create_post

    async def post_then_timeout(**kwargs):
        await original(**kwargs)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(m.mm, "create_post", post_then_timeout)
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "revoked" in reply
    assert len(m.mm.posts) == 1
    assert not m.mm.remote_tokens


async def test_bot_db_failure_disables_remote_bot(manager):
    m = manager
    m.conn.execute(
        "CREATE TRIGGER fail_bot BEFORE INSERT ON bot "
        "BEGIN SELECT RAISE(ABORT, 'disk write failed'); END"
    )
    reply = await dispatch(m.ctx, "!create demo-bot")
    assert "not completed" in reply
    assert m.mm.disabled == ["bot-0"]
    assert m.ctx.bot_repo.list_by_owner("alice") == []
    assert m.ctx.operation_repo.unfinished() == []


async def test_token_db_failure_revokes_remote_token(manager):
    m = manager
    await create_bot(m)
    m.conn.execute(
        "CREATE TRIGGER fail_token BEFORE INSERT ON token "
        "BEGIN SELECT RAISE(ABORT, 'disk write failed'); END"
    )
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "revoked" in reply
    assert m.mm.tokens_revoked == ["tok-0"]
    assert m.ctx.token_repo.list_active_for_bot("bot-0") == []
    assert not m.mm.posts


async def test_failure_saving_remote_id_still_compensates(manager):
    m = manager
    m.conn.execute(
        "CREATE TRIGGER fail_remote_id BEFORE UPDATE ON operation "
        "WHEN NEW.status = 'remote_created' "
        "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    await dispatch(m.ctx, "!create demo-bot")
    assert m.mm.disabled == ["bot-0"]
    assert m.ctx.operation_repo.unfinished() == []


async def test_failure_marking_token_delivered_revokes_it(manager):
    m = manager
    await create_bot(m)
    m.conn.execute(
        "CREATE TRIGGER fail_completion BEFORE UPDATE ON operation "
        "WHEN NEW.kind = 'token_create' AND NEW.status = 'completed' "
        "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "revoked" in reply
    assert m.mm.posts  # The received token must now be unusable.
    assert not m.mm.remote_tokens
    assert not m.ctx.token_repo.list_active_for_bot("bot-0")


async def test_failed_revocation_is_retried_during_recovery(manager):
    m = manager
    await create_bot(m)
    m.mm.raise_on["create_post"] = httpx.ReadTimeout("delivery uncertain")
    m.mm.raise_on["revoke_user_access_token"] = MattermostError(503, "unavailable")
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "operator attention" in reply
    assert m.ctx.operation_repo.unfinished()[0].remote_id == "tok-0"
    assert await recover_operations(m.ctx) == 0
    assert m.mm.tokens_revoked == ["tok-0"]
    assert not m.ctx.token_repo.list_active_for_bot("bot-0")
    assert not m.ctx.operation_repo.unfinished()


async def test_failed_bot_compensation_survives_reconnection(manager):
    m = manager
    m.conn.execute(
        "CREATE TRIGGER fail_bot BEFORE INSERT ON bot "
        "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    m.mm.raise_on["disable_bot"] = MattermostError(503, "unavailable")
    await dispatch(m.ctx, "!create demo-bot")
    assert m.ctx.operation_repo.unfinished()[0].remote_id == "bot-0"
    second_conn = connect(m.path)
    try:
        ctx = replace(
            m.ctx,
            operation_repo=m.new_factory(second_conn).operation_repo,
            bot_repo=BotRepo(second_conn),
            token_repo=TokenRepo(second_conn),
            pending_repo=PendingRequestRepo(second_conn),
        )
        assert await recover_operations(ctx) == 0
        assert not ctx.operation_repo.unfinished()
    finally:
        second_conn.close()
    assert m.mm.disabled == ["bot-0"]


async def test_lost_token_response_recovered_by_marker_without_touching_other_tokens(
    manager,
    monkeypatch,
):
    m = manager
    await create_bot(m)
    m.mm.remote_tokens["unrelated"] = {"id": "unrelated", "description": "another integration"}
    original = m.mm.create_user_access_token

    async def create_then_timeout(*args):
        await original(*args)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(m.mm, "create_user_access_token", create_then_timeout)
    await dispatch(m.ctx, "!token create demo-bot")
    op = m.ctx.operation_repo.unfinished()[0]
    assert op.remote_id is None
    assert op.status == "needs_review"
    assert await recover_operations(m.ctx) == 0
    assert m.mm.tokens_revoked == ["tok-0"]
    assert "unrelated" in m.mm.remote_tokens
    assert not m.ctx.operation_repo.unfinished()


async def test_unknown_bot_outcome_is_blocked_and_never_adopted_by_name(manager, monkeypatch):
    m = manager
    original = m.mm.create_bot

    async def create_then_timeout(**kwargs):
        await original(**kwargs)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(m.mm, "create_bot", create_then_timeout)
    await dispatch(m.ctx, "!create demo-bot")
    assert await recover_operations(m.ctx) == 1
    reply = await dispatch(m.ctx, "!create demo-bot")
    assert "operator review" in reply
    assert len(m.mm.created) == 1
    assert not m.mm.disabled
    assert not m.ctx.bot_repo.list_by_owner("alice")


async def test_no_matching_token_is_uncertain_not_automatically_reissued(manager):
    m = manager
    await create_bot(m)
    m.ctx.operation_repo.start("token_create", "bot-0")
    assert await recover_operations(m.ctx) == 1
    reply = await dispatch(m.ctx, "!token create demo-bot")
    assert "operator review" in reply
    assert not m.mm.tokens_created


async def test_approve_excludes_concurrent_deny_and_second_approve(manager, monkeypatch):
    m = manager
    request = m.ctx.pending_repo.create(
        request_id="req",
        username="demo-bot",
        display_name=None,
        description=None,
        requester_user_id="alice",
        requester_username="alice",
    )
    approver = replace(
        m.ctx,
        caller_user_id="approver-a",
        caller_username="approver-a",
        approver_ids=frozenset({"approver-a", "approver-b"}),
    )
    started, release = asyncio.Event(), asyncio.Event()
    original = m.mm.create_bot

    async def blocked_create(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(m.mm, "create_bot", blocked_create)
    approving = asyncio.create_task(dispatch(approver, f"!approve {request.request_id}"))
    await asyncio.wait_for(started.wait(), 1)
    other = replace(approver, caller_user_id="approver-b")
    try:
        assert "already" in await dispatch(other, "!deny req")
        assert "already" in await dispatch(other, "!approve req")
    finally:
        release.set()
    assert "Approved" in await approving
    assert len(m.mm.created) == 1
    assert m.ctx.pending_repo.get("req").status == "approved"
    assert m.ctx.bot_repo.get_by_owner_and_username("alice", "demo-bot") is not None
    assert m.ctx.bot_repo.list_by_owner("approver-a") == []


async def test_denied_request_cannot_be_approved(manager):
    m = manager
    m.ctx.pending_repo.create(
        request_id="req",
        username="demo-bot",
        display_name=None,
        description=None,
        requester_user_id="alice",
        requester_username="alice",
    )
    approver = replace(m.ctx, caller_user_id="approver-a", approver_ids=frozenset({"approver-a"}))
    assert "Denied" in await dispatch(approver, "!deny req")
    assert "already" in await dispatch(approver, "!approve req")
    assert not m.mm.created


async def test_approval_finalization_and_ownership_are_one_transaction(manager):
    m = manager
    m.ctx.pending_repo.create(
        request_id="req",
        username="demo-bot",
        display_name=None,
        description=None,
        requester_user_id="alice",
        requester_username="alice",
    )
    m.conn.execute(
        "CREATE TRIGGER fail_approval BEFORE UPDATE ON pending_bot_request "
        "WHEN NEW.status = 'approved' BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    approver = replace(m.ctx, caller_user_id="approver-a", approver_ids=frozenset({"approver-a"}))
    await dispatch(approver, "!approve req")
    assert m.mm.disabled == ["bot-0"]
    assert not m.ctx.bot_repo.list_by_owner("alice")
    assert m.ctx.pending_repo.get("req").status == "failed"


async def test_concurrent_duplicate_post_mints_once_and_stays_deduplicated_after_restart(manager):
    m = manager
    await create_bot(m)
    await asyncio.gather(*(handle_event(event(), m.router, m.factory) for _ in range(8)))
    assert len(m.mm.tokens_created) == 1
    second_conn = connect(m.path)
    try:
        await handle_event(event(), m.router, m.new_factory(second_conn))
    finally:
        second_conn.close()
    assert len(m.mm.tokens_created) == 1
    assert len([p for p in m.mm.posts if "secret-tok-0" in p["message"]]) == 1


async def test_missing_post_id_is_not_executed(manager):
    m = manager
    await create_bot(m)
    ev = event()
    post = json.loads(ev["data"]["post"])
    del post["id"]
    ev["data"]["post"] = post
    await handle_event(ev, m.router, m.factory)
    assert not m.mm.tokens_created


async def test_cancelled_delivery_revokes_and_marks_post_interrupted(manager, monkeypatch):
    m = manager
    await create_bot(m)

    async def cancelled_post(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(m.mm, "create_post", cancelled_post)
    with pytest.raises(asyncio.CancelledError):
        await handle_event(event(), m.router, m.factory)
    assert m.mm.tokens_revoked == ["tok-0"]
    assert m.conn.execute("SELECT status FROM processed_post").fetchone()["status"] == "interrupted"
    await handle_event(event(), m.router, m.factory)
    assert len(m.mm.tokens_created) == 1


async def test_command_body_is_not_logged(manager, capsys):
    await handle_event(
        event(message="!help synthetic-private-command-body"), manager.router, manager.factory
    )
    assert "synthetic-private-command-body" not in capsys.readouterr().out
