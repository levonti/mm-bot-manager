# ruff: noqa: RUF001 -- Russian UI assertions intentionally use Cyrillic.
import asyncio
import json
from dataclasses import replace
from itertools import count

import pytest

from mm_bot_manager.commands import CommandContext, dispatch
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.dispatcher import CommandContextFactory, MessageRouter, handle_event
from mm_bot_manager.feedback import FeedbackRepo
from mm_bot_manager.feedback_delivery import deliver_once, run_delivery
from mm_bot_manager.i18n import UserFacingError, validate_catalogs
from mm_bot_manager.repository import (
    BotRepo,
    OperationRepo,
    PendingRequestRepo,
    PreferenceRepo,
    TokenRepo,
)
from tests.test_commands import FakeMM

HANDLERS = frozenset({"support", "backup"})


def feedback_context(conn, mm=None):
    mm = mm or FakeMM()
    return CommandContext(
        caller_user_id="alice",
        caller_username="alice",
        channel_id="dm-alice",
        bot_repo=BotRepo(conn),
        token_repo=TokenRepo(conn),
        pending_repo=PendingRequestRepo(conn),
        mm=mm,
        bot_mm=mm,
        manager_user_id="manager",
        approver_ids=frozenset(),
        operation_repo=OperationRepo(conn),
        channel_type="D",
        feedback_handler_ids=HANDLERS,
        display_timezone="Europe/Moscow",
    )


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "feedback.sqlite")
    conn = connect(path)
    init_schema(conn)
    ctx = feedback_context(conn)
    yield ctx, FeedbackRepo(conn), path
    conn.close()


@pytest.fixture()
def command(env):
    ctx, _, _ = env
    ids = count()

    async def run(text, actor="alice", **overrides):
        return await dispatch(
            replace(
                ctx,
                caller_user_id=actor,
                caller_username=actor,
                post_id=f"post-{next(ids)}",
                **overrides,
            ),
            text,
        )

    return run


async def test_full_ticket_conversation_lifecycle(env, command):
    ctx, repo, _ = env
    assert "FB-1" in await command("!feedback new Missing notification")
    ticket = repo.get(1)
    assert (ticket.status, ticket.author_id, ticket.author_name) == ("new", "alice", "alice")
    assert "FB-1" in await command("!feedback queue", "support")
    await command("!feedback take FB-1", "support")
    assert repo.get(1).assignee_id == "support"
    await command("!feedback set-status FB-1 waiting Which operation?", "support")
    assert repo.get(1).status == "waiting"
    await command("!feedback reply FB-1 Updating the bot")
    assert repo.get(1).status == "in_progress"
    await command("!feedback reply FB-1 Investigating", "support")
    await command("!feedback set-status FB-1 resolved Fixed notification", "support")
    assert repo.get(1).status == "resolved"
    assert "Reopen" in await command("!feedback reply FB-1 Still broken")
    assert repo.get(1).version == 6
    await command("!feedback close FB-1 Confirmed fixed")
    assert repo.get(1).status == "closed"
    await command("!feedback reopen FB-1 Reproduced again")
    assert (repo.get(1).status, repo.get(1).assignee_id) == ("new", None)
    assert repo.get(1).assignee_name is None
    assert [event.version for event in repo.history(1, 0, 100)] == list(range(1, 9))
    assert "Reproduced again" in await command("!feedback status FB-1")
    assert "Missing notification" in await command("!feedback show FB-1")
    assert "!feedback show FB-1 2" in await command("!feedback show FB-1")
    assert "Which operation?" in await command("!feedback show FB-1 3")
    assert "UTC+03:00" in await command("!feedback status FB-1")
    validate_catalogs()
    # Author language changes do not change the handler's notification language.
    PreferenceRepo(ctx.bot_repo.conn).language("alice", "ru")
    PreferenceRepo(ctx.bot_repo.conn).language("support", "en")
    while repo.due():
        await deliver_once(ctx)
    author_posts = [p["message"] for p in ctx.bot_mm.posts if p["channel_id"].endswith("-alice")]
    handler_posts = [p["message"] for p in ctx.bot_mm.posts if p["channel_id"].endswith("-support")]
    assert author_posts and all("событие" in p for p in author_posts)
    assert handler_posts and all("event #" in p for p in handler_posts)


