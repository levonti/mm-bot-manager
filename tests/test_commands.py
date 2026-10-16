from typing import Any

import pytest

from mm_bot_manager.commands import CommandContext, dispatch
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.mm_client import MattermostError
from mm_bot_manager.repository import BotRepo, OperationRepo, PendingRequestRepo, TokenRepo


class FakeMM:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.patched: list[dict[str, Any]] = []
        self.disabled: list[str] = []
        self.tokens_created: list[tuple[str, str]] = []
        self.tokens_revoked: list[str] = []
        self.posts: list[dict[str, str]] = []
        self.remote_tokens: dict[str, dict[str, str]] = {}
        self._next_bot_id = iter(f"bot-{i}" for i in range(100))
        self._next_token_id = iter(f"tok-{i}" for i in range(100))
        # Map method name → exception to raise on next call (consumed on use).
        self.raise_on: dict[str, Exception] = {}

    def _maybe_raise(self, method: str) -> None:
        if exc := self.raise_on.pop(method, None):
            raise exc

    async def create_bot(self, *, username, display_name=None, description=None, owner_id=None):
        self._maybe_raise("create_bot")
        self.created.append(
            {"username": username, "display_name": display_name, "description": description}
        )
        return {"user_id": next(self._next_bot_id), "username": username}

    async def patch_bot(self, bot_user_id, *, username=None, display_name=None, description=None):
        self._maybe_raise("patch_bot")
        self.patched.append(
            {
                "bot_user_id": bot_user_id,
                "username": username,
                "display_name": display_name,
                "description": description,
            }
        )
        return {}

    async def patch_user(self, user_id, *, username):
        self._maybe_raise("patch_user")
        self.patched.append({"bot_user_id": user_id, "username": username})
        return {}

    async def disable_bot(self, bot_user_id):
        self._maybe_raise("disable_bot")
        self.disabled.append(bot_user_id)

    async def create_user_access_token(self, user_id, description):
        self._maybe_raise("create_user_access_token")
        tok_id = next(self._next_token_id)
        self.tokens_created.append((user_id, description))
        self.remote_tokens[tok_id] = {"id": tok_id, "description": description}
        return {"id": tok_id, "token": f"secret-{tok_id}", "description": description}

    async def revoke_user_access_token(self, token_id):
        self._maybe_raise("revoke_user_access_token")
        self.tokens_revoked.append(token_id)
        self.remote_tokens.pop(token_id, None)

    async def list_user_access_tokens(self, user_id):
        self._maybe_raise("list_user_access_tokens")
        return list(self.remote_tokens.values())

    async def create_direct_channel(self, user_id_a, user_id_b):
        return {"id": f"dm-{user_id_a}-{user_id_b}"}

    async def create_post(self, *, channel_id, message, root_id=None):
        self._maybe_raise("create_post")
        self.posts.append({"channel_id": channel_id, "message": message})
        return {}


@pytest.fixture()
def ctx():
    conn = connect(":memory:")
    init_schema(conn)
    bot_repo = BotRepo(conn)
    token_repo = TokenRepo(conn)
    pending_repo = PendingRequestRepo(conn)
    mm = FakeMM()

    def make(
        caller_id: str = "alice-id",
        caller_name: str = "alice",
        approver_ids: frozenset[str] = frozenset(),
    ) -> CommandContext:
        return CommandContext(
            caller_user_id=caller_id,
            caller_username=caller_name,
            channel_id="chan1",
            bot_repo=bot_repo,
            token_repo=token_repo,
            pending_repo=pending_repo,
            mm=mm,  # type: ignore[arg-type]
            bot_mm=mm,  # type: ignore[arg-type]
            manager_user_id="mgr-id",
            approver_ids=approver_ids,
            operation_repo=OperationRepo(conn),
            channel_type="D",
        )

    yield make, mm, bot_repo, token_repo, pending_repo
    conn.close()


# ── dispatch edge cases ───────────────────────────────────────────────────────


