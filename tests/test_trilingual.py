"""Behavioral checks for three-language upgrades and translation review gates."""

import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from mm_bot_manager.commands import dispatch
from mm_bot_manager.dates import format_datetime
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.i18n import catalog, normalize_language, plural
from mm_bot_manager.localization import LanguageResolver
from mm_bot_manager.repository import PreferenceRepo
from tests.test_i18n import env  # noqa: F401


async def test_german_choice_auto_and_restart(env):  # noqa: F811
    env.mm.users[env.caller_user_id]["locale"] = "de-DE"
    assert "Befehle" in await dispatch(env, "!help")
    assert "Sprache gespeichert" in await dispatch(env, "!language de")
    init_schema(env.bot_repo.conn)
    assert PreferenceRepo(env.bot_repo.conn).get(env.caller_user_id)[0] == "de"
    env.mm.users[env.caller_user_id]["locale"] = "ru-RU"
    assert "Befehle" in await dispatch(env, "!help")
    assert "Язык сохранён" in await dispatch(env, "!language auto")
    assert normalize_language("de_CH") == "de"
    assert "31.10.2026" in format_datetime("2026-10-31T12:00:00+00:00", "de")
    for count in (0, 1, 2, 5, 11, 21):
        assert plural("de", count, "catalog.count") == (
            f"{count} passender Bot" if count == 1 else f"{count} passende Bots"
        )
    env = replace(env, language_resolver=LanguageResolver(env.bot_repo.conn, env.mm, "de"))
    env.mm.users[env.caller_user_id]["locale"] = "fr"
    assert "Befehle" in await dispatch(env, "!help")


def test_sqlite_upgrade_preserves_both_preferences_and_is_atomic(monkeypatch):
    conn = connect(":memory:")
    conn.execute(
        "CREATE TABLE user_preference (user_id TEXT PRIMARY KEY, "
        "language TEXT NOT NULL CHECK(language IN ('auto','en','ru')), "
        "catalog_format TEXT NOT NULL CHECK(catalog_format IN ('cards','table')))"
    )
    conn.execute("INSERT INTO user_preference VALUES ('owner', 'ru', 'table')")
    original = conn.execute

    def fail(sql, params=()):
        if sql == "DROP TABLE user_preference_old":
            raise RuntimeError("interrupted migration")
        return original(sql, params)

    monkeypatch.setattr(conn, "execute", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        init_schema(conn)
    monkeypatch.setattr(conn, "execute", original)
    assert PreferenceRepo(conn).get("owner") == ("ru", "table")
    with pytest.raises(sqlite3.IntegrityError):
        PreferenceRepo(conn).language("owner", "de")
    init_schema(conn)
    PreferenceRepo(conn).language("owner", "de")
    init_schema(conn)
    assert PreferenceRepo(conn).get("owner") == ("de", "table")
    assert catalog("de").keys() == catalog("en").keys()
    conn.close()


def test_translation_gate_detects_stale_source_and_broken_placeholders(tmp_path):
    import shutil

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "translation_check", root / "scripts/check_translations.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check(root) == []
    for name in module.fingerprints(root):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    shutil.copyfile(root / "docs/translations.json", tmp_path / "docs/translations.json")
    path = tmp_path / "src/mm_bot_manager/locales/de.json"
    data = json.loads(path.read_text())
    data["error.api"] = "Fehler {wrong}"
    path.write_text(json.dumps(data))
    errors = module.check(tmp_path)
    assert any("placeholders differ" in error for error in errors)
    assert any("stale" in error for error in errors)
    data["error.api"] = catalog("de")["error.api"]
    path.write_text(json.dumps(data))
    guide = tmp_path / "docs/de/user-guide.md"
    guide.write_text(guide.read_text() + "\n[Broken](missing.md)\n")
    assert any("broken link" in error for error in module.check(tmp_path))


@pytest.mark.parametrize(
    "module_name", ["__main__", "import_bots", "metadata_migration", "recovery"]
)
def test_german_cli_help_without_credentials(monkeypatch, capsys, tmp_path, module_name):
    from importlib import import_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MM_DEFAULT_LANGUAGE", "de")
    monkeypatch.setattr("sys.argv", ["tool", "--language", "de", "--help"])
    module = import_module(f"mm_bot_manager.{module_name}")
    with pytest.raises(SystemExit) as stopped:
        (module.main if module_name == "recovery" else module.run)()
    assert stopped.value.code == 0
    assert "Verwendung:" in capsys.readouterr().out


def test_german_preferences_survive_database_reopen(tmp_path):
    path = str(tmp_path / "preferences.sqlite")
    conn = connect(path)
    init_schema(conn)
    PreferenceRepo(conn).language("owner", "de")
    conn.close()
    conn = connect(path)
    try:
        init_schema(conn)
        assert PreferenceRepo(conn).get("owner") == ("de", "cards")
    finally:
        conn.close()


async def test_three_language_approval_delivery(env):  # noqa: F811
    from tests.test_i18n import as_user
    from tests.test_imports import APPROVER, BOB
    from tests.test_registry import FLAGS

    prefs = PreferenceRepo(env.bot_repo.conn)
    prefs.language(env.caller_user_id, "de")
    prefs.language(APPROVER, "en")
    prefs.language(BOB, "ru")
    ctx = replace(env, approver_ids=frozenset({APPROVER, BOB}))
    assert "eingereicht" in await dispatch(ctx, f"!create localized-bot {FLAGS}")
    messages = {p["channel_id"]: p["message"] for p in ctx.mm.posts}
    assert "Bot creation request" in messages[f"dm-{ctx.manager_user_id}-{APPROVER}"]
    assert "Заявка на создание" in messages[f"dm-{ctx.manager_user_id}-{BOB}"]
    request_id = ctx.bot_repo.conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[
        0
    ]
    assert "Approved" in await dispatch(as_user(ctx, APPROVER), f"!approve {request_id}")
    assert "genehmigt" in ctx.mm.posts[-1]["message"]
    assert "!token create localized-bot" in ctx.mm.posts[-1]["message"]
