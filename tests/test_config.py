import pytest
from pydantic import ValidationError

from mm_bot_manager.config import Settings

BASE = {
    "mm_url": "https://mm.example.com",
    "mm_token": "admin-token",
    "mm_bot_token": "bot-token",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)  # type: ignore[call-arg]


def test_component_postgres_settings_are_accepted() -> None:
    settings = make_settings(
        db_host="postgresql",
        db_port=5433,
        db_name="mm_bot_manager",
        db_user="mm_bot",
        db_password="secret",
    )
    assert settings.db_host == "postgresql"
    assert settings.db_port == 5433


@pytest.mark.parametrize("missing", ["db_name", "db_user", "db_password"])
def test_component_postgres_settings_must_be_complete(missing: str) -> None:
    values: dict[str, object] = {
        "db_host": "postgresql",
        "db_name": "mm_bot_manager",
        "db_user": "mm_bot",
        "db_password": "secret",
    }
    values.pop(missing)
    with pytest.raises(ValidationError, match="DB_HOST requires"):
        make_settings(**values)


def test_db_url_takes_precedence_over_partial_component_settings() -> None:
    settings = make_settings(
        db_url="postgresql://user:pass@external/database",
        db_host="ignored",
    )
    assert settings.db_url == "postgresql://user:pass@external/database"
