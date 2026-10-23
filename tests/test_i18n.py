# ruff: noqa: RUF001 -- Russian UI assertions intentionally use Cyrillic.
import asyncio
from dataclasses import replace

import httpx
import pytest
import respx

from mm_bot_manager.commands import dispatch
from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.i18n import catalog, error_text, plural, tr, validate_catalogs
from mm_bot_manager.localization import LanguageResolver
from mm_bot_manager.metadata import MetadataError
from mm_bot_manager.mm_client import MattermostError
from mm_bot_manager.repository import PreferenceRepo
from tests.test_imports import ALICE, APPROVER, BOB, BOT, ImportMM, import_context
from tests.test_registry import FLAGS, REPO, seed


@pytest.fixture()
def env():
    conn = connect(":memory:")
    init_schema(conn)
    ctx = import_context(conn)
    ctx = replace(
        ctx,
        language_resolver=LanguageResolver(conn, ctx.mm),
        catalog_viewer_ids=frozenset({ALICE, BOB}),
    )
    yield ctx
    conn.close()


def choose(ctx, language):
    PreferenceRepo(ctx.bot_repo.conn).language(ctx.caller_user_id, language)
    return ctx


def as_user(ctx, user_id):
    return replace(ctx, caller_user_id=user_id, caller_username=ctx.mm.users[user_id]["username"])


def test_translation_keys_placeholders_and_english_commands_are_complete():
    validate_catalogs()
    english, russian = catalog("en"), catalog("ru")
    assert len(english) > 200
    assert english.keys() == russian.keys()
    # The command examples are a protocol contract, even in Russian help.
    for command in (
        "!create",
        "!catalog",
        "!language",
        "!approve",
        "!token revoke",
        "--repo",
        "--format",
    ):
        for translated in (russian, english):
            assert command in "\n".join(
                translated[key] for key in ("help.text", "help.catalog", "help.approvers")
            )


def test_missing_russian_key_falls_back_to_english_without_global_state(monkeypatch):
    with monkeypatch.context() as patch:
        russian = dict(catalog("ru"))
        russian.pop("catalog.empty")
        patch.setattr(
            "mm_bot_manager.i18n.catalog",
            lambda language: russian if language == "ru" else catalog_en,
        )
        catalog_en = catalog("en")
        assert tr("ru", "catalog.empty") == "No bots on this page."
    assert tr("ru", "catalog.empty") == "На этой странице ботов нет."


@pytest.mark.parametrize(
    "count,form",
    [
        (0, "ботов"),
        (1, "бот"),
        (2, "бота"),
        (5, "ботов"),
        (11, "ботов"),
        (12, "ботов"),
        (21, "бот"),
        (22, "бота"),
        (111, "ботов"),
        (114, "ботов"),
    ],
)
def test_russian_plural_forms(count, form):
    assert plural("ru", count, "catalog.count").endswith(f"{count} {form}")
    expected = "bot" if count == 1 else "bots"
    assert plural("en", count, "catalog.count") == f"{count} matching {expected}"


async def test_language_selection_persists_per_user_id_and_auto_refreshes_profile(env):
    env.mm.users[ALICE]["locale"] = "en-US"
    assert "Language preference" in await dispatch(env, "!language")
    assert "Язык сохранён" in await dispatch(env, "!language ru")
    assert "Команды" in await dispatch(env, "!help")
    env.mm.users[ALICE]["username"] = "renamed"
    assert "Команды" in await dispatch(replace(env, caller_username="renamed"), "!help")
    assert "Language saved" in await dispatch(env, "!language en")
    env.mm.users[ALICE]["locale"] = "ru-RU"
    assert "Язык сохранён" in await dispatch(env, "!language auto")
    env.mm.users[ALICE]["locale"] = "en"
    assert "Language saved" in await dispatch(env, "!language auto")
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE)[0] == "auto"
    assert "commands" in await dispatch(as_user(env, BOB), "!help")


async def test_profile_cache_fallback_and_explicit_preference_priority(env):
    env.mm.users[ALICE]["locale"] = "ru"
    assert "Команды" in await dispatch(env, "!help")
    await dispatch(env, "!help")
    assert env.mm.reads.count(("user", ALICE)) == 1
    env.mm.raise_on["get_user"] = httpx.ReadTimeout("credential-secret")
    choose(env, "en")
    assert "commands" in await dispatch(env, "!help")
    assert "get_user" in env.mm.raise_on  # Manual preference does not need a profile API call.
    choose(env, "auto")
    env.language_resolver.cache[ALICE] = (0, "ru")
    assert "Команды" in await dispatch(env, "!help")  # Stale known language on transient failure.
    env.language_resolver.invalidate(ALICE)
    env.mm.users[ALICE]["locale"] = "unsupported"
    assert "commands" in await dispatch(env, "!help")


