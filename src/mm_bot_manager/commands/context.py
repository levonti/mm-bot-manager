from dataclasses import dataclass
from typing import Any

from ..i18n import error_text, tr
from ..localization import LanguageResolver
from ..mm_client import MattermostClient
from ..repository import BotRepo, OperationRepo, PendingRequestRepo, TokenRepo


@dataclass(frozen=True, slots=True)
class CommandContext:
    caller_user_id: str
    caller_username: str
    channel_id: str
    bot_repo: BotRepo
    token_repo: TokenRepo
    pending_repo: PendingRequestRepo
    mm: MattermostClient  # admin client — bot management REST calls
    bot_mm: MattermostClient  # bot client — posting DMs and replies
    manager_user_id: str  # the bot account's own MM user_id
    approver_ids: frozenset[str]  # empty permits immediate creation, but forbids imports
    operation_repo: OperationRepo
    channel_type: str | None = None  # Unknown channels must never receive credentials.
    catalog_viewer_ids: frozenset[str] = frozenset()
    language: str = "en"
    default_language: str = "en"
    language_resolver: LanguageResolver | None = None

    def tr(self, key: str, **params: Any) -> str:
        return tr(self.language, key, **params)

    def error(self, error: BaseException) -> str:
        return error_text(error, self.language)
