import argparse
import csv
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from mm_bot_manager.commands import dispatch
from mm_bot_manager.db import connect, init_schema, transaction
from mm_bot_manager.metadata import MetadataError, validate_metadata
from mm_bot_manager.metadata_migration import (
    apply_backfill,
    execute,
    preview_backfill,
    read_backfill,
)
from mm_bot_manager.repository import (
    BotRepo,
    CatalogRepo,
    MetadataRepo,
    PendingRequestRepo,
    TokenRepo,
)
from tests.test_imports import (
    ALICE,
    APPROVER,
    BOB,
    BOT,
    approver,
    import_context,
    request_import,
    settings_for,
)

REPO = "https://git.example/team/project"
FLAGS = f"--repo {REPO} --criticality 1"


@pytest.fixture()
def env():
    conn = connect(":memory:")
    init_schema(conn)
    yield import_context(conn)
    conn.close()


def seed(ctx, number=0, *, owner=ALICE, name=None, criticality=2, description=None):
    return ctx.bot_repo.insert(
        bot_user_id=f"bot-{number}",
        username=name or f"demo-{number}",
        display_name=None,
        description=description,
        owner_user_id=owner,
        owner_username="alice" if owner == ALICE else "bob",
        git_repository=REPO,
        criticality=criticality,
    )


def make_legacy(conn):
    """Downgrade inventory columns only, with realistic persisted dependent records."""
    for table in ("bot", "pending_bot_request"):
        for event in ("insert", "update"):
            suffix = "" if conn.dialect == "sqlite" else f" ON {table}"
            conn.execute(f"DROP TRIGGER require_{table}_metadata_{event}{suffix}")
        for column in ("git_repository", "criticality"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute(
        "INSERT INTO bot (bot_user_id, username, owner_user_id, owner_username, created_at) "
        "VALUES ('legacy', 'legacy', ?, 'alice', 'old')",
        (ALICE,),
    )
    TokenRepo(conn).record(token_id="old-token", bot_user_id="legacy", description="keep")
    conn.execute(
        "INSERT INTO pending_bot_request (request_id, username, requester_user_id, "
        "requester_username, created_at) VALUES ('pending', 'pending-bot', ?, 'alice', 'old')",
        (ALICE,),
    )
    conn.execute(
        "INSERT INTO pending_bot_request (request_id, username, requester_user_id, "
        "requester_username, created_at, status) "
        "VALUES ('history', 'historical-bot', ?, 'alice', 'old', 'approved')",
        (ALICE,),
    )


def backfill_rows():
    metadata = validate_metadata(REPO, 1)
    return [("bot", "legacy", metadata), ("request", "pending", metadata)]


@pytest.mark.parametrize(
    "url",
    [REPO, "git@git.internal:group/subgroup/repo.git", "ssh://git@git.internal:2222/team/repo.git"],
)
def test_repository_formats_accept_private_hosts_without_network(url):
    assert validate_metadata(url, 1).git_repository == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        " ",
        "/local/repo",
        "http://git.example/team/repo",
        "file:///etc/passwd",
        "https://user:credential-secret@git.example/team/repo",
        "https://credential-secret@git.example/team/repo",
        "https://git.example/team/repo?token=credential-secret",
        "https://git.example/team/repo#credential-secret",
        "ssh://git:secret@git.internal/team/repo",
        "https://git.example/",
        "git@git.example:",
        "https://git.example:99999/team/repo",
        "https://git.example/team/%0Arepo",
        "https://git.example/team/../repo",
        "x" * 2049,
    ],
)
def test_invalid_repository_never_echoes_potential_credentials(url):
    with pytest.raises(MetadataError) as exc:
        validate_metadata(url, 1)
    assert "credential-secret" not in str(exc.value)


@pytest.mark.parametrize("criticality", [None, 0, 4, -1, 1.5, "01", "2.0", "x", "", True, False])
def test_criticality_is_an_explicit_integer_from_one_to_three(criticality):
    with pytest.raises(MetadataError):
        validate_metadata(REPO, criticality)


