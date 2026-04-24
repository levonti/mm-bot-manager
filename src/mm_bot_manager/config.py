from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mm_url: HttpUrl = Field(
        ...,
        description="Base URL of the Mattermost server, e.g. https://mm.example.com",
    )
    mm_token: str = Field(
        ...,
        min_length=1,
        description="Personal access token from a system_admin user (for bot management API calls)",
    )
    mm_bot_token: str = Field(
        ...,
        min_length=1,
        description="Personal access token of the manager bot account (WS + posting)",
    )
    db_path: str = Field(default="/data/mm-bot-manager.sqlite3")
    db_url: str | None = Field(
        default=None,
        description="PostgreSQL DSN (e.g. postgresql://user:pass@host/db). "
        "When set, takes precedence over db_path.",
    )
    log_level: str = Field(default="INFO")
    mm_verify_ssl: bool = Field(default=True)
    # Comma-separated Mattermost usernames of users who can approve bot creation.
    # If empty, bot creation is immediate (no approval required).
    mm_approvers: str = Field(default="")

    @property
    def approver_usernames(self) -> list[str]:
        return [u.strip() for u in self.mm_approvers.split(",") if u.strip()]

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


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
