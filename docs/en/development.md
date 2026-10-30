# Development and translations

[English](../en/development.md) · [Русский](../ru/development.md) · [Deutsch](../de/development.md)

Use Python 3.14 and a uv virtual environment. Commands, flags, identifiers, JSON keys and user data stay unchanged. English is the source language. German uses formal Sie; Russian uses neutral polite wording.

Before changing a message, update all three JSON catalogs. Preserve placeholders, format specifications, command examples and Markdown. Check plural counts 0, 1, 2, 5, 11, 21 and Unicode text. Add integration tests for behavior, not just literal strings.

Update the corresponding guides in all three languages whenever behavior changes. Historical reports and platform-specific reference material retain their original language; the localized guides explain the main operational workflows. Review meaning manually: matching keys cannot prove translation accuracy.

After reviewing translations, refresh the source fingerprints below. Never refresh only to bypass a failure. Commit the catalog, guides and manifest together. CI rejects stale fingerprints, broken local links, missing keys and incompatible placeholders.

Glossary: owner / владелец / Eigentümer; approver / согласующий / Genehmiger; request / заявка / Antrag; criticality / критичность / Kritikalität; feedback handler / обработчик обращений / Rückmeldungsbearbeiter.

```sh
uv venv .venv
uv sync --frozen --extra postgres
uv run --no-sync python scripts/check_translations.py
uv run --no-sync ruff check src tests scripts/check_translations.py
uv run --no-sync mypy src
uv run --no-sync pytest -q
helm lint deploy/helm/mm-bot-manager
```

```sh
uv run --no-sync python scripts/check_translations.py --refresh
```

Optional live acceptance: `uv run --no-sync python scripts/check_live_languages.py --output /private/tmp/mm-language-report.json`. Requires Docker; provisions an isolated local Mattermost with three real users and removes its containers, volumes and network afterward. It never loads `.env`. The pinned amd64 image may require downloading and emulation. See the [acceptance report](../trilingual-live-acceptance-2026-10-30.md) for scope and limitations.
