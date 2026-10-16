"""Validated inventory attributes. Repository URLs are never fetched or authenticated."""

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


class MetadataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BotMetadata:
    git_repository: str
    criticality: int


def validate_criticality(criticality: str | int | None) -> int:
    if isinstance(criticality, bool) or str(criticality) not in {"1", "2", "3"}:
        raise MetadataError("Criticality must be 1 (critical), 2 (important), or 3 (auxiliary).")
    return int(str(criticality))


def validate_metadata(repository: str | None, criticality: str | int | None) -> BotMetadata:
    if not repository or not isinstance(repository, str):
        raise MetadataError("Both --repo and --criticality are required.")
    repository = repository.strip()
    if len(repository) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", unquote(repository)):
        raise MetadataError("Invalid repository URL; use HTTPS or SSH without credentials.")
    value = validate_criticality(criticality)
    try:
        if repository.startswith(("https://", "ssh://")):
            parts = urlsplit(repository)
            if (
                not parts.hostname
                or not re.fullmatch(r"[a-zA-Z0-9.-]+", parts.hostname)
                or parts.password is not None
                or (parts.scheme == "https" and parts.username is not None)
                or (parts.scheme == "ssh" and parts.username not in {None, "git"})
                or parts.query
                or parts.fragment
                or not parts.path.strip("/")
                or (parts.port is not None and not 1 <= parts.port <= 65535)
            ):
                raise ValueError
        elif not re.fullmatch(r"git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./~-]+", repository):
            raise ValueError
        if any(part in {".", ".."} for part in unquote(repository).split("/")):
            raise ValueError
    except ValueError:
        # Do not echo a rejected URL: it might contain a token or password.
        raise MetadataError(
            "Use an HTTPS or SSH repository URL without credentials/query/fragment."
        ) from None
    return BotMetadata(repository, value)


def cell(value: object, limit: int = 90) -> str:
    """Bound and escape untrusted text before inserting it into a chat table."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    for char in ("\\", "|", "`", "[", "]", "*", "_", "<", ">", "@"):
        text = text.replace(char, "\\" + char)
    return text
