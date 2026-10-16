"""Backfill legacy inventory attributes without inventing repository URLs or criticality."""

import argparse
import asyncio
import csv
import json
from contextlib import closing
from pathlib import Path

from .__main__ import _connect_database
from .cli_i18n import parser_for
from .config import Settings, load_settings
from .db import DbConn, init_schema, transaction
from .i18n import error_text
from .import_bots import _preview_database
from .metadata import BotMetadata, MetadataError, validate_metadata
from .mm_client import MattermostClient
from .repository import MetadataRepo
from .runtime import instance_lock

HEADER = ["record_type", "record_id", "git_repository", "criticality"]


def read_backfill(path: Path) -> list[tuple[str, str, BotMetadata]]:
    records: list[tuple[str, str, BotMetadata]] = []
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != HEADER:
            raise MetadataError("metadata_migration.csv_header_must_be_record_type_record")
        for row in reader:
            if len(records) >= 10000 or None in row or any(not v for v in row.values()):
                raise MetadataError("metadata_migration.invalid_csv_row_or_batch_exceeds_records")
            key = row["record_type"], row["record_id"]
            if key[0] not in {"bot", "request"} or key in seen:
                raise MetadataError("metadata_migration.record_types_must_be_bot_request_with")
            seen.add(key)
            records.append((*key, validate_metadata(row["git_repository"], row["criticality"])))
    if not records:
        raise MetadataError("metadata_migration.backfill_csv_is_empty")
    return records


def preview_backfill(conn: DbConn, records: list[tuple[str, str, BotMetadata]]) -> int:
    for kind, record_id, metadata in records:
        table, key = (
            ("bot", "bot_user_id") if kind == "bot" else ("pending_bot_request", "request_id")
        )
        if kind not in {"bot", "request"}:
            raise MetadataError("metadata_migration.invalid_record_type")
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)).fetchone()
        if row is None:
            raise MetadataError("metadata_migration.backfill_target_not_found")
        data = dict(row)
        if kind == "request" and data["status"] != "pending":
            raise MetadataError("repository.only_pending_legacy_requests_may_be_backfilled")
        old = data.get("git_repository"), data.get("criticality")
        if None not in old and old != (metadata.git_repository, metadata.criticality):
            raise MetadataError("repository.backfill_cannot_replace_existing_metadata_use_an")
    covered = {(kind, record_id) for kind, record_id, _ in records}
    return sum(
        (r["record_type"], r["record_id"]) not in covered for r in MetadataRepo(conn).missing()
    )


def apply_backfill(conn: DbConn, records: list[tuple[str, str, BotMetadata]], actor: str) -> None:
    with transaction(conn):
        if preview_backfill(conn, records):
            raise MetadataError("metadata_migration.csv_must_cover_all_missing_bot_and")
        for kind, record_id, metadata in records:
            MetadataRepo(conn).set(
                kind, record_id, metadata, actor=actor, source="migration", only_missing=True
            )
        if MetadataRepo(conn).missing():
            raise MetadataError(
                "metadata_migration.incomplete_backfill_the_entire_batch_was_rolled"
            )


async def _actor(settings: Settings) -> str:
    async with MattermostClient(
        settings.mm_rest_base, settings.mm_token, verify_ssl=settings.mm_verify_ssl
    ) as mm:
        account = await mm.get_me()
        if not account.get("id") or account.get("is_bot", False) or account.get("delete_at") != 0:
            raise MetadataError(
                "metadata_migration.backfill_requires_an_authenticated_active_human_mm"
            )
        return str(account["id"])


def execute(settings: Settings, args: argparse.Namespace) -> int:
    if args.command == "backfill" and args.apply:
        records = read_backfill(args.path)
        conn = _connect_database(settings)
        with closing(conn), instance_lock(conn, settings.db_path):
            init_schema(conn)
            # Fail before any API call when the reviewed CSV is incomplete or unsafe.
            if preview_backfill(conn, records):
                raise MetadataError("metadata_migration.csv_must_cover_all_missing_metadata_before")
            actor = asyncio.run(_actor(settings))
            apply_backfill(conn, records, actor)
        print(json.dumps({"mode": "applied", "records": len(records), "remaining": 0}))
        return 0
    with closing(_preview_database(settings)) as conn:
        if args.command == "backfill":
            records = read_backfill(args.path)
            remaining = preview_backfill(conn, records)
            print(json.dumps({"mode": "dry_run", "records": len(records), "remaining": remaining}))
            return 0 if remaining == 0 else 2
        missing = MetadataRepo(conn).missing()
        if args.command == "check":
            print(json.dumps({"missing": missing}, indent=2))
            return 2 if missing else 0
        with args.path.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(missing)
        print(json.dumps({"mode": "export", "records": len(missing)}))
        return 0


def run() -> None:
    parser, argv = parser_for("cli.metadata.description")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help=parser.text("cli.metadata.check"))
    export = commands.add_parser("export", help=parser.text("cli.metadata.export"))
    export.add_argument("path", type=Path, help=parser.text("cli.metadata.path"))
    backfill = commands.add_parser("backfill", help=parser.text("cli.metadata.backfill"))
    backfill.add_argument("path", type=Path, help=parser.text("cli.metadata.path"))
    mode = backfill.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=parser.text("cli.import.dry_run"))
    mode.add_argument("--apply", action="store_true", help=parser.text("cli.metadata.apply"))
    args = parser.parse_args(argv)
    try:
        result = execute(load_settings(), args)
    except MetadataError as exc:
        parser.exit(2, parser.text("cli.metadata.rejected", error=error_text(exc, parser.language)))
    except csv.Error:
        parser.exit(2, parser.text("cli.metadata.rejected", error=parser.text("error.csv")))
    except Exception:
        parser.exit(2, parser.text("cli.metadata.failed"))
    else:
        raise SystemExit(result)


if __name__ == "__main__":
    run()