@pytest.mark.parametrize("command", ["!create new-bot", "!import legacy"])
@pytest.mark.parametrize(
    "flags", ["", f"--repo {REPO}", "--criticality 1", f"--repo {REPO} --criticality 4"]
)
async def test_missing_or_invalid_fields_never_create_requests_or_remote_resources(
    env, command, flags
):
    reply = await dispatch(env, f"{command} {flags}")
    assert "required" in reply or "Criticality" in reply
    assert env.bot_repo.conn.execute("SELECT COUNT(*) FROM pending_bot_request").fetchone()[0] == 0
    assert not env.mm.reads
    env.mm.assert_no_bot_mutations()


async def test_new_create_request_and_approval_preserve_exact_attributes(env):
    assert "submitted" in await dispatch(env, f"!create demo-bot {FLAGS}")
    request_id = env.bot_repo.conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[
        0
    ]
    request = env.pending_repo.get(request_id)
    assert request.git_repository == REPO and request.criticality == 1
    assert REPO in env.mm.posts[0]["message"] and "criticality: 1" in env.mm.posts[0]["message"]
    assert "created" in await dispatch(approver(env), f"!approve {request_id}")
    bot = env.bot_repo.list_by_owner(ALICE)[0]
    assert bot.git_repository == REPO and bot.criticality == 1
    assert "repository:" in await dispatch(env, "!info demo-bot")
    assert REPO in await dispatch(env, "!list")


async def test_import_metadata_is_in_approval_and_conflicts_require_new_request(env):
    request = await request_import(env)
    assert "Could not request import" in await dispatch(env, f"!import legacy {FLAGS}")
    assert env.pending_repo.get(request.request_id).criticality == 2
    assert "imported" in await dispatch(approver(env), f"!approve {request.request_id}")
    bot = env.bot_repo.get(BOT)
    assert bot.git_repository == REPO and bot.criticality == 2
    assert "Local metadata differs" in await dispatch(env, f"!import legacy {FLAGS}")
    env.mm.assert_no_bot_mutations()


async def test_catalog_permissions_are_read_only_and_dm_only(env):
    seed(env, owner=BOB)
    assert "not authorised" in await dispatch(env, "!catalog")
    viewer = replace(env, catalog_viewer_ids=frozenset({ALICE}))
    assert REPO in await dispatch(viewer, "!catalog")
    assert "not owned" in await dispatch(viewer, "!update demo-0 --criticality 1")
    assert "not owned" in await dispatch(viewer, "!delete demo-0")
    assert "not owned" in await dispatch(viewer, "!token create demo-0")
    for channel in (None, "O", "P", "G"):
        assert "direct message" in await dispatch(
            replace(approver(env), channel_type=channel), "!catalog"
        )
    env.mm.assert_no_bot_mutations()


async def test_catalog_filters_order_pagination_and_deleted_exclusion(env):
    for i in range(23):
        seed(env, i, owner=BOB if i % 2 else ALICE, criticality=(i % 3) + 1)
    env.bot_repo.mark_deleted("bot-22")
    ctx = approver(env)
    page1 = await dispatch(ctx, "!catalog")
    page2 = await dispatch(ctx, "!catalog --page 2")
    assert "22 matching bots" in page1 and "page 1/3" in page1
    assert "page 2/3" in page2
    rows, total = CatalogRepo(env.bot_repo.conn).page()
    assert total == 22 and len(rows) == 10
    assert rows[0]["criticality"] == 1 and rows[-1]["criticality"] == 2
    assert "demo-22" not in page1 + page2
    filtered = await dispatch(ctx, "!catalog --owner @bob --criticality 2")
    assert "4 matching bots" in filtered
    assert "1 matching bots" in await dispatch(ctx, "!catalog --search DEMO-21")
    assert "0 matching bots" in await dispatch(ctx, "!catalog --search %")
    assert "0 matching bots" in await dispatch(ctx, '!catalog --search "x\' OR 1=1 --"')
    assert "No bots" in await dispatch(ctx, "!catalog --page 999")


@pytest.mark.parametrize(
    "flags",
    [
        "extra",
        "--unknown x",
        "--page 0",
        "--page -1",
        "--page xx",
        "--page 100001",
        "--criticality 4",
        "--owner",
        "--search",
    ],
)
async def test_catalog_rejects_invalid_arguments(env, flags):
    reply = await dispatch(approver(env), "!catalog " + flags)
    assert "Usage" in reply or "Invalid" in reply