async def test_slow_profile_for_one_user_does_not_block_another(env):
    original = env.mm.get_user
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(user_id):
        if user_id == ALICE:
            entered.set()
            await release.wait()
        return await original(user_id)

    env.mm.get_user = delayed
    first = asyncio.create_task(dispatch(env, "!help"))
    await entered.wait()
    try:
        second = await asyncio.wait_for(dispatch(as_user(env, BOB), "!help"), timeout=0.2)
        assert "commands" in second
    finally:
        release.set()
        await first


async def test_profile_cache_is_bounded(env):
    # Cache size is fixed independently of server population.
    for index in range(1030):
        uid = f"user-{index}"
        env.mm.users[uid] = {"id": uid, "locale": "ru"}
        assert await env.language_resolver.resolve(uid) == "ru"
    assert len(env.language_resolver.cache) == 1024


@pytest.mark.parametrize("command", ["!language de", "!language ru extra", "!language --unknown x"])
async def test_invalid_language_does_not_change_preferences(env, command):
    assert "Usage" in await dispatch(env, command)
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE) == ("auto", "cards")
    assert "direct message" in await dispatch(replace(env, channel_type="O"), "!language ru")
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE) == ("auto", "cards")


async def test_language_and_format_survive_restart_without_touching_other_user(tmp_path):
    path = str(tmp_path / "preferences.sqlite")
    conn = connect(path)
    init_schema(conn)
    ctx = replace(import_context(conn), catalog_viewer_ids=frozenset({ALICE}))
    await dispatch(ctx, "!language ru")
    seed(ctx)
    await dispatch(ctx, "!catalog --format table")
    conn.close()
    conn = connect(path)
    try:
        init_schema(conn)
        ctx = replace(
            import_context(conn),
            catalog_viewer_ids=frozenset({ALICE}),
            language_resolver=LanguageResolver(conn, ImportMM()),
        )
        output = await dispatch(ctx, "!catalog")
        assert "Каталог ботов" in output and "\n\n| бот |" in output
        assert PreferenceRepo(conn).get(ALICE) == ("ru", "table")
        assert PreferenceRepo(conn).get(BOB) == ("auto", "cards")
    finally:
        conn.close()


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_catalog_formats_preserve_markdown_block_boundary_and_user_data(env, language):
    choose(env, language)
    seed(env, description="User supplied English description")
    table = await dispatch(env, "!catalog --format table")
    paragraphs = table.split("\n\n")
    table_block = next(block for block in paragraphs if block.startswith("|"))
    assert table_block.splitlines()[1] == "|---|---|---|---|---|---|"
    assert len(table_block.splitlines()) == 3
    assert "User supplied English description" in table and REPO in table
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE) == (language, "table")
    cards = await dispatch(env, "!catalog --format cards")
    assert "|---" not in cards and "**demo-0**" in cards
    assert (
        "**Критичность**: 2 — важный" if language == "ru" else "**Criticality**: 2 — important"
    ) in cards
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE) == (language, "cards")
    assert "**demo-0**" in await dispatch(env, "!catalog")
    assert "|---" not in await dispatch(env, "!catalog --page 99 --format table")


@pytest.mark.parametrize("flag", ["--format", "--format bad", "--format cards --page 0"])
async def test_invalid_format_does_not_change_saved_preferences(env, flag):
    PreferenceRepo(env.bot_repo.conn).format(ALICE, "table")
    assert "Invalid" in await dispatch(env, "!catalog " + flag)
    assert PreferenceRepo(env.bot_repo.conn).get(ALICE)[1] == "table"
    outsider = replace(env, caller_user_id="outsider", catalog_viewer_ids=frozenset())
    assert "not authorised" in await dispatch(outsider, "!catalog --format table")
    assert PreferenceRepo(env.bot_repo.conn).get("outsider") == ("auto", "cards")


