from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    mm_url: HttpUrl = Field(
        ...,
        description="Base URL of the Mattermost server, e.g. https://mm.example.com",
    )
    mm_token: str = Field(
        ...,
        min_length=1,
        repr=False,
        description="Personal access token from a system_admin user (for bot management API calls)",
    )
    mm_bot_token: str = Field(
        ...,
        min_length=1,
        repr=False,
        description="Generated access token of the manager bot account (WS + posting)",
    )
    db_path: str = Field(default="/data/mm-bot-manager.sqlite3")
    db_url: str | None = Field(
        default=None,
        repr=False,
        description="PostgreSQL DSN (e.g. postgresql://user:pass@host/db). "
        "When set, takes precedence over db_path.",
    )
    db_host: str | None = Field(default=None)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str | None = Field(default=None)
    db_user: str | None = Field(default=None)
    db_password: str | None = Field(default=None, repr=False)
    health_host: str = Field(default="127.0.0.1")
    health_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="INFO")
    mm_verify_ssl: bool = Field(default=True)
    # Approvers can review creation/import requests and read the global catalog.
    # If empty, creation is immediate and imports are disabled.
    mm_approvers: str = Field(default="")
    mm_catalog_viewers: str = Field(default="")
    mm_default_language: Literal["en", "ru"] = "en"
    max_workers: int = Field(default=4, ge=1, le=32)
    event_queue_size: int = Field(default=100, ge=1, le=10000)
    shutdown_timeout: float = Field(default=30, ge=1, le=300)

    @property
    def approver_usernames(self) -> list[str]:
        return [u.strip() for u in self.mm_approvers.split(",") if u.strip()]

    @property
    def catalog_viewer_usernames(self) -> list[str]:
        return [u.strip() for u in self.mm_catalog_viewers.split(",") if u.strip()]

    @property
    def mm_rest_base(self) -> str:
        return f"{str(self.mm_url).rstrip('/')}/api/v4"

    @property
    def mm_ws_url(self) -> str:
        base = str(self.mm_url).rstrip("/")
        if base.startswith("https://"):
            ws = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            ws = "ws://" + base[len("http://") :]
        else:
            ws = base
        return f"{ws}/api/v4/websocket"

    @model_validator(mode="after")
    def validate_database_settings(self) -> Self:
        if self.db_url is not None:
            return self
        if self.db_host is None:
            return self
        missing = [
            env_name
            for env_name, value in (
                ("DB_NAME", self.db_name),
                ("DB_USER", self.db_user),
                ("DB_PASSWORD", self.db_password),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"DB_HOST requires {', '.join(missing)}")
        return self


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