async def test_catalog_bounds_and_escapes_free_text_and_never_contains_tokens(env):
    seed(env, description="[link](https://wrong.example) | `code`\n@everyone " + "z" * 2000)
    text = await dispatch(approver(env), "!catalog")
    assert "\\|" in text and "\\`code\\`" in text and "\\@everyone" in text
    assert "\\[link\\]" in text and len(text) < 1500
    assert "token" not in text.lower()


async def test_metadata_update_is_local_authorised_and_audited(env):
    seed(env)
    reply = await dispatch(env, "!update demo-0 --criticality 1")
    assert "Updated registry" in reply
    audit = dict(env.bot_repo.conn.execute("SELECT * FROM metadata_audit").fetchone())
    assert audit["old_criticality"] == 2 and audit["criticality"] == 1
    assert audit["changed_by"] == ALICE and audit["source"] == "chat"
    # An identical retry is a no-op, with no duplicate change event.
    await dispatch(env, "!update demo-0 --criticality 1")
    assert env.bot_repo.conn.execute("SELECT COUNT(*) FROM metadata_audit").fetchone()[0] == 1
    await dispatch(approver(env), "!update demo-0 --repo git@git.example:group/project.git")
    assert env.bot_repo.get("bot-0").git_repository == "git@git.example:group/project.git"
    assert "separately" in await dispatch(env, "!update demo-0 --criticality 3 --description hi")
    assert "direct message" in await dispatch(
        replace(env, channel_type="O"), "!update demo-0 --criticality 3"
    )
    assert "Criticality" in await dispatch(env, "!update demo-0 --criticality 0")
    assert "Unknown" in await dispatch(env, "!update demo-0 --bogus x")
    env.mm.assert_no_bot_mutations()


def assert_constraints(conn):
    for table in ("bot", "pending_bot_request"):
        for values in ((None, None), (REPO, 0), (" ", 1), (REPO, 4)):
            statement = (
                "INSERT INTO bot (bot_user_id, username, owner_user_id, owner_username, "
                "created_at, git_repository, criticality) "
                "VALUES ('invalid', 'invalid', 'owner', 'owner', 'now', ?, ?)"
                if table == "bot"
                else "INSERT INTO pending_bot_request (request_id, username, requester_user_id, "
                "requester_username, created_at, git_repository, criticality) "
                "VALUES ('invalid', 'invalid', 'owner', 'owner', 'now', ?, ?)"
            )
            with pytest.raises(Exception, match=r"metadata|constraint"), transaction(conn):
                conn.execute(statement, values)
    BotRepo(conn).insert(
        bot_user_id="valid",
        username="valid",
        display_name=None,
        description=None,
        owner_user_id=ALICE,
        owner_username="alice",
        git_repository=REPO,
        criticality=1,
    )
    with pytest.raises(Exception, match=r"metadata|constraint"), transaction(conn):
        conn.execute("UPDATE bot SET git_repository = NULL WHERE bot_user_id = 'valid'")
    with pytest.raises(Exception, match=r"metadata|constraint"), transaction(conn):
        conn.execute("UPDATE bot SET criticality = NULL WHERE bot_user_id = 'valid'")


def test_sqlite_constraints_prevent_sql_bypass(env):
    assert_constraints(env.bot_repo.conn)


async def test_legacy_rows_are_explicitly_missing_and_cannot_be_approved(env):
    conn = env.bot_repo.conn
    make_legacy(conn)
    # Preview of the old schema is possible without ALTER TABLE or guessed defaults.
    assert len(MetadataRepo(conn).missing()) == 2
    init_schema(conn)
    init_schema(conn)
    assert env.bot_repo.get("legacy").git_repository is None
    assert env.bot_repo.get("legacy").criticality is None
    assert env.token_repo.get("old-token").revoked_at is None
    assert "METADATA REQUIRED" in await dispatch(approver(env), "!catalog")
    assert "incomplete" in await dispatch(approver(env), "!approve pending")
    env.mm.assert_no_bot_mutations()
    # Denial/recovery of old requests is allowed without fabricating inventory values.
    assert "Denied" in await dispatch(approver(env), "!deny pending")


