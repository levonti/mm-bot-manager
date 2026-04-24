import httpx
import pytest
import respx

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
async def test_revoke_token(client):
    route = respx.post("https://mm.example/api/v4/users/tokens/revoke").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await client.revoke_user_access_token("tok1")
    assert route.called
    await client.aclose()
