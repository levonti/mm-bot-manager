import pytest

from mm_bot_manager.api_diagnostics import response_fields, route_template


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/users/secret/tokens?page=0&secret=value", "/users/{resource}/tokens"),
        ("/users/tokens/secret", "/users/tokens/{resource}"),
        ("/bots/secret/disable", "/bots/{resource}/disable"),
        ("https://private.example/secret", "unknown"),
        ("/unknown/secret", "unknown"),
    ],
)
def test_routes_never_include_dynamic_values(path, expected):
    assert route_template(path) == expected


def test_response_identifiers_are_validated_and_header_has_priority():
    body = {"request_id": "b" * 26, "id": "api.bot.create.app_error"}
    assert response_fields(body, "a" * 26)["mattermost_request_id"] == "a" * 26
    assert response_fields(body, "invalid\nheader")["mattermost_request_id"] == "b" * 26
    assert response_fields(body, None)["mattermost_error_id"] == "api.bot.create.app_error"
    for code in ("secret", "api.secret\nforged.app_error", "api." + "a" * 200 + ".error"):
        assert "mattermost_error_id" not in response_fields({"id": code}, None)


def test_long_arbitrary_message_is_omitted():
    fields = response_fields({"message": "secret" * 10000}, None)
    assert fields == {"server_message": "[omitted]"}