def assert_backfill(conn, monkeypatch):
    make_legacy(conn)
    init_schema(conn)
    records = backfill_rows()
    assert preview_backfill(conn, records) == 0
    assert len(MetadataRepo(conn).missing()) == 2  # preview made no writes
    with pytest.raises(MetadataError, match="all missing"):
        apply_backfill(conn, records[:1], APPROVER)
    assert BotRepo(conn).get("legacy").git_repository is None
    original = MetadataRepo.set

    def fail_second(self, kind, *args, **kwargs):
        original(self, kind, *args, **kwargs)
        if kind == "request":
            raise RuntimeError("audit disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(MetadataRepo, "set", fail_second)
        with pytest.raises(RuntimeError, match="disk failure"):
            apply_backfill(conn, records, APPROVER)
    assert len(MetadataRepo(conn).missing()) == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM metadata_audit").fetchone()["n"] == 0
    apply_backfill(conn, records, APPROVER)
    assert not MetadataRepo(conn).missing()
    assert BotRepo(conn).get("legacy").criticality == 1
    request = PendingRequestRepo(conn).get("pending")
    assert request.status == "pending" and request.git_repository == REPO
    assert PendingRequestRepo(conn).get("history").git_repository is None
    assert TokenRepo(conn).get("old-token").revoked_at is None
    audits = conn.execute("SELECT * FROM metadata_audit ORDER BY record_type").fetchall()
    assert len(audits) == 2 and all(row["changed_by"] == APPROVER for row in audits)
    apply_backfill(conn, records, APPROVER)
    assert conn.execute("SELECT COUNT(*) AS n FROM metadata_audit").fetchone()["n"] == 2


def test_sqlite_backfill_is_complete_atomic_audited_and_idempotent(env, monkeypatch):
    assert_backfill(env.bot_repo.conn, monkeypatch)


def test_backfill_refuses_changes_to_completed_metadata_and_resolved_requests(env):
    conn = env.bot_repo.conn
    make_legacy(conn)
    init_schema(conn)
    apply_backfill(conn, backfill_rows(), APPROVER)
    with pytest.raises(MetadataError, match="replace"):
        preview_backfill(conn, [("bot", "legacy", validate_metadata(REPO, 3))])
    with pytest.raises(MetadataError, match="pending"):
        preview_backfill(conn, [("request", "history", validate_metadata(REPO, 3))])
    with pytest.raises(MetadataError, match="not found"):
        preview_backfill(conn, [("bot", "absent", validate_metadata(REPO, 1))])


def csv_file(path, rows=None):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["record_type", "record_id", "git_repository", "criticality"])
        writer.writerows(rows or [["bot", "legacy", REPO, 1], ["request", "pending", REPO, 1]])
    return path