@pytest.mark.parametrize(
    "action",
    [
        "status",
        "show",
        "reply",
        "close",
        "reopen",
        "take",
        "set-status",
    ],
)
async def test_ticket_authorization_does_not_disclose_existence(command, env, action):
    await command("!feedback new Private text")
    foreign = await command(f"!feedback {action} FB-1 details", "bob")
    missing = await command(f"!feedback {action} FB-999 details", "bob")
    assert foreign == missing == "Ticket not found or unavailable."
    assert env[1].get(1).version == 1
    assert "FB-1" not in await command("!feedback list", "bob")


@pytest.mark.parametrize("channel", ["O", "P", "G", None])
async def test_all_commands_are_dm_only_and_have_no_side_effects(env, command, channel):
    for cmd in ("new Secret", "queue", "status FB-1", "reply FB-1 Secret", "help"):
        assert "direct message" in await command(f"!feedback {cmd}", channel_type=channel)
    assert env[1].listing("alice", 0) == []
    assert env[1].due() == []


async def test_feedback_roles_are_separate_from_approvers_and_catalog_readers(command, env):
    await command("!feedback new Example")
    assert "only to feedback handlers" in await command(
        "!feedback queue",
        "approver",
        approver_ids=frozenset({"approver"}),
        catalog_viewer_ids=frozenset({"approver"}),
    )
    assert "only to feedback handlers" in await command("!feedback take FB-1")
    assert "only to feedback handlers" in await command("!feedback set-status FB-1 resolved Done")
    assert env[1].get(1).version == 1


async def test_disabled_feature_and_files_are_rejected(command, env):
    assert "not configured" in await command(
        "!feedback new Example", feedback_handler_ids=frozenset()
    )
    assert "Attachments" in await command("!feedback new Example", file_ids=("file",))
    assert env[1].listing("alice", 0) == []


async def test_free_text_preserves_quotes_flags_newlines_and_non_ascii(command, env):
    text = "Не работает \"импорт\n--repo и apostrophe's [link](https://example.com)"
    assert "FB-1" in await command("!feedback\tnew\n" + text)
    assert env[1].latest(1).body == text
    await command("!feedback reply FB-1 " + text)
    assert env[1].latest(1).body == text
    assert "Could not parse" not in await command("!feedback show FB-1 2")


@pytest.mark.parametrize("text", ["", " ", "x" * 3001, "a\x00b"])
async def test_body_validation(command, env, text):
    assert "1–3000" in await command("!feedback new " + text)
    assert env[1].listing("alice", 0) == []


@pytest.mark.parametrize(
    "tail",
    [
        "unknown",
        "status",
        "status nonsense",
        "status FB-1 unexpected",
        "take FB-1 extra",
        "show FB-1 0",
        "show FB-1 -1",
        "show FB-1 1000000",
        "list --page",
        "list 0",
        "set-status FB-1 new reason",
        "set-status FB-1 waiting",
        "help extra",
        "status FB-99999999999999999999999999999",
    ],
)
async def test_invalid_syntax_does_not_mutate(command, env, tail):
    await command("!feedback new Example")
    assert "Feedback commands" in await command("!feedback " + tail, "support")
    assert env[1].get(1).version == 1


async def test_pagination_is_bounded_and_queue_orders_unassigned_first(command, env):
    for i in range(12):
        await command(f"!feedback new Ticket {i}")
    await command("!feedback take FB-1", "support")
    listing = await command("!feedback list")
    assert "!feedback list 2" in listing
    assert "FB-12" in listing and "FB-1 ·" not in listing
    assert "FB-1 ·" in await command("!feedback list 2")
    queue = await command("!feedback queue", "support")
    assert queue.index("FB-2 ·") < queue.index("FB-3 ·")
    assert "FB-1 ·" not in queue
    assert "No entries" in await command("!feedback list 99")
    assert "No entries" in await command("!feedback show FB-1 99")
    await command("!feedback close FB-2 Withdrawn")
    assert "FB-2 ·" not in await command("!feedback queue", "support")


