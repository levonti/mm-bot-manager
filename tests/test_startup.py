import json

import httpx
import pytest
import respx

from mm_bot_manager import __main__ as app
from mm_bot_manager.config import Settings
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.repository import BotRepo, OperationRepo, TokenRepo


def settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        mm_url="https://mm.example",
        mm_token="test-admin",
        mm_bot_token="test-manager",
        db_path=str(tmp_path / "db"),
        **overrides,
    )


@respx.mock
async def test_startup_recovers_before_consuming_events_and_uses_separate_clients(
    tmp_path, monkeypatch
):
    cfg = settings(tmp_path)
    cfg.health_port = 0  # Let the test listener select an ephemeral port.
    conn = connect(cfg.db_path)
    init_schema(conn)
    BotRepo(conn).insert(
        bot_user_id="bot-0",
        username="demo-bot",
        display_name=None,
        description=None,
        owner_user_id="alice",
        owner_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    TokenRepo(conn).record(token_id="abandoned-token", bot_user_id="bot-0", description="test")
    operations = OperationRepo(conn)
    op = operations.start("token_create", "bot-0")
    operations.update(op, "recorded", "abandoned-token")
    me = respx.get("https://mm.example/api/v4/users/me").mock(
        return_value=httpx.Response(200, json={"id": "mgr", "username": "manager"})
    )
    revoke = respx.post("https://mm.example/api/v4/users/tokens/revoke").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    post = respx.post("https://mm.example/api/v4/posts").mock(
        return_value=httpx.Response(201, json={"id": "reply"})
    )

    async def stream(url, token, **kwargs):
        assert revoke.called
        assert not operations.unfinished()
        assert token == "test-manager"
        yield {
            "event": "posted",
            "data": {
                "channel_type": "D",
                "sender_name": "@alice",
                "post": json.dumps(
                    {"id": "p1", "user_id": "alice", "channel_id": "dm", "message": "!help"}
                ),
            },
        }

    monkeypatch.setattr(app, "stream_events", stream)
    try:
        await app.serve(cfg, conn)
        assert me.calls.last.request.headers["Authorization"] == "Bearer test-manager"
        assert revoke.calls.last.request.headers["Authorization"] == "Bearer test-admin"
        assert post.calls.last.request.headers["Authorization"] == "Bearer test-manager"
    finally:
        conn.close()


async def test_main_closes_database_and_releases_lock_on_failure(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    conn = connect(cfg.db_path)
    monkeypatch.setattr(app, "load_settings", lambda: cfg)
    monkeypatch.setattr(app, "connect", lambda path: conn)

    async def fail(*args):
        raise RuntimeError("startup failure")

    monkeypatch.setattr(app, "serve", fail)
    with pytest.raises(RuntimeError, match="startup failure"):
        await app.main_async()
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")
    # Another startup can take the same file lock after the failure.
    other = connect(cfg.db_path)
    try:
        with app.instance_lock(other, cfg.db_path):
            pass
    finally:
        other.close()


@respx.mock
async def test_approvers_resolved_to_ids_and_invalid_configuration_fails_closed():
    from mm_bot_manager.mm_client import MattermostClient

    respx.get("https://mm.example/api/v4/users/username/alice").mock(
        return_value=httpx.Response(200, json={"id": "alice-id"})
    )
    respx.get("https://mm.example/api/v4/users/username/missing").mock(
        return_value=httpx.Response(404, json={"message": "secret-response"})
    )
    async with MattermostClient("https://mm.example/api/v4", "test") as client:
        assert await app._resolve_approvers(client, ["alice"]) == frozenset({"alice-id"})
        with pytest.raises(SystemExit) as exc:
            await app._resolve_approvers(client, ["missing"])
        assert "secret-response" not in str(exc.value)


def test_configuration_bounds_and_repr_hide_credentials(tmp_path):
    cfg = settings(tmp_path)
    assert "test-admin" not in repr(cfg)
    assert "test-manager" not in repr(cfg)
    with pytest.raises(ValueError):
        settings(tmp_path, max_workers=0)
    with pytest.raises(ValueError):
        settings(tmp_path, event_queue_size=0)
    with pytest.raises(ValueError):
        settings(tmp_path, shutdown_timeout=0)
    assert cfg.mm_ws_url == "wss://mm.example/api/v4/websocket"
    assert cfg.mm_rest_base == "https://mm.example/api/v4"


@respx.mock
async def test_catalog_readers_resolve_to_ids_and_receive_catalog_through_dispatcher(
    tmp_path, monkeypatch
):
    cfg = settings(tmp_path, mm_catalog_viewers="reader")
    cfg.health_port = 0
    conn = connect(cfg.db_path)
    init_schema(conn)
    BotRepo(conn).insert(
        bot_user_id="registered",
        username="demo-bot",
        display_name=None,
        description="alerts",
        owner_user_id="owner",
        owner_username="owner",
        git_repository="https://git.example/team/repo",
        criticality=1,
    )
    respx.get("https://mm.example/api/v4/users/me").mock(
        return_value=httpx.Response(200, json={"id": "mgr", "username": "manager"})
    )
    reader = respx.get("https://mm.example/api/v4/users/username/reader").mock(
        return_value=httpx.Response(200, json={"id": "reader-id", "username": "reader"})
    )
    post = respx.post("https://mm.example/api/v4/posts").mock(
        return_value=httpx.Response(201, json={"id": "reply"})
    )

    async def stream(*args, **kwargs):
        yield {
            "event": "posted",
            "data": {
                "channel_type": "D",
                "sender_name": "@reader",
                "post": json.dumps(
                    {
                        "id": "reader-post",
                        "user_id": "reader-id",
                        "channel_id": "dm",
                        "message": "!catalog",
                    }
                ),
            },
        }

    monkeypatch.setattr(app, "stream_events", stream)
    try:
        await app.serve(cfg, conn)
        assert reader.calls.last.request.headers["authorization"] == "Bearer test-admin"
        message = json.loads(post.calls.last.request.content)["message"]
        assert "Bot catalog" in message and "demo-bot" in message
        assert "https://git.example/team/repo" in message
    finally:
        conn.close()


@respx.mock
async def test_startup_automatically_upgrades_previous_release_before_events(tmp_path, monkeypatch):
    from mm_bot_manager.repository import PendingRequestRepo
    from tests.test_imports import BOT
    from tests.test_updates import install_previous_schema, seed_previous_version

    cfg = settings(tmp_path, mm_display_timezone="Europe/Moscow")
    cfg.health_port = 0
    conn = connect(cfg.db_path)
    install_previous_schema(conn)
    seed_previous_version(conn)
    respx.get("https://mm.example/api/v4/users/me").mock(
        return_value=httpx.Response(200, json={"id": "mgr", "username": "manager"})
    )
    post = respx.post("https://mm.example/api/v4/posts").mock(
        return_value=httpx.Response(201, json={"id": "reply"})
    )
    conn.execute("UPDATE bot SET created_at = '2026-10-22T11:35:08+00:00'")

    async def stream(*args, **kwargs):
        assert conn.execute("SELECT version FROM schema_migration").fetchone()[0] == 1
        assert BotRepo(conn).get(BOT).revision == 0
        assert PendingRequestRepo(conn).get("old-pending").status == "pending"
        assert TokenRepo(conn).get("keep-token").revoked_at is None
        yield {
            "event": "posted",
            "data": {
                "channel_type": "D",
                "sender_name": "@alice",
                "post": json.dumps(
                    {
                        "id": "info-1",
                        "user_id": "a" * 26,
                        "channel_id": "dm",
                        "message": "!info legacy",
                    }
                ),
            },
        }

    monkeypatch.setattr(app, "stream_events", stream)
    try:
        await app.serve(cfg, conn)
        body = json.loads(post.calls.last.request.content)["message"]
        assert "22.10.2026 14:35:08 (UTC+03:00)" in body
    finally:
        conn.close()