@respx.mock
async def test_migration_cli_previews_old_schema_without_disk_or_network_writes(tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    conn = connect(str(path))
    init_schema(conn)
    make_legacy(conn)
    conn.close()
    before = path.read_bytes()
    settings = settings_for(path)
    exported = tmp_path / "export.csv"
    assert execute(settings, argparse.Namespace(command="export", path=exported)) == 0
    assert exported.read_text().count("\n") == 3
    with pytest.raises(FileExistsError):
        execute(settings, argparse.Namespace(command="export", path=exported))
    assert execute(settings, argparse.Namespace(command="check")) == 2
    mapping = csv_file(tmp_path / "mapping.csv")
    assert execute(settings, argparse.Namespace(command="backfill", path=mapping, apply=False)) == 0
    assert '"dry_run"' in capsys.readouterr().out
    assert path.read_bytes() == before
    assert not respx.calls


@respx.mock
def test_migration_cli_applies_with_authenticated_actor_and_refuses_running_manager(tmp_path):
    from mm_bot_manager.runtime import instance_lock

    path = tmp_path / "db.sqlite"
    conn = connect(str(path))
    init_schema(conn)
    make_legacy(conn)
    mapping = csv_file(tmp_path / "mapping.csv")
    args = argparse.Namespace(command="backfill", path=mapping, apply=True)
    settings = settings_for(path)
    route = respx.get("https://mm.example/api/v4/users/me").mock(
        return_value=httpx.Response(200, json={"id": APPROVER, "delete_at": 0, "is_bot": False})
    )
    try:
        with instance_lock(conn, str(path)), pytest.raises(RuntimeError, match="Another"):
            execute(settings, args)
        assert not route.called
        assert execute(settings, args) == 0
        assert route.calls.last.request.headers["authorization"] == "Bearer admin-test-token"
        assert not MetadataRepo(conn).missing()
        assert conn.execute("SELECT changed_by FROM metadata_audit").fetchone()[0] == APPROVER
    finally:
        conn.close()


@pytest.mark.parametrize(
    "rows",
    [
        [["bot", "legacy", REPO, ""]],
        [["wrong", "legacy", REPO, 1]],
        [["bot", "legacy", REPO, 1], ["bot", "legacy", REPO, 2]],
    ],
)
def test_backfill_csv_rejects_missing_attributes_wrong_type_and_duplicates(tmp_path, rows):
    with pytest.raises(MetadataError):
        read_backfill(csv_file(tmp_path / "mapping.csv", rows))


@respx.mock
def test_migration_refuses_bot_or_disabled_actor_without_backfilling(tmp_path):
    path = tmp_path / "db.sqlite"
    conn = connect(str(path))
    init_schema(conn)
    make_legacy(conn)
    mapping = csv_file(tmp_path / "mapping.csv")
    args = argparse.Namespace(command="backfill", path=mapping, apply=True)
    route = respx.get("https://mm.example/api/v4/users/me")
    try:
        for actor in (
            {"id": APPROVER, "delete_at": 0, "is_bot": True},
            {"id": APPROVER, "delete_at": 1},
        ):
            route.mock(return_value=httpx.Response(200, json=actor))
            with pytest.raises(MetadataError, match="active human"):
                execute(settings_for(path), args)
            assert len(MetadataRepo(conn).missing()) == 2
            assert conn.execute("SELECT COUNT(*) FROM metadata_audit").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "error,expected",
    [
        (MetadataError("invalid attributes"), "invalid attributes"),
        (RuntimeError("credential-secret"), "check input"),
    ],
)
def test_migration_entrypoint_never_prints_raw_database_or_transport_secrets(
    tmp_path, monkeypatch, capsys, error, expected
):
    from mm_bot_manager import metadata_migration

    monkeypatch.setattr("sys.argv", ["mm-bot-manager-metadata", "check"])
    monkeypatch.setattr(metadata_migration, "load_settings", lambda: settings_for(tmp_path / "db"))

    def fail(*args):
        raise error

    monkeypatch.setattr(metadata_migration, "execute", fail)
    with pytest.raises(SystemExit) as exc:
        metadata_migration.run()
    assert exc.value.code == 2
    output = capsys.readouterr().err
    assert expected in output and "credential-secret" not in output


def test_migration_entrypoint_preserves_incomplete_exit_code(tmp_path, monkeypatch):
    from mm_bot_manager import metadata_migration

    monkeypatch.setattr("sys.argv", ["mm-bot-manager-metadata", "check"])
    monkeypatch.setattr(metadata_migration, "load_settings", lambda: settings_for(tmp_path / "db"))
    monkeypatch.setattr(metadata_migration, "execute", lambda *args: 2)
    with pytest.raises(SystemExit) as exc:
        metadata_migration.run()
    assert exc.value.code == 2


def test_compose_passes_catalog_viewer_configuration_to_container():
    import yaml

    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    assert (
        compose["services"]["mm-bot-manager"]["environment"]["MM_CATALOG_VIEWERS"]
        == "${MM_CATALOG_VIEWERS:-}"
    )


@respx.mock
async def test_bulk_import_old_two_column_format_is_rejected_before_any_api_call(tmp_path):
    from mm_bot_manager.import_bots import run_import
    from mm_bot_manager.imports import ImportError

    path = tmp_path / "import.csv"
    path.write_text(f"bot_user_id,owner_user_id\n{BOT},{ALICE}\n")
    with pytest.raises(ImportError, match="header"):
        await run_import(settings_for(tmp_path / "db"), path, submit=True)
    assert not respx.calls and not (tmp_path / "db").exists()


def test_bulk_import_duplicate_bot_id_is_rejected_with_complete_metadata(tmp_path):
    from mm_bot_manager.import_bots import read_mapping
    from mm_bot_manager.imports import ImportError

    path = tmp_path / "import.csv"
    path.write_text(
        f"bot_user_id,owner_user_id,git_repository,criticality\n{BOT},{ALICE},{REPO},1\n"
        f"{BOT},{BOB},{REPO},2\n"
    )
    with pytest.raises(ImportError, match="Duplicate"):
        read_mapping(path)
