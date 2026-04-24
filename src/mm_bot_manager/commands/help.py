from .context import CommandContext
from .parser import ParsedArgs

_LINES = [
    "**mm-bot-manager commands**",
    "",
    '- `!create <username> [--display-name "..."] [--description "..."]`',
    "    — request bot creation (goes to approval if approvers are configured)",
    "- `!list` — list bots you own",
    "- `!info <username>` — show details of a bot you own",
    '- `!update <username> [--username "..."] [--display-name "..."] [--description "..."]`',
    "    — update bot attributes",
    "- `!delete <username>` — disable a bot you own",
    '- `!token create <username> [--description "..."]`',
    "    — mint a personal access token for your bot (shown once)",
    "- `!token list <username>` — list active token IDs for your bot",
    "- `!token revoke <token_id>` — revoke a token you minted",
    "",
    "**Approvers only**",
    "- `!approve <request_id>` — approve a pending bot creation request",
    "- `!deny <request_id> [reason]` — deny a pending bot creation request",
    "",
    "- `!help` — show this message",
    "",
    "Ownership is scoped to your Mattermost user: you can only manage",
    "bots you created via this manager.",
]
HELP_TEXT = "\n".join(_LINES)


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    return HELP_TEXT
