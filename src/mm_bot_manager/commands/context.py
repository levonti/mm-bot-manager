from dataclasses import dataclass

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
    approver_ids: frozenset[str]  # empty = no approval required
    operation_repo: OperationRepo
    channel_type: str | None = None  # Unknown channels must never receive credentials.
