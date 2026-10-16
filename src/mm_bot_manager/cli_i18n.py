"""Localized argparse presentation without changing global gettext/locale state."""

import argparse
import sys
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .i18n import normalize_language, tr


class InterfaceSettings(BaseSettings):
    # Help must work before required server credentials have been configured.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    mm_default_language: Literal["en", "ru"] = "en"


class LocalHelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str, language: str) -> None:
        super().__init__(prog)
        self.language = language

    def add_usage(
        self, usage: str | None, actions: Any, groups: Any, prefix: str | None = None
    ) -> None:
        super().add_usage(usage, actions, groups, prefix or tr(self.language, "cli.usage"))


class LocalParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, language: str = "en", **kwargs: Any) -> None:
        self.language = language
        kwargs["add_help"] = False
        kwargs["formatter_class"] = lambda prog: LocalHelpFormatter(prog, language)
        super().__init__(*args, **kwargs)
        self._positionals.title = tr(language, "cli.positional")
        self._optionals.title = tr(language, "cli.options")
        self.add_argument("-h", "--help", action="help", help=tr(language, "cli.help"))

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        self.exit(2, tr(self.language, "cli.error") + "\n")

    def text(self, key: str, **params: Any) -> str:
        return tr(self.language, key, **params)

    def add_subparsers(self, **kwargs: Any) -> Any:
        kwargs["parser_class"] = lambda **values: LocalParser(language=self.language, **values)
        return super().add_subparsers(**kwargs)


def parser_for(description_key: str) -> tuple[LocalParser, list[str]]:
    preliminary = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    preliminary.add_argument("--language")
    try:
        language = normalize_language(InterfaceSettings().mm_default_language) or "en"
    except Exception:
        language = "en"
    try:
        choice, remaining = preliminary.parse_known_args()
    except argparse.ArgumentError:
        LocalParser(language=language).error("")
        raise AssertionError("unreachable") from None
    if choice.language is not None:
        if choice.language not in {"ru", "en"}:
            LocalParser(language=language).exit(2, tr(language, "cli.unsupported_language") + "\n")
        language = choice.language
    parser = LocalParser(language=language, description=tr(language, description_key))
    parser.add_argument(
        "--language", choices=["ru", "en"], default=language, help=tr(language, "cli.language")
    )
    return parser, remaining