async def test_creation_approval_and_denial_notifications_use_each_recipient_language(env):
    choose(env, "ru")
    prefs = PreferenceRepo(env.bot_repo.conn)
    prefs.language(APPROVER, "en")
    prefs.language(BOB, "ru")
    ctx = replace(env, approver_ids=frozenset({APPROVER, BOB}))
    assert "отправлена" in await dispatch(ctx, f"!create localized-bot {FLAGS}")
    messages = {p["channel_id"]: p["message"] for p in ctx.mm.posts}
    assert "Bot creation request" in messages[f"dm-{ctx.manager_user_id}-{APPROVER}"]
    assert "Заявка на создание" in messages[f"dm-{ctx.manager_user_id}-{BOB}"]
    request_id = ctx.bot_repo.conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[
        0
    ]
    reply = await dispatch(as_user(ctx, APPROVER), f"!approve {request_id}")
    assert "Approved" in reply
    assert "одобрена" in ctx.mm.posts[-1]["message"]
    assert (
        "выпуст" not in ctx.mm.posts[-1]["message"]
        or "!token create" in ctx.mm.posts[-1]["message"]
    )
    await dispatch(ctx, f"!create denied-bot {FLAGS}")
    request_id = ctx.bot_repo.conn.execute(
        "SELECT request_id FROM pending_bot_request WHERE status = 'pending'"
    ).fetchone()[0]
    await dispatch(as_user(ctx, APPROVER), f"!deny {request_id} User reason remains unchanged")
    assert "отклонена" in ctx.mm.posts[-1]["message"]
    assert "User reason remains unchanged" in ctx.mm.posts[-1]["message"]


async def test_import_notifications_and_domain_errors_are_localized(env):
    choose(env, "ru")
    PreferenceRepo(env.bot_repo.conn).language(APPROVER, "ru")
    reply = await dispatch(env, f"!import legacy {FLAGS}")
    assert "согласование" in reply
    assert "Заявка на импорт" in env.mm.posts[-1]["message"]
    request_id = env.bot_repo.conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[
        0
    ]
    await dispatch(as_user(env, APPROVER), f"!approve {request_id}")
    assert "Импорт" in env.mm.posts[-1]["message"]
    env.mm.assert_no_bot_mutations()
    env.mm.raise_on["get_bot"] = MattermostError(500, "credential-secret")
    failed = await dispatch(env, f"!import legacy {FLAGS}")
    assert "Ошибка API Mattermost (HTTP 500)" in failed and "credential-secret" not in failed
    invalid = await dispatch(
        env, "!import legacy --repo https://secret@git.example/team/repo --criticality 1"
    )
    assert "учётных данных" in invalid and "secret" not in invalid
    assert "параметра" in await dispatch(env, "!create missing-bot")
    assert "Неизвестная команда" in await dispatch(env, "!помощь")
    assert "кавычки" in await dispatch(env, '!create "unclosed')


async def test_concurrent_token_messages_keep_languages_and_secrets_in_their_own_dm(env):
    seed(env, 1, owner=ALICE)
    seed(env, 2, owner=BOB)
    choose(env, "ru")
    prefs = PreferenceRepo(env.bot_repo.conn)
    prefs.language(BOB, "en")
    original = env.mm.create_user_access_token

    async def interleaved(user_id, description):
        result = await original(user_id, description)
        await asyncio.sleep(0)
        return result

    env.mm.create_user_access_token = interleaved
    alice = replace(env, channel_id="dm-alice")
    bob = replace(as_user(env, BOB), channel_id="dm-bob")
    replies = await asyncio.gather(
        dispatch(alice, "!token create demo-1"), dispatch(bob, "!token create demo-2")
    )
    messages = {p["channel_id"]: p["message"] for p in env.mm.posts}
    assert "Создан токен" in messages["dm-alice"]
    assert "Minted token" in messages["dm-bob"]
    assert "secret-tok-0" in messages["dm-alice"] and "secret-tok-0" not in messages["dm-bob"]
    assert "secret-tok-1" in messages["dm-bob"] and "secret-tok-1" not in messages["dm-alice"]
    assert "Токен" in replies[0] and "Minted" in replies[1]
    assert all("secret-" not in reply for reply in replies)


@pytest.mark.parametrize("module", ["import_bots", "metadata_migration", "recovery"])
def test_cli_russian_help_and_invalid_arguments_without_credentials(monkeypatch, capsys, module):
    import importlib

    command = importlib.import_module("mm_bot_manager." + module)
    run = command.main if module == "recovery" else command.run
    monkeypatch.setattr("sys.argv", ["tool", "--language", "ru", "--help"])
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "использование:" in text and "параметры:" in text and "Show this help" not in text
    monkeypatch.setattr("sys.argv", ["tool", "--language=ru", "--unknown"])
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2
    assert "Некорректные параметры" in capsys.readouterr().err


def test_cli_metadata_error_localization_and_machine_json_keys(monkeypatch, capsys):
    from mm_bot_manager import metadata_migration

    monkeypatch.setattr("sys.argv", ["tool", "check", "--language", "ru"])
    monkeypatch.setattr(metadata_migration, "load_settings", lambda: object())

    def fail(*args):
        raise MetadataError("metadata.both_repo_and_criticality_are_required")

    monkeypatch.setattr(metadata_migration, "execute", fail)
    with pytest.raises(SystemExit):
        metadata_migration.run()
    assert "Оба параметра" in capsys.readouterr().err