async def test_idempotent_mutations_do_not_duplicate_history_or_outbox(env):
    ctx, repo, _ = env
    request = replace(ctx, post_id="same-new")
    await dispatch(request, "!feedback new Example")
    await dispatch(request, "!feedback new Example")
    assert len(repo.listing("alice", 0)) == 1
    request = replace(ctx, post_id="same-reply")
    await dispatch(request, "!feedback reply FB-1 More context")
    await dispatch(request, "!feedback reply FB-1 More context")
    assert repo.get(1).version == 2
    assert ctx.bot_repo.conn.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 4


async def test_mutation_rolls_back_ticket_history_and_outbox(env, command, monkeypatch):
    ctx, repo, _ = env
    await command("!feedback new Example")
    original = FeedbackRepo._record

    def broken(self, *args):
        original(self, *args)
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(FeedbackRepo, "_record", broken)
    with pytest.raises(RuntimeError):
        await command("!feedback reply FB-1 Should roll back")
    with pytest.raises(RuntimeError):
        await command("!feedback new Should roll back")
    assert repo.get(1).version == 1
    assert len(repo.listing("alice", 0)) == 1
    assert len(repo.history(1, 0, 100)) == 1
    assert ctx.bot_repo.conn.execute("SELECT COUNT(*) FROM feedback_outbox").fetchone()[0] == 2


async def test_optimistic_lock_and_assignment_rules(env, command):
    _, repo, _ = env
    await command("!feedback new Example")
    stale = repo.get(1)
    await command("!feedback take FB-1", "support")
    with pytest.raises(UserFacingError, match="changed"):
        repo.change(stale, "take", "backup", "", "another-take", HANDLERS)
    assert "changed" in await command("!feedback take FB-1", "backup")
    assert "changed" in await command("!feedback set-status FB-1 waiting Question", "backup")
    await command("!feedback close FB-1 Duplicate", "backup")
    assert "changed" in await command("!feedback take FB-1", "support")
    assert "changed" in await command("!feedback close FB-1 Again")
    await command("!feedback reopen FB-1 Recheck")
    assert "changed" in await command("!feedback reopen FB-1 Already open")
    # Removing a handler does not leave a ticket permanently unassignable.
    await command("!feedback take FB-1", "support")
    await command("!feedback take FB-1", "backup", feedback_handler_ids=frozenset({"backup"}))
    assert repo.get(1).assignee_id == "backup"


async def test_delivery_survives_restart_and_preserves_order_per_recipient(env, command):
    ctx, repo, path = env
    await command("!feedback new First")
    await command("!feedback reply FB-1 Second")
    ctx.bot_mm.raise_on["create_post"] = RuntimeError("do not log secret response")
    await deliver_once(ctx)
    rows = ctx.bot_repo.conn.execute(
        "SELECT * FROM feedback_outbox WHERE state = 'pending' AND attempts = 1"
    ).fetchall()
    assert len(rows) == 1
    blocked_recipient = rows[0]["recipient_id"]
    assert not any(row["recipient_id"] == blocked_recipient for row in repo.due())
    # A fresh connection simulates restart; persisted notification is retried.
    reopened = connect(path)
    try:
        init_schema(reopened)
        resumed = feedback_context(reopened)
        reopened.execute("UPDATE feedback_outbox SET next_attempt_at = '2000-01-01'")
        resumed_repo = FeedbackRepo(reopened)
        while resumed_repo.due():
            await deliver_once(resumed)
        messages = [
            p["message"]
            for p in resumed.bot_mm.posts
            if p["channel_id"].endswith("-" + blocked_recipient)
        ]
        assert len(messages) == 2
        assert "First" in messages[0] and "Second" in messages[1]
        assert (
            reopened.execute(
                "SELECT COUNT(*) FROM feedback_outbox WHERE state = 'pending'"
            ).fetchone()[0]
            == 0
        )
    finally:
        reopened.close()


