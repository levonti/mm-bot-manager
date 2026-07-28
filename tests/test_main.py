import asyncio
from unittest.mock import Mock

import pytest

from mm_bot_manager import __main__ as main
from mm_bot_manager.__main__ import _connect_database
from mm_bot_manager.config import Settings
from mm_bot_manager.db import DbConn

BASE = {
    "mm_url": "https://mm.example.com",
    "mm_token": "admin-token",
    "mm_bot_token": "bot-token",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)  # type: ignore[call-arg]


def test_connect_database_prefers_db_url(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_postgres = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect_postgres", connect_postgres)

    result = _connect_database(
        make_settings(
            db_url="postgresql://user:pass@external/database",
            db_host="ignored",
        )
    )

    assert result is expected
    connect_postgres.assert_called_once_with("postgresql://user:pass@external/database")


def test_connect_database_uses_component_parameters(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_postgres = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect_postgres", connect_postgres)

    result = _connect_database(
        make_settings(
            db_host="postgresql",
            db_port=5432,
            db_name="mm_bot_manager",
            db_user="mm_bot",
            db_password="p@ss:/word",
        )
    )

    assert result is expected
    connect_postgres.assert_called_once_with(
        None,
        host="postgresql",
        port=5432,
        dbname="mm_bot_manager",
        user="mm_bot",
        password="p@ss:/word",
    )


def test_connect_database_falls_back_to_sqlite(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_sqlite = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect", connect_sqlite)

    result = _connect_database(make_settings(db_path="/tmp/mm-bot.sqlite3"))

    assert result is expected
    connect_sqlite.assert_called_once_with("/tmp/mm-bot.sqlite3")


async def test_main_waits_for_in_flight_health_handler_before_closing_db(
    monkeypatch,
) -> None:
    handler_cancelled = asyncio.Event()
    db_closed_after_handler: list[bool] = []

    class FakeConn:
        def close(self) -> None:
            db_closed_after_handler.append(handler_cancelled.is_set())

    class FakeMattermostClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_me(self):
            return {"id": "manager-id", "username": "manager"}

    class FakeHealthServer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def start_health_server(
        _db,
        _host,
        _port,
        handler_tasks: set[asyncio.Task[None]],
    ):
        async def in_flight_handler() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                handler_cancelled.set()

        task = asyncio.create_task(in_flight_handler())
        handler_tasks.add(task)
        task.add_done_callback(handler_tasks.discard)
        return FakeHealthServer()

    async def resolve_approvers(*_args):
        return frozenset()

    async def stream_events(*_args, **_kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError
        yield None

    monkeypatch.setattr(main, "load_settings", lambda: make_settings())
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setattr(main, "get_logger", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "_connect_database", Mock(return_value=FakeConn()))
    monkeypatch.setattr(main, "init_schema", lambda _conn: None)
    monkeypatch.setattr(main, "BotRepo", Mock())
    monkeypatch.setattr(main, "TokenRepo", Mock())
    monkeypatch.setattr(main, "PendingRequestRepo", Mock())
    monkeypatch.setattr(main, "MattermostClient", lambda *_args, **_kwargs: FakeMattermostClient())
    monkeypatch.setattr(main, "_resolve_approvers", resolve_approvers)
    monkeypatch.setattr(main, "MessageRouter", Mock())
    monkeypatch.setattr(main, "CommandContextFactory", Mock())
    monkeypatch.setattr(main, "start_health_server", start_health_server)
    monkeypatch.setattr(main, "stream_events", stream_events)

    with pytest.raises(asyncio.CancelledError):
        await main.main_async()

    assert db_closed_after_handler == [True]
