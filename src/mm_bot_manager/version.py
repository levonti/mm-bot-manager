"""Installed package version and optional container build provenance."""

import os
import re
from importlib.metadata import version


def build_info() -> tuple[str, str]:
    revision = os.environ.get("MM_BUILD_REVISION", "unknown")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        revision = "unknown"
    return version("mm-bot-manager"), revision


def version_text() -> str:
    release, revision = build_info()
    return f"mm-bot-manager {release} (revision {revision})"
