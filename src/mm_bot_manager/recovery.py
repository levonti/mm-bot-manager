"""Operator-only recovery CLI. Run with the manager stopped and the same configuration."""

import asyncio
import json
from contextlib import closing
from dataclasses import asdict

from .__main__ import _connect_database
from .cli_i18n import parser_for
from .config import load_settings
from .db import DbConn, init_schema, transaction
from .dispatcher import CommandContextFactory
from .i18n import UserFacingError, error_text
from .mm_client import MattermostClient
from .operations import compensate, recover_operations
from .repository import BotRepo, OperationRecord, OperationRepo, PendingRequestRepo, TokenRepo
from .runtime import instance_lock


class RecoveryError(UserFacingError, ValueError):
    pass


def find_operation(conn: DbConn, operation_id: str) -> OperationRecord:
    for op in OperationRepo(conn).unfinished():
        if op.operation_id == operation_id:
            return op
    raise RecoveryError("recovery.not_found")


def acknowledge_missing(conn: DbConn, operation_id: str) -> None:
    op = find_operation(conn, operation_id)
    if op.status != "needs_review" or op.remote_id is not None:
        raise RecoveryError("recovery.only_uncertain")
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
                raise RecoveryError("recovery.only_bot")
            user = await admin.get_user_by_username(op.resource)
            if user.get("id") != remote_id or not user.get("is_bot"):
                raise RecoveryError("recovery.wrong_id")
            assert remote_id is not None
            # Persist the explicitly reviewed mapping before attempting compensation.
            ctx.operation_repo.update(operation_id, "remote_created", remote_id)
            return (
                0 if await compensate(ctx, operation_id, op.kind, remote_id, op.request_id) else 1
            )
        return await recover_operations(ctx)


def main() -> int:
    parser, argv = parser_for("cli.recovery.description")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help=parser.text("cli.recovery.list"))
    commands.add_parser("retry", help=parser.text("cli.recovery.retry"))
    missing = commands.add_parser("acknowledge-missing", help=parser.text("cli.recovery.missing"))
    missing.add_argument("operation_id", help=parser.text("cli.recovery.operation"))
    missing.add_argument(
        "--reviewed-no-resource",
        action="store_true",
        required=True,
        help=parser.text("cli.recovery.reviewed"),
    )
    attach = commands.add_parser("attach-bot", help=parser.text("cli.recovery.attach"))
    attach.add_argument("operation_id", help=parser.text("cli.recovery.operation"))
    attach.add_argument("remote_id", help=parser.text("cli.recovery.remote"))
    attach.add_argument(
        "--reviewed", action="store_true", required=True, help=parser.text("cli.recovery.reviewed")
    )
    args = parser.parse_args(argv)
    try:
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
    except RecoveryError as exc:
        parser.exit(2, parser.text("cli.recovery.rejected", error=error_text(exc, parser.language)))
    except Exception:
        parser.exit(2, parser.text("cli.recovery.failed"))


if __name__ == "__main__":
    raise SystemExit(main())
