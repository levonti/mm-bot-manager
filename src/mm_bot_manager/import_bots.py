"""Bulk request submission; every import still requires an approver's !approve."""

import asyncio
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .__main__ import _connect_database, _resolve_approvers
from .cli_i18n import parser_for
from .commands.context import CommandContext
from .config import Settings, load_settings
from .db import DbConn, connect, init_schema
from .i18n import error_text, tr
from .imports import (
    ImportError,
    ImportPlan,
    check_local,
    notify_import,
    prepare_import,
    submit_imports,
)
from .localization import LanguageResolver
from .metadata import MetadataError, validate_metadata
from .mm_client import MattermostClient, MattermostError
from .repository import BotRepo, OperationRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock


def read_mapping(path: Path) -> list[tuple[str, str, str, int]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != ["bot_user_id", "owner_user_id", "git_repository", "criticality"]:
            raise ImportError("import_bots.csv_header_must_be_bot_user_id")
        rows: list[tuple[str, str, str, int]] = []
        seen: set[str] = set()
        for row in reader:
            if len(rows) >= 1000:
                raise ImportError("import_bots.a_batch_may_contain_at_most_bots")
            if None in row or any(value is None or not value.strip() for value in row.values()):
                raise ImportError("import_bots.every_csv_row_must_contain_four_nonempty")
            bot_id, owner_id = row["bot_user_id"].strip(), row["owner_user_id"].strip()
            if bot_id in seen:
                raise ImportError("import_bots.duplicate_bot_ids_in_csv_use_one")
            seen.add(bot_id)
            metadata = validate_metadata(row["git_repository"], row["criticality"])
            rows.append((bot_id, owner_id, metadata.git_repository, metadata.criticality))
        if not rows:
            raise ImportError("import_bots.the_csv_contains_no_bots")
        return rows


def _preview_database(settings: Settings) -> DbConn:
    if settings.db_url or settings.db_host:
        conn = _connect_database(settings)
        conn.execute("SET default_transaction_read_only = on")
        return conn
    path = Path(settings.db_path).resolve()
    if not path.exists():
        conn = connect(":memory:")
        init_schema(conn)
        return conn
    raw = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA query_only = ON")
    return DbConn(raw, dialect="sqlite")


async def process_mapping(
    ctx: CommandContext, rows: list[tuple[str, str, str, int]], *, submit: bool
) -> tuple[list[ImportPlan], list[str]]:
    # Validate the entire batch before the first durable write or notification.
    plans = [
        await prepare_import(ctx, bot_id, owner_id, repo, crit)
        for bot_id, owner_id, repo, crit in rows
    ]
    requests = submit_imports(ctx, plans) if submit else []
    for request in requests:
        await notify_import(ctx, request)
    return plans, [request.request_id for request in requests]


async def run_import(settings: Settings, path: Path, *, submit: bool = False) -> int:
    rows = read_mapping(path)
    conn = _connect_database(settings) if submit else _preview_database(settings)
    with closing(conn):
        # A live manager must be stopped for submission; preview requires no exclusive lock.
        if submit:
            with instance_lock(conn, settings.db_path):
                init_schema(conn)
                return await _run_connected(settings, conn, rows, submit=True)
        return await _run_connected(settings, conn, rows, submit=False)


async def _run_connected(
    settings: Settings, conn: DbConn, rows: list[tuple[str, str, str, int]], *, submit: bool
) -> int:
    async with (
        MattermostClient(
            settings.mm_rest_base, settings.mm_token, verify_ssl=settings.mm_verify_ssl
        ) as mm,
        MattermostClient(
            settings.mm_rest_base, settings.mm_bot_token, verify_ssl=settings.mm_verify_ssl
        ) as bot_mm,
    ):
        approvers = await _resolve_approvers(
            mm, settings.approver_usernames, language=settings.mm_default_language
        )
        if not approvers:
            raise ImportError("import_bots.configure_mm_approvers_before_importing_any_bots")
        actor = await mm.get_me()
        manager = await bot_mm.get_me()
        ctx = CommandContext(
            caller_user_id=actor["id"],
            caller_username=actor["username"],
            channel_id="",
            bot_repo=BotRepo(conn),
            token_repo=TokenRepo(conn),
            pending_repo=PendingRequestRepo(conn),
            mm=mm,
            bot_mm=bot_mm,
            manager_user_id=manager["id"],
            approver_ids=approvers,
            operation_repo=OperationRepo(conn),
            language=settings.mm_default_language,
            default_language=settings.mm_default_language,
            language_resolver=LanguageResolver(conn, mm, settings.mm_default_language),
        )
        plans, request_ids = await process_mapping(ctx, rows, submit=submit)
        print(
            json.dumps(
                {
                    "mode": "submitted_for_approval" if submit else "dry_run",
                    "bots": [
                        {
                            "bot_user_id": p.bot_user_id,
                            "username": p.username,
                            "remote_owner_id": p.remote_owner_id,
                            "owner_user_id": p.owner_user_id,
                            "owner_username": p.owner_username,
                            "git_repository": p.git_repository,
                            "criticality": p.criticality,
                            "locally_managed": check_local(ctx, p) is not None,
                        }
                        for p in plans
                    ],
                    "request_ids": request_ids,
                    "next_step": tr(settings.mm_default_language, "cli.import.next_step"),
                },
                indent=2,
            )
        )
    return 0


def run() -> None:
    parser, argv = parser_for("cli.import.description")
    parser.add_argument("mapping", type=Path, help=parser.text("cli.import.mapping"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=parser.text("cli.import.dry_run"))
    mode.add_argument("--submit", action="store_true", help=parser.text("cli.import.submit"))
    args = parser.parse_args(argv)
    try:
        settings = load_settings().model_copy(update={"mm_default_language": args.language})
        result = asyncio.run(run_import(settings, args.mapping, submit=args.submit))
    except (ImportError, MetadataError) as exc:
        parser.exit(2, parser.text("cli.import.rejected", error=error_text(exc, args.language)))
    except csv.Error:
        parser.exit(2, parser.text("cli.import.rejected", error=parser.text("error.csv")))
    except MattermostError as exc:
        parser.exit(2, parser.text("cli.import.api", status=exc.status))
    except Exception:
        parser.exit(2, parser.text("cli.import.failed"))
    else:
        raise SystemExit(result)


if __name__ == "__main__":
    run()