async def test_pending_notifications_do_not_leak_to_removed_handlers(env, command):
    ctx, repo, _ = env
    await command("!feedback new Private")
    await deliver_once(replace(ctx, feedback_handler_ids=frozenset({"backup"})))
    assert all(p["channel_id"].endswith("-backup") for p in ctx.bot_mm.posts)
    assert (
        ctx.bot_repo.conn.execute(
            "SELECT state FROM feedback_outbox WHERE recipient_id = 'support'"
        ).fetchone()[0]
        == "skipped"
    )
    assert repo.due() == []


@pytest.mark.parametrize("character", ["&", "<", "@", "\n", "\\", "`"])
async def test_maximum_body_is_safe_and_fits_one_mattermost_post(env, command, character):
    text = (character * 2998) + "x"
    await command("!feedback new " + text)
    shown = await command("!feedback show FB-1")
    status = await command("!feedback status FB-1")
    await deliver_once(env[0])
    assert max(len(shown), len(status), *(len(p["message"]) for p in env[0].bot_mm.posts)) < 16383


async def test_mentions_and_markdown_are_neutralized(command, env):
    await command("!feedback new @all @channel <@user> ![image](https://example.com) `code`")
    shown = await command("!feedback show FB-1")
    assert "@all" not in shown and "@channel" not in shown and "<@user>" not in shown
    assert "![image]" not in shown
    assert env[1].latest(1).body.startswith("@all")


async def test_feedback_help_is_role_aware(command):
    assert "!feedback new" in await command("!help")
    assert "!feedback queue" not in await command("!feedback")
    assert "!feedback queue" in await command("!feedback", "support")
    assert "!feedback" not in await command("!help", feedback_handler_ids=frozenset())
    assert "!feedback" not in await command("!help", channel_type="O")


async def test_dispatcher_routes_feedback_and_deduplicates_post(env):
    ctx, repo, _ = env
    factory = CommandContextFactory(
        ctx.bot_repo,
        ctx.token_repo,
        ctx.pending_repo,
        ctx.mm,
        ctx.bot_mm,
        ctx.manager_user_id,
        frozenset(),
        feedback_handler_ids=HANDLERS,
    )
    post = {
        "id": "websocket-post",
        "user_id": "alice",
        "channel_id": "dm",
        "message": '!feedback new "unclosed\n--literal',
    }
    event = {
        "event": "posted",
        "data": {"channel_type": "D", "sender_name": "@alice", "post": json.dumps(post)},
    }
    router = MessageRouter("manager", "manager")
    await handle_event(event, router, factory)
    await handle_event(event, router, factory)
    assert repo.latest(1).body == '"unclosed\n--literal'
    assert len(ctx.bot_mm.posts) == 1
    assert repo.get(1).version == 1


async def test_background_delivery_retries_loop_errors_and_cancels(env, monkeypatch):
    calls = 0
    reached = asyncio.Event()

    async def fail_then_succeed(ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database temporarily unavailable")
        reached.set()

    monkeypatch.setattr("mm_bot_manager.feedback_delivery.deliver_once", fail_then_succeed)
    task = asyncio.create_task(run_delivery(env[0]))
    try:
        await asyncio.wait_for(reached.wait(), timeout=3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls >= 2


def test_feedback_migration_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "old.sqlite"))
    try:
        init_schema(conn)
        ticket = FeedbackRepo(conn).create("alice", "Keep me", "old-post", HANDLERS)
        original = conn.execute

        def fail(sql, params=()):
            if "idx_feedback_delivery" in sql:
                raise RuntimeError("migration interrupted")
            return original(sql, params)

        monkeypatch.setattr(conn, "execute", fail)
        with pytest.raises(RuntimeError):
            init_schema(conn)
        monkeypatch.setattr(conn, "execute", original)
        init_schema(conn)
        assert FeedbackRepo(conn).get(ticket.number) == ticket
        assert [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migration ORDER BY version")
        ] == [1, 2]
    finally:
        conn.close()