def test_api_error_details_are_never_echoed_in_either_language():
    for language in ("ru", "en"):
        text = error_text(MattermostError(403, "credential-secret"), language)
        assert "403" in text and "credential-secret" not in text


def test_cli_default_language_reads_dotenv_without_server_credentials(
    tmp_path, monkeypatch, capsys
):
    from mm_bot_manager import metadata_migration

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MM_DEFAULT_LANGUAGE", raising=False)
    (tmp_path / ".env").write_text("MM_DEFAULT_LANGUAGE=ru\nUNRELATED_SECRET=credential-secret\n")
    monkeypatch.setattr("sys.argv", ["tool", "--help"])
    with pytest.raises(SystemExit) as exc:
        metadata_migration.run()
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "использование:" in text and "credential-secret" not in text


@pytest.mark.parametrize("arguments", [["--language"], ["--language", "unsupported"]])
def test_invalid_cli_language_is_rejected_before_database_or_credentials(
    monkeypatch, capsys, arguments
):
    from mm_bot_manager import import_bots

    monkeypatch.setattr("sys.argv", ["tool", *arguments])
    with pytest.raises(SystemExit) as exc:
        import_bots.run()
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_every_static_message_key_in_source_has_a_translation():
    import ast
    from pathlib import Path

    messages = catalog("en")
    root = Path(__file__).parents[1] / "src/mm_bot_manager"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func, index = node.func, 0
            relevant = isinstance(func, ast.Attribute) and func.attr in {"tr", "text"}
            if isinstance(func, ast.Name):
                relevant = func.id in {
                    "tr",
                    "MetadataError",
                    "ImportError",
                    "OperationError",
                    "RecoveryError",
                    "Message",
                }
                index = 1 if func.id == "tr" else 0
            if relevant and len(node.args) > index:
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    assert arg.value in messages, (path, node.lineno, arg.value)


@respx.mock
def test_russian_bulk_preview_keeps_machine_json_keys(tmp_path, monkeypatch, capsys):
    from mm_bot_manager import import_bots
    from tests.test_imports import mock_read_api, settings_for

    path = tmp_path / "mapping.csv"
    path.write_text(
        f"bot_user_id,owner_user_id,git_repository,criticality\n{BOT},{ALICE},{REPO},1\n"
    )
    mock_read_api(ImportMM())
    monkeypatch.setattr("sys.argv", ["tool", str(path), "--dry-run", "--language", "ru"])
    monkeypatch.setattr(import_bots, "load_settings", lambda: settings_for(tmp_path / "db"))
    with pytest.raises(SystemExit) as exc:
        import_bots.run()
    assert exc.value.code == 0
    import json

    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"mode", "bots", "request_ids", "next_step"}
    assert output["mode"] == "dry_run" and output["request_ids"] == []
    assert "должен выполнить !approve" in output["next_step"]
    assert output["bots"][0]["git_repository"] == REPO
    assert not (tmp_path / "db").exists()


@pytest.mark.parametrize("preference_failure", [False, True])
async def test_dispatcher_unexpected_errors_are_localized_and_do_not_leak_payloads(
    env, monkeypatch, capsys, preference_failure
):
    import json

    from mm_bot_manager import dispatcher

    choose(env, "ru")
    factory = dispatcher.CommandContextFactory(
        env.bot_repo,
        env.token_repo,
        env.pending_repo,
        env.mm,
        env.bot_mm,
        env.manager_user_id,
        env.approver_ids,
        default_language="ru",
    )

    async def fail(*args):
        raise RuntimeError("credential-secret")

    monkeypatch.setattr(dispatcher, "dispatch", fail)
    if preference_failure:

        def unavailable(*args):
            raise RuntimeError("credential-secret")

        monkeypatch.setattr(PreferenceRepo, "get", unavailable)
    event = {
        "event": "posted",
        "data": {
            "channel_type": "D",
            "sender_name": "alice",
            "post": json.dumps(
                {
                    "id": "localized-failure",
                    "user_id": ALICE,
                    "channel_id": "dm",
                    "message": "!help",
                }
            ),
        },
    }
    await dispatcher.handle_event(
        event, dispatcher.MessageRouter(env.manager_user_id, "manager"), factory
    )
    assert "Не удалось выполнить команду" in env.mm.posts[-1]["message"]
    assert "credential-secret" not in env.mm.posts[-1]["message"]
    assert "credential-secret" not in capsys.readouterr().out
