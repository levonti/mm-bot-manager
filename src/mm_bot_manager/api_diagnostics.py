"""Allowlisted API diagnostics; never serialize arbitrary remote error bodies."""

import re
from typing import Any

_ROUTES = (
    r"/users/me",
    r"/users/username/[^/]+",
    r"/users/tokens/revoke",
    r"/users/tokens/[^/]+",
    r"/users/[^/]+/tokens",
    r"/users/[^/]+/patch",
    r"/users/[^/]+",
    r"/bots",
    r"/bots/[^/]+/disable",
    r"/bots/[^/]+",
    r"/channels/direct",
    r"/posts",
)
_SAFE_MESSAGES = frozenset(
    {
        "Bad request.",
        "Invalid request body.",
        "Invalid username.",
        "Invalid or missing username in request body.",
        "Username already exists.",
        "Bot accounts are not enabled.",
        "Internal server error.",
    }
)


def route_template(path: str) -> str:
    path = path.partition("?")[0]
    for pattern in _ROUTES:
        if re.fullmatch(pattern, path):
            return pattern.replace("[^/]+", "{resource}")
    return "unknown"


def response_fields(payload: Any, request_id: str | None) -> dict[str, str]:
    fields = {}
    body = payload if isinstance(payload, dict) else {}
    code = body.get("id")
    if (
        isinstance(code, str)
        and len(code) <= 160
        and re.fullmatch(
            r"(?:api|app|model|store|web)\.(?:[a-z][a-z0-9_]*\.)+[a-z][a-z0-9_]*", code
        )
    ):
        fields["mattermost_error_id"] = code
    for candidate in (request_id, body.get("request_id")):
        if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9]{26}", candidate):
            fields["mattermost_request_id"] = candidate
            break
    message = body.get("message")
    fields["server_message"] = (
        message if isinstance(message, str) and message in _SAFE_MESSAGES else "[omitted]"
    )
    return fields
