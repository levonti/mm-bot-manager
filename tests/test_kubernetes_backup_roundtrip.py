"""Optional real PostgreSQL/client-image check; run with MM_TEST_BACKUP_DOCKER=1."""

import os

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from mm_bot_manager.db import connect_postgres, init_schema
from mm_bot_manager.repository import BotRepo, PreferenceRepo, TokenRepo
from tests.test_helm_chart import HELM, by_kind, render

pytestmark = pytest.mark.skipif(
    os.environ.get("MM_TEST_BACKUP_DOCKER") != "1" or HELM is None,
    reason="Set MM_TEST_BACKUP_DOCKER=1 with a Docker daemon and Helm for the real backup drill",
)


def test_rendered_backup_verification_and_restore_with_real_postgresql(tmp_path):
    # Separate disposable server: no use of the operator's database or Mattermost credentials.
    with PostgresContainer(
        "postgres:15-alpine", username="test_operator", password="test-only", dbname="test_registry"
    ).with_volume_mapping(str(tmp_path), "/backups", mode="rw") as container:
        dsn = "postgresql://" + container.get_connection_url().split("://", 1)[1]
        conn = connect_postgres(dsn)
        init_schema(conn)
        BotRepo(conn).insert(
            bot_user_id="test-bot",
            username="test-bot",
            display_name="original",
            description="purpose",
            owner_user_id="test-owner",
            owner_username="owner",
            git_repository="https://git.example/project",
            criticality=1,
        )
        TokenRepo(conn).record(
            token_id="tracked-test-token", bot_user_id="test-bot", description="test"
        )
        PreferenceRepo(conn).language("test-owner", "ru")
        conn.close()

        def query(sql, *, admin=False):
            with psycopg.connect(
                dsn, dbname="postgres" if admin else "test_registry", autocommit=True
            ) as db:
                cursor = db.execute(sql)
                return cursor.fetchall() if cursor.description else []

        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(
                "CREATE TABLE roundtrip_probe AS SELECT string_agg(md5(g::text), '') AS marker "
                "FROM generate_series(1,10000) g"
            )
        marker = query("SELECT marker FROM roundtrip_probe")
        env = {
            "PGHOST": "127.0.0.1",
            "PGPORT": "5432",
            "PGDATABASE": "test_registry",
            "PGUSER": "test_operator",
            "PGPASSWORD": "test-only",
            "RETENTION_DAYS": "14",
        }
        backup = by_kind(render("values-internal-longhorn.yaml"), "CronJob")[0]["spec"][
            "jobTemplate"
        ]["spec"]["template"]["spec"]["containers"][0]["args"][0]
        verification = by_kind(
            render(
                "values-internal-longhorn.yaml",
                "--set",
                "database.internal.verification.enabled=true",
                "--set",
                "database.internal.verification.file=archive.dump",
                show_only="templates/backup-verification-job.yaml",
            ),
            "Job",
        )[0]["spec"]["template"]["spec"]["containers"][0]["args"][0]
        restore = by_kind(
            render(
                "values-internal-longhorn.yaml",
                "--set",
                "database.internal.restore.enabled=true",
                "--set",
                "database.internal.restore.file=archive.dump",
                show_only="templates/restore-job.yaml",
            ),
            "Job",
        )[0]["spec"]["template"]["spec"]["containers"][0]["args"][0]

        def run(script, **overrides):
            return container.get_wrapped_container().exec_run(
                ["sh", "-ec", script], environment={**env, **overrides}
            )

        for uid in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
            result = run(backup, POD_UID=uid)
            assert result.exit_code == 0, result.output.decode()
        archives = sorted(tmp_path.glob("*.dump"))
        assert len(archives) == 2 and not list(tmp_path.glob("*.tmp"))
        pvc_check = by_kind(
            render(
                "values-internal-local-path.yaml",
                show_only="templates/backup-pvc-check-job.yaml",
            ),
            "Job",
        )[0]["spec"]["template"]["spec"]["containers"][0]
        # Exercise the rendered check with the PostgreSQL image's actual /bin/sh
        # and filesystem tools, without passing database credentials to the check.
        before_check = {path.name: path.read_bytes() for path in archives}
        result = container.get_wrapped_container().exec_run(
            [*pvc_check["command"], *pvc_check["args"]]
        )
        assert result.exit_code == 0, result.output.decode()
        assert b"backup PVC write/read/delete check passed" in result.output
        assert {path.name: path.read_bytes() for path in tmp_path.glob("*.dump")} == before_check
        assert not list(tmp_path.glob(".pvc-check.*"))
        archive = archives[0].name
        verify_uid = "33333333-3333-3333-3333-333333333333"
        result = run(verification, VERIFY_FILE=archive, POD_UID=verify_uid)
        assert result.exit_code == 0, result.output.decode()
        assert "isolated database" in result.output.decode()
        assert (
            query("SELECT datname FROM pg_database WHERE datname LIKE 'mm_verify_%'", admin=True)
            == []
        )
        assert query("SELECT marker FROM roundtrip_probe") == marker

        # The archive catalog can be valid while its data stream is truncated.
        result = run(
            'size="$(stat -c %s "/backups/$GOOD_FILE")"; '
            'head -c "$((size / 2))" "/backups/$GOOD_FILE" > /backups/broken.dump; '
            "pg_restore --list /backups/broken.dump >/dev/null",
            GOOD_FILE=archive,
        )
        assert result.exit_code == 0, result.output.decode()
        result = run(verification, VERIFY_FILE="broken.dump", POD_UID=verify_uid)
        assert result.exit_code != 0
        assert (
            query("SELECT datname FROM pg_database WHERE datname LIKE 'mm_verify_%'", admin=True)
            == []
        )
        assert query("SELECT marker FROM roundtrip_probe") == marker

        # Failed createdb must not trigger cleanup of someone else's existing database.
        query("CREATE DATABASE mm_verify_33333333333333333333333333333333", admin=True)
        result = run(verification, VERIFY_FILE=archive, POD_UID=verify_uid)
        assert result.exit_code != 0
        assert query(
            "SELECT datname FROM pg_database "
            "WHERE datname = 'mm_verify_33333333333333333333333333333333'",
            admin=True,
        )
        query("DROP DATABASE mm_verify_33333333333333333333333333333333", admin=True)

        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("UPDATE bot SET display_name = 'after-backup'")
        result = run(restore, RESTORE_FILE=archive)
        assert result.exit_code == 0, result.output.decode()
        assert query("SELECT display_name FROM bot") == [("original",)]
        assert query("SELECT language FROM user_preference") == [("ru",)]
        assert query("SELECT count(*) FROM token") == [(1,)]
        assert query("SELECT marker FROM roundtrip_probe") == marker
        result = run(restore, RESTORE_FILE=archive, PGDATABASE="postgres")
        assert result.exit_code != 0 and b"system database" in result.output
