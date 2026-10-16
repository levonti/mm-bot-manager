"""Backfill legacy inventory attributes without inventing repository URLs or criticality."""

import argparse
import asyncio
import csv
import json
from contextlib import closing
from pathlib import Path

from .__main__ import _connect_database
from .config import Settings, load_settings
from .db import DbConn, init_schema, transaction
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
            raise MetadataError(
                "CSV header must be record_type,record_id,git_repository,criticality."
            )
        for row in reader:
            if len(records) >= 10000 or None in row or any(not v for v in row.values()):
                raise MetadataError("Invalid CSV row or batch exceeds 10000 records.")
            key = row["record_type"], row["record_id"]
            if key[0] not in {"bot", "request"} or key in seen:
                raise MetadataError("Record types must be bot/request, with no duplicate targets.")
            seen.add(key)
            records.append((*key, validate_metadata(row["git_repository"], row["criticality"])))
    if not records:
        raise MetadataError("Backfill CSV is empty.")
    return records


def preview_backfill(conn: DbConn, records: list[tuple[str, str, BotMetadata]]) -> int:
    for kind, record_id, metadata in records:
        table, key = (
            ("bot", "bot_user_id") if kind == "bot" else ("pending_bot_request", "request_id")
        )
        if kind not in {"bot", "request"}:
            raise MetadataError("Invalid record type.")
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)).fetchone()
        if row is None:
            raise MetadataError("Backfill target not found.")
        data = dict(row)
        if kind == "request" and data["status"] != "pending":
            raise MetadataError("Only pending legacy requests may be backfilled.")
        old = data.get("git_repository"), data.get("criticality")
        if None not in old and old != (metadata.git_repository, metadata.criticality):
            raise MetadataError("Backfill cannot replace existing metadata; use an audited update.")
    covered = {(kind, record_id) for kind, record_id, _ in records}
    return sum(
        (r["record_type"], r["record_id"]) not in covered for r in MetadataRepo(conn).missing()
    )


def apply_backfill(conn: DbConn, records: list[tuple[str, str, BotMetadata]], actor: str) -> None:
    with transaction(conn):
        if preview_backfill(conn, records):
            raise MetadataError("CSV must cover all missing bot and pending-request metadata.")
        for kind, record_id, metadata in records:
            MetadataRepo(conn).set(
                kind, record_id, metadata, actor=actor, source="migration", only_missing=True
            )
        if MetadataRepo(conn).missing():
            raise MetadataError("Incomplete backfill; the entire batch was rolled back.")


async def _actor(settings: Settings) -> str:
    async with MattermostClient(
        settings.mm_rest_base, settings.mm_token, verify_ssl=settings.mm_verify_ssl
    ) as mm:
        account = await mm.get_me()
        if not account.get("id") or account.get("is_bot", False) or account.get("delete_at") != 0:
            raise MetadataError("Backfill requires an authenticated active human MM_TOKEN account.")
        return str(account["id"])


def execute(settings: Settings, args: argparse.Namespace) -> int:
    if args.command == "backfill" and args.apply:
        records = read_backfill(args.path)
        conn = _connect_database(settings)
        with closing(conn), instance_lock(conn, settings.db_path):
            init_schema(conn)
            # Fail before any API call when the reviewed CSV is incomplete or unsafe.
            if preview_backfill(conn, records):
                raise MetadataError("CSV must cover all missing metadata before --apply.")
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
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check", help="Report missing metadata; exit 2 until all records are filled"
    )
    export = commands.add_parser("export", help="Export missing records into a new CSV file")
    export.add_argument("path", type=Path)
    backfill = commands.add_parser("backfill", help="Validate or atomically fill legacy attributes")
    backfill.add_argument("path", type=Path)
    mode = backfill.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (default)")
    mode.add_argument(
        "--apply", action="store_true", help="Stop manager first; fill all missing rows"
    )
    args = parser.parse_args()
    try:
        result = execute(load_settings(), args)
    except (MetadataError, csv.Error) as exc:
        parser.exit(2, f"Metadata migration rejected: {exc}\n")
    except Exception:
        parser.exit(
            2, "Metadata migration failed; check input, database, lock and MM_TOKEN access.\n"
        )
    else:
        raise SystemExit(result)


if __name__ == "__main__":
    run()
