"""Format stored dates without relying on the process locale or timezone."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .i18n import tr


def format_datetime(value: str | None, language: str, timezone: str = "UTC") -> str:
    try:
        date = datetime.fromisoformat(value or "")
        if date.tzinfo is None:
            return tr(language, "date.unknown")
        date = date.astimezone(ZoneInfo(timezone))
    except ValueError, OverflowError:
        return tr(language, "date.unknown")
    pattern = "%d.%m.%Y %H:%M:%S" if language in {"ru", "de"} else "%Y-%m-%d %H:%M:%S"
    offset = date.strftime("%z")
    return f"{date.strftime(pattern)} (UTC{offset[:3]}:{offset[3:]})"
