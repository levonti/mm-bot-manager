"""Operator-only recovery CLI. Run with the manager stopped and the same configuration."""

import argparse
import asyncio
import json
from contextlib import closing
from dataclasses import asdict

from .__main__ import _connect_database
from .config import load_settings
from .db import DbConn, init_schema, transaction
from .dispatcher import CommandContextFactory
from .mm_client import MattermostClient
from .operations import compensate, recover_operations
from .repository import BotRepo, OperationRecord, OperationRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock


def find_operation(conn: DbConn, operation_id: str) -> OperationRecord:
    for op in OperationRepo(conn).unfinished():
        if op.operation_id == operation_id:
            return op
    raise ValueError("Unfinished operation not found")


def acknowledge_missing(conn: DbConn, operation_id: str) -> None:
    op = find_operation(conn, operation_id)
    if op.status != "needs_review" or op.remote_id is not None:
        raise ValueError("Only uncertain operations without a remote ID can be acknowledged")
    with transaction(conn):
        OperationRepo(conn).update(operation_id, "compensated")
        if op.request_id:
            PendingRequestRepo(conn).release(op.request_id)


async def recover(
    conn: DbConn, *, operation_id: str | None = None, remote_id: str | None = None
) -> int:
    settings = load_settings()
    async with MattermostClient(
        settings.mm_rest_base, settings.mm_token, verify_ssl=settings.mm_verify_ssl
    ) as admin:
        factory = CommandContextFactory(
            BotRepo(conn),
            TokenRepo(conn),
            PendingRequestRepo(conn),
            admin,
            admin,
            "operator",
            frozenset(),
        )
        ctx = factory.build(caller_user_id="operator", caller_username="operator", channel_id="")
        if operation_id is not None:
            op = find_operation(conn, operation_id)
            if op.kind != "bot_create" or op.status != "needs_review" or op.remote_id is not None:
                raise ValueError("Only uncertain bot creation without a remote ID can be attached")
            user = await admin.get_user_by_username(op.resource)
            if user.get("id") != remote_id or not user.get("is_bot"):
                raise ValueError("The supplied ID is not the bot named by this operation")
            assert remote_id is not None
            # Persist the explicitly reviewed mapping before attempting compensation.
            ctx.operation_repo.update(operation_id, "remote_created", remote_id)
            return (
                0 if await compensate(ctx, operation_id, op.kind, remote_id, op.request_id) else 1
            )
        return await recover_operations(ctx)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List unfinished operations; never prints credentials")
    commands.add_parser("retry", help="Retry startup recovery of unfinished operations")
    missing = commands.add_parser(
        "acknowledge-missing", help="Confirm manual verification of absence"
    )
    missing.add_argument("operation_id")
    missing.add_argument("--reviewed-no-resource", action="store_true", required=True)
    attach = commands.add_parser("attach-bot", help="Disable an explicitly reviewed incomplete bot")
    attach.add_argument("operation_id")
    attach.add_argument("remote_id")
    attach.add_argument("--reviewed", action="store_true", required=True)
    args = parser.parse_args()
    settings = load_settings()
    conn = _connect_database(settings)
    with closing(conn), instance_lock(conn, settings.db_path):
        init_schema(conn)
        if args.command == "list":
            print(json.dumps([asdict(op) for op in OperationRepo(conn).unfinished()], indent=2))
            return 0
        if args.command == "acknowledge-missing":
            acknowledge_missing(conn, args.operation_id)
            return 0
        if args.command == "attach-bot":
            return asyncio.run(
                recover(conn, operation_id=args.operation_id, remote_id=args.remote_id)
            )
        return 1 if asyncio.run(recover(conn)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
