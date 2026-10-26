"""Shared bot input rules, matching Mattermost's local user and bot models.

Validate only supplied fields for patches. Never normalize an identifier silently.
"""

import re
from collections.abc import Mapping

from .i18n import UserFacingError

RESTRICTED_USERNAMES = frozenset({"all", "channel", "matterbot", "system"})
TEXT_LIMITS = {"display_name": 64, "description": 1024}


class BotValidationError(UserFacingError, ValueError):
    pass


def validate_bot_fields(fields: Mapping[str, object], *, require_bot_suffix: bool = False) -> None:
    """Reject invalid input before persisting intent or performing remote writes."""
    for field in ("username", *TEXT_LIMITS):
        if field not in fields:
            continue
        value = fields[field]
        if not isinstance(value, str):
            raise BotValidationError("validation.text_required", field=field)
        if field == "username":
            if not 1 <= len(value) <= 64:
                raise BotValidationError("validation.username_length")
            for position, char in enumerate(value, start=1):
                if not re.fullmatch(r"[a-z0-9._-]", char):
                    # Report a code point, not arbitrary input or invisible formatting.
                    raise BotValidationError(
                        "validation.username_character",
                        codepoint=f"U+{ord(char):04X}",
                        position=position,
                    )
            if value in RESTRICTED_USERNAMES:
                raise BotValidationError("validation.username_reserved")
            if require_bot_suffix and not value.endswith("-bot"):
                raise BotValidationError("validation.username_suffix")
        elif len(value) > TEXT_LIMITS[field]:
            raise BotValidationError(
                "validation.text_length", field=field, limit=TEXT_LIMITS[field]
            )