async def test_help(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!help")
    assert "commands" in reply.lower()


async def test_dispatch_empty_message(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "")
    assert "commands" in reply.lower()


async def test_dispatch_bang_only(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!")
    assert "commands" in reply.lower()


async def test_dispatch_no_bang(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "hello")
    assert "must start with" in reply


async def test_dispatch_parse_error(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), '!create "unclosed')
    assert "parse" in reply.lower()


async def test_unknown_command(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!frobnicate")
    assert "Unknown command" in reply


# ── create ────────────────────────────────────────────────────────────────────


async def test_create_and_list_and_info(ctx):
    make, mm, _bot_repo, _, _pr = ctx
    reply = await dispatch(
        make(),
        '!create demo-bot --display-name "Demo Bot" --description "hi" '
        "--repo https://git.example/team/project --criticality 2",
    )
    assert "Created bot" in reply
    assert mm.created == [{"username": "demo-bot", "display_name": "Demo Bot", "description": "hi"}]

    reply = await dispatch(make(), "!list")
    assert "@demo-bot" in reply

    reply = await dispatch(make(), "!info demo-bot")
    assert "Demo Bot" in reply
    assert "hi" in reply


async def test_create_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!create")
    assert "Usage" in reply


async def test_create_bad_username_suffix(ctx):
    make, *_ = ctx
    reply = await dispatch(
        make(), "!create mybot --repo https://git.example/team/project --criticality 2"
    )
    assert "-bot" in reply


async def test_create_mm_error(ctx):
    make, mm, *_ = ctx
    mm.raise_on["create_bot"] = MattermostError(500, "internal error")
    reply = await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    assert "Could not create bot" in reply


async def test_create_mm_403(ctx):
    make, mm, *_ = ctx
    mm.raise_on["create_bot"] = MattermostError(403, "forbidden")
    reply = await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    assert "permission denied" in reply


# ── list / info ───────────────────────────────────────────────────────────────


async def test_list_empty(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!list")
    assert "don't own any bots" in reply
    assert "--repo <url> --criticality <1|2|3>" in reply


async def test_info_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!info")
    assert "Usage" in reply


async def test_info_bot_not_found(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!info ghost-bot")
    assert "not found or not owned by you" in reply


async def test_ownership_enforced_on_info(ctx):
    make, *_ = ctx
    await dispatch(
        make("alice-id", "alice"),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    reply = await dispatch(make("bob-id", "bob"), "!info demo-bot")
    assert "not found or not owned by you" in reply


# ── update ────────────────────────────────────────────────────────────────────


async def test_update(ctx):
    make, _mm, bot_repo, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    reply = await dispatch(make(), '!update demo-bot --description "new desc"')
    assert "Updated" in reply
    bot = bot_repo.get_by_owner_and_username("alice-id", "demo-bot")
    assert bot is not None
    assert bot.description == "new desc"


async def test_update_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!update")
    assert "Usage" in reply


async def test_update_bot_not_found(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!update ghost-bot --description x")
    assert "not found or not owned by you" in reply


async def test_update_no_flags(ctx):
    make, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    reply = await dispatch(make(), "!update demo-bot")
    assert "Nothing to update" in reply


async def test_update_mm_error(ctx):
    make, mm, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    mm.raise_on["patch_bot"] = MattermostError(500, "oops")
    reply = await dispatch(make(), '!update demo-bot --display-name "x"')
    assert "Could not update bot" in reply


# ── delete ────────────────────────────────────────────────────────────────────


async def test_delete(ctx):
    make, mm, bot_repo, _, _pr = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    reply = await dispatch(make(), "!delete demo-bot")
    assert "Disabled" in reply
    assert mm.disabled == ["bot-0"]
    assert bot_repo.get_by_owner_and_username("alice-id", "demo-bot") is None


async def test_delete_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!delete")
    assert "Usage" in reply


async def test_delete_bot_not_found(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!delete ghost-bot")
    assert "not found or not owned by you" in reply


async def test_delete_mm_error(ctx):
    make, mm, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    mm.raise_on["disable_bot"] = MattermostError(500, "oops")
    reply = await dispatch(make(), "!delete demo-bot")
    assert "Could not disable bot" in reply


# ── token create ──────────────────────────────────────────────────────────────


async def test_token_create_and_revoke(ctx):
    make, mm, _, token_repo, _pr = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    reply = await dispatch(make(), '!token create demo-bot --description "e2e"')
    assert "Minted token" in reply
    assert "secret-tok-0" not in reply
    assert "secret-tok-0" in mm.posts[-1]["message"]
    active = token_repo.list_active_for_bot("bot-0")
    assert len(active) == 1

    reply = await dispatch(make(), "!token revoke tok-0")
    assert "Revoked" in reply
    assert mm.tokens_revoked == ["tok-0"]
    assert token_repo.list_active_for_bot("bot-0") == []


async def test_token_create_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token create")
    assert "Usage" in reply


async def test_token_create_bot_not_found(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token create ghost-bot")
    assert "not found or not owned by you" in reply


async def test_token_create_mm_error(ctx):
    make, mm, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    mm.raise_on["create_user_access_token"] = MattermostError(500, "oops")
    reply = await dispatch(make(), "!token create demo-bot")
    assert "Could not create token" in reply


# ── token list ────────────────────────────────────────────────────────────────


async def test_token_list_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token list")
    assert "Usage" in reply


async def test_token_list_bot_not_found(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token list ghost-bot")
    assert "not found or not owned by you" in reply


async def test_token_list_no_tokens(ctx):
    make, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    reply = await dispatch(make(), "!token list demo-bot")
    assert "no active tokens" in reply


async def test_token_list_with_tokens(ctx):
    make, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    await dispatch(make(), '!token create demo-bot --description "ci"')
    reply = await dispatch(make(), "!token list demo-bot")
    assert "tok-0" in reply
    assert "ci" in reply


# ── token revoke ──────────────────────────────────────────────────────────────


async def test_token_revoke_no_args(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token revoke")
    assert "Usage" in reply


async def test_token_revoke_not_tracked(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!token revoke unknown-tok")
    assert "not tracked" in reply


async def test_token_revoke_already_revoked(ctx):
    make, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    await dispatch(make(), "!token create demo-bot")
    await dispatch(make(), "!token revoke tok-0")
    reply = await dispatch(make(), "!token revoke tok-0")
    assert "already revoked" in reply


async def test_token_revoke_mm_error(ctx):
    make, mm, *_ = ctx
    await dispatch(
        make(), "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    await dispatch(make(), "!token create demo-bot")
    mm.raise_on["revoke_user_access_token"] = MattermostError(500, "oops")
    reply = await dispatch(make(), "!token revoke tok-0")
    assert "Could not revoke token" in reply


async def test_token_revoke_rejects_foreign_owner(ctx):
    make, *_ = ctx
    await dispatch(
        make("alice-id", "alice"),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    await dispatch(make("alice-id", "alice"), "!token create demo-bot")
    reply = await dispatch(make("bob-id", "bob"), "!token revoke tok-0")
    assert "does not belong" in reply or "not tracked" in reply


# ── approve ───────────────────────────────────────────────────────────────────


async def test_approve_denied_to_non_approver(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!approve abc123")
    assert "not authorised" in reply


async def test_approve_no_args(ctx):
    make, *_ = ctx
    approvers = frozenset(["alice-id"])
    reply = await dispatch(make(approver_ids=approvers), "!approve")
    assert "Usage" in reply


async def test_approve_request_not_found(ctx):
    make, *_ = ctx
    approvers = frozenset(["alice-id"])
    reply = await dispatch(make(approver_ids=approvers), "!approve no-such-id")
    assert "not found" in reply


async def test_approve_already_resolved(ctx):
    make, _, _, _, pending_repo = ctx
    approvers = frozenset(["approver-id"])
    await dispatch(
        make(approver_ids=approvers),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    rows = pending_repo._conn.execute("SELECT request_id FROM pending_bot_request").fetchall()
    req_id = rows[0]["request_id"]
    await dispatch(make("approver-id", "approver", approver_ids=approvers), f"!approve {req_id}")
    reply = await dispatch(
        make("approver-id", "approver", approver_ids=approvers), f"!approve {req_id}"
    )
    assert "already" in reply


async def test_approve_mm_error(ctx):
    make, mm, *_ = ctx
    approvers = frozenset(["approver-id"])
    await dispatch(
        make(approver_ids=approvers),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    rows = (
        make(approver_ids=approvers)
        .pending_repo._conn.execute("SELECT request_id FROM pending_bot_request")
        .fetchall()
    )
    req_id = rows[0]["request_id"]
    mm.raise_on["create_bot"] = MattermostError(500, "oops")
    reply = await dispatch(
        make("approver-id", "approver", approver_ids=approvers), f"!approve {req_id}"
    )
    assert "Could not create bot" in reply


async def test_create_requires_approval_when_approvers_set(ctx):
    make, mm, bot_repo, _, pending_repo = ctx
    approvers = frozenset(["approver-id"])
    reply = await dispatch(
        make(approver_ids=approvers),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    assert "pending approval" in reply or "submitted for approval" in reply
    assert len(mm.created) == 0
    rows = pending_repo._conn.execute(
        "SELECT * FROM pending_bot_request WHERE username = 'demo-bot'"
    ).fetchall()
    assert len(rows) == 1
    req_id = rows[0]["request_id"]

    reply = await dispatch(
        make("approver-id", "approver", approver_ids=approvers), f"!approve {req_id}"
    )
    assert "Approved" in reply
    assert len(mm.created) == 1
    assert bot_repo.get_by_owner_and_username("alice-id", "demo-bot") is not None


# ── deny ─────────────────────────────────────────────────────────────────────


async def test_deny_flow(ctx):
    make, mm, _br, _, pending_repo = ctx
    approvers = frozenset(["approver-id"])
    await dispatch(
        make(approver_ids=approvers),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    rows = pending_repo._conn.execute("SELECT request_id FROM pending_bot_request").fetchall()
    req_id = rows[0]["request_id"]

    reply = await dispatch(
        make("approver-id", "approver", approver_ids=approvers),
        f"!deny {req_id} not needed",
    )
    assert "Denied" in reply
    assert len(mm.created) == 0
    row = pending_repo._conn.execute(
        "SELECT status, deny_reason FROM pending_bot_request WHERE request_id = ?",
        (req_id,),
    ).fetchone()
    assert row["status"] == "denied"
    assert row["deny_reason"] == "not needed"


async def test_deny_not_authorised(ctx):
    make, *_ = ctx
    reply = await dispatch(make(), "!deny abc123")
    assert "not authorised" in reply


async def test_deny_no_args(ctx):
    make, *_ = ctx
    approvers = frozenset(["alice-id"])
    reply = await dispatch(make(approver_ids=approvers), "!deny")
    assert "Usage" in reply


async def test_deny_request_not_found(ctx):
    make, *_ = ctx
    approvers = frozenset(["alice-id"])
    reply = await dispatch(make(approver_ids=approvers), "!deny no-such-id")
    assert "not found" in reply


async def test_deny_already_resolved(ctx):
    make, *_ = ctx
    approvers = frozenset(["approver-id"])
    await dispatch(
        make(approver_ids=approvers),
        "!create demo-bot --repo https://git.example/team/project --criticality 2",
    )
    rows = (
        make(approver_ids=approvers)
        .pending_repo._conn.execute("SELECT request_id FROM pending_bot_request")
        .fetchall()
    )
    req_id = rows[0]["request_id"]
    await dispatch(make("approver-id", "approver", approver_ids=approvers), f"!deny {req_id}")
    reply = await dispatch(
        make("approver-id", "approver", approver_ids=approvers), f"!deny {req_id}"
    )
    assert "already" in reply
