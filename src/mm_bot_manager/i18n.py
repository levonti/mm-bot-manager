"""Explicit message catalogs; machine identifiers and user data are never translated."""

import json
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from string import Formatter
from typing import Any

SUPPORTED_LANGUAGES = frozenset({"en", "ru"})


@cache
def catalog(language: str) -> dict[str, str]:
    return json.loads(  # type: ignore[no-any-return]
        files("mm_bot_manager").joinpath("locales", f"{language}.json").read_text(encoding="utf-8")
    )


def normalize_language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    base = value.lower().replace("_", "-").split("-", 1)[0]
    return base if base in SUPPORTED_LANGUAGES else None


@dataclass(frozen=True, slots=True)
class Message:
    key: str
    params: dict[str, Any] = field(default_factory=dict)

    def render(self, language: str) -> str:
        return tr(language, self.key, **self.params)


def tr(language: str, key: str, **params: Any) -> str:
    locale = normalize_language(language) or "en"
    template = catalog(locale).get(key, catalog("en").get(key, key))
    values = {k: v.render(locale) if isinstance(v, Message) else v for k, v in params.items()}
    return template.format(**values)


class UserFacingError(Exception):
    def __init__(self, key: str, **params: Any) -> None:
        self.key = key
        self.params = params
        super().__init__(tr("en", key, **params))


def error_text(error: BaseException, language: str) -> str:
    from .mm_client import MattermostError

    if isinstance(error, UserFacingError) and error.key in catalog("en"):
        return tr(language, error.key, **error.params)
    if isinstance(error, MattermostError):
        if error.diagnostic_id:
            return tr(
                language,
                "error.api_diagnostic",
                status=error.status,
                diagnostic_id=error.diagnostic_id,
            )
        return tr(language, "error.api", status=error.status)
    return tr(language, "error.unexpected")


def plural(language: str, count: int, key: str) -> str:
    if normalize_language(language) == "ru":
        form = (
            "one"
            if count % 10 == 1 and count % 100 != 11
            else ("few" if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14 else "many")
        )
    else:
        form = "one" if count == 1 else "many"
    return tr(language, f"{key}.{form}", count=count)


def validate_catalogs() -> None:
    english = catalog("en")
    for language in SUPPORTED_LANGUAGES:
        translated = catalog(language)
        if english.keys() != translated.keys() or any(not value for value in translated.values()):
            raise ValueError(f"Incomplete translation catalog: {language}")
        for key, template in english.items():
            fields = {name for _, name, _, _ in Formatter().parse(template) if name is not None}
            actual = {
                name for _, name, _, _ in Formatter().parse(translated[key]) if name is not None
            }
            if fields != actual:
                raise ValueError(f"Translation placeholders differ: {language}/{key}")
