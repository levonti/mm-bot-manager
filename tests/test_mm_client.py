import httpx
import pytest
import respx

from mm_bot_manager.i18n import error_text
from mm_bot_manager.logging import configure_logging
from mm_bot_manager.mm_client import MattermostClient, MattermostError


@pytest.fixture()
def client():
    return MattermostClient("https://mm.example/api/v4", "test-token")


@respx.mock
async def test_get_me(client):
    respx.get("https://mm.example/api/v4/users/me").mock(
        return_value=httpx.Response(200, json={"id": "abc", "username": "manager"})
    )
    me = await client.get_me()
    assert me["id"] == "abc"
    await client.aclose()


@respx.mock
async def test_create_bot(client):
    route = respx.post("https://mm.example/api/v4/bots").mock(
        return_value=httpx.Response(201, json={"user_id": "uid1", "username": "demo"})
    )
    result = await client.create_bot(username="demo", display_name="Demo", description="d")
    assert result["user_id"] == "uid1"
    assert route.calls.last.request.headers["authorization"] == "Bearer test-token"
    await client.aclose()


@respx.mock
async def test_error_surface(client):
    respx.post("https://mm.example/api/v4/bots").mock(
        return_value=httpx.Response(400, json={"message": "bad username"})
    )
    with pytest.raises(MattermostError) as exc:
        await client.create_bot(username="bad")
    assert exc.value.status == 400
    assert "bad username" in str(exc.value)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [400, 403, 500])
async def test_api_error_is_logged_at_info_and_correlates_with_reply(client, capsys, status):
    configure_logging("INFO")
    request_id = "a" * 26
    respx.post("https://mm.example/api/v4/bots").mock(
        return_value=httpx.Response(
            status,
            headers={"X-Request-ID": request_id},
            json={"id": "model.bot.is_valid.username.app_error", "message": "Invalid username."},
        )
    )
    try:
        with pytest.raises(MattermostError) as caught:
            await client.create_bot(username="private-username")
        captured = capsys.readouterr()
        assert not captured.out
        output = captured.err
        error = caught.value
        assert error.diagnostic_id and error.diagnostic_id in output
        for language in ("ru", "en"):
            assert error.diagnostic_id in error_text(error, language)
            assert "Invalid username." not in error_text(error, language)
        for expected in (
            "mattermost_api_error",
            "POST",
            "/bots",
            str(status),
            "elapsed_ms",
            "model.bot.is_valid.username.app_error",
            request_id,
            "Invalid username.",
        ):
            assert expected in output
        assert ("error" if status >= 500 else "warning") in output
        assert "private-username" not in output
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"message": "Bearer test-token https://private.example/secret", "detail": "secret"},
        {"message": {"token": "secret"}, "id": "secret\nforged", "request_id": "secret"},
        ["secret"],
        "secret",
        None,
        42,
    ],
)
async def test_untrusted_error_payload_is_not_logged(client, capsys, body):
    configure_logging("INFO")
    respx.post("https://mm.example/api/v4/posts").mock(
        return_value=httpx.Response(400, json=body, headers={"X-Request-ID": "secret"})
    )
    try:
        with pytest.raises(MattermostError):
            await client.create_post(channel_id="secret", message="secret test-token")
        output = capsys.readouterr().err
        assert "mattermost_api_error" in output
        assert "omitted" in output
        for forbidden in ("secret", "test-token", "private.example", "Bearer", "forged"):
            assert forbidden not in output
    finally:
        await client.aclose()


@respx.mock
async def test_non_json_error_and_dynamic_route_are_safe(client, capsys):
    configure_logging("INFO")
    respx.get("https://mm.example/api/v4/users/username/secret").mock(
        return_value=httpx.Response(502, text="<html>secret test-token</html>")
    )
    try:
        with pytest.raises(MattermostError):
            await client.get_user_by_username("secret")
        output = capsys.readouterr().err
        assert "/users/username/{resource}" in output
        assert "secret" not in output and "test-token" not in output
    finally:
        await client.aclose()


@respx.mock
async def test_revoke_token(client):
    route = respx.post("https://mm.example/api/v4/users/tokens/revoke").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await client.revoke_user_access_token("tok1")
    assert route.called
    await client.aclose()


@respx.mock
async def test_token_listing_paginates_without_returning_token_values(client):
    first = [{"id": f"t-{i}", "description": "integration"} for i in range(200)]
    page0 = respx.get("https://mm.example/api/v4/users/bot/tokens?page=0&per_page=200").mock(
        return_value=httpx.Response(200, json=first)
    )
    page1 = respx.get("https://mm.example/api/v4/users/bot/tokens?page=1&per_page=200").mock(
        return_value=httpx.Response(200, json=[{"id": "last", "description": "integration"}])
    )
    try:
        tokens = await client.list_user_access_tokens("bot")
        assert len(tokens) == 201
        assert tokens[-1]["id"] == "last"
        assert page0.call_count == page1.call_count == 1
    finally:
        await client.aclose()


@respx.mock
async def test_repeated_revoke_only_accepts_verified_absence(client):
    respx.post("https://mm.example/api/v4/users/tokens/revoke").mock(
        return_value=httpx.Response(400, json={"message": "not found"})
    )
    respx.get("https://mm.example/api/v4/users/tokens/tok1").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    try:
        await client.revoke_user_access_token("tok1")
    finally:
        await client.aclose()


@respx.mock
async def test_revoke_bad_request_does_not_hide_existing_active_token(client):
    respx.post("https://mm.example/api/v4/users/tokens/revoke").mock(
        return_value=httpx.Response(400, json={"message": "bad request"})
    )
    respx.get("https://mm.example/api/v4/users/tokens/tok1").mock(
        return_value=httpx.Response(200, json={"id": "tok1", "is_active": True})
    )
    try:
        with pytest.raises(MattermostError):
            await client.revoke_user_access_token("tok1")
    finally:
        await client.aclose()
