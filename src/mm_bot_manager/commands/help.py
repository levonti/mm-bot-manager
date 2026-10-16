from .context import CommandContext
from .parser import ParsedArgs

_LINES = [
    "**mm-bot-manager commands**",
    "",
    "- `!create <username> --repo <url> --criticality <1|2|3> "
    '[--display-name "..."] [--description "..."]`',
    "    — request bot creation (goes to approval if approvers are configured)",
    "- `!import <username> --repo <url> --criticality <1|2|3>` — request import in DM",
    "- `!list` — list bots you own",
    "- `!catalog [--criticality <1|2|3>] [--owner <name>] [--search <text>] [--page <n>]`",
    "    — global registry in DM; approvers and MM_CATALOG_VIEWERS only",
    "- `!info <username>` — show details of a bot you own",
    '- `!update <username> [--username "..."] [--display-name "..."] [--description "..."]`',
    "    — update bot attributes",
    "- `!update <username> [--repo <url>] [--criticality <1|2|3>]` — audited registry update in DM",
    "    — owner or approver; separate from remote updates",
    "Criticality: 1 critical, 2 important, 3 auxiliary. Both attributes are required.",
    "- `!delete <username>` — disable a bot you own",
    '- `!token create <username> [--description "..."]`',
    "    — mint a personal access token in DM (the message remains in DM history)",
    "- `!token list <username>` — list active token IDs tracked by this manager",
    "- `!token revoke <token_id>` — revoke a token you minted",
    "",
    "**Approvers only**",
    "- `!approve <request_id>` — approve a pending creation or import request",
    "- `!deny <request_id> [reason]` — deny a pending creation or import request",
    "",
    "- `!help` — show this message",
    "",
    "Ownership is scoped to your Mattermost user: you can only manage",
    "bots you created via this manager or imported with explicit approval.",
]
HELP_TEXT = "\n".join(_LINES)


async def handle(ctx: CommandContext, args: ParsedArgs) -> str:
    return HELP_TEXT
