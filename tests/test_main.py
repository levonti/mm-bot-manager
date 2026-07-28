from unittest.mock import Mock

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
