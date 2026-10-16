"""Bulk request submission; every import still requires an approver's !approve."""

import argparse
import asyncio
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .__main__ import _connect_database, _resolve_approvers
from .commands.context import CommandContext
from .config import Settings, load_settings
from .db import DbConn, connect, init_schema
from .imports import (
    ImportError,
    ImportPlan,
    check_local,
    notify_import,
    prepare_import,
    submit_imports,
)
from .metadata import MetadataError, validate_metadata
from .mm_client import MattermostClient, MattermostError
from .repository import BotRepo, OperationRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock


def read_mapping(path: Path) -> list[tuple[str, str, str, int]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != ["bot_user_id", "owner_user_id", "git_repository", "criticality"]:
            raise ImportError(
                "CSV header must be bot_user_id,owner_user_id,git_repository,criticality."
            )
        rows: list[tuple[str, str, str, int]] = []
        seen: set[str] = set()
        for row in reader:
            if len(rows) >= 1000:
                raise ImportError("A batch may contain at most 1000 bots.")
            if None in row or any(value is None or not value.strip() for value in row.values()):
                raise ImportError("Every CSV row must contain four nonempty fields.")
            bot_id, owner_id = row["bot_user_id"].strip(), row["owner_user_id"].strip()
            if bot_id in seen:
                raise ImportError("Duplicate bot IDs in CSV; use one explicit owner per bot.")
            seen.add(bot_id)
            metadata = validate_metadata(row["git_repository"], row["criticality"])
            rows.append((bot_id, owner_id, metadata.git_repository, metadata.criticality))
        if not rows:
            raise ImportError("The CSV contains no bots.")
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
        approvers = await _resolve_approvers(mm, settings.approver_usernames)
        if not approvers:
            raise ImportError("Configure MM_APPROVERS before importing any bots.")
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
                    "next_step": "An MM_APPROVERS user must !approve each request ID.",
                },
                indent=2,
            )
        )
    return 0


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mapping", type=Path, help="CSV: bot_user_id,owner_user_id,git_repository,criticality"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only preview (default)")
    mode.add_argument("--submit", action="store_true", help="submit requests; does NOT approve")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_import(load_settings(), args.mapping, submit=args.submit))
    except (ImportError, MetadataError, csv.Error) as exc:
        parser.exit(2, f"Import rejected: {exc}\n")
    except MattermostError as exc:
        parser.exit(2, f"Mattermost validation failed (HTTP {exc.status}); nothing approved.\n")
    except Exception:
        # Never print connection DSNs, credentials, or raw database/HTTP exception payloads.
        parser.exit(
            2, "Import failed: check database access, instance lock and server connectivity.\n"
        )
    else:
        raise SystemExit(result)


if __name__ == "__main__":
    run()
