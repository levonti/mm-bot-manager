# mm-bot-manager

A Mattermost bot that lets any Mattermost user manage their own bot accounts by chatting
with it. The manager bot listens over the Mattermost websocket; users DM it (or @-mention
it in a channel) to create bots, edit their attributes, and mint/revoke personal access
tokens. Ownership metadata is stored in a SQLite or PostgreSQL database so users can only modify
bots they created through this manager.

## Requirements

- Python 3.14 (only needed for local dev; the container image bundles its own)
- [uv](https://docs.astral.sh/uv/) 0.11+
- A Mattermost server and an admin who can create the manager bot account

## Mattermost setup

### 1. Enable bot account creation

In *System Console → Integrations → Bot Accounts*, set **Enable Bot Account Creation** to
**true**. Without this, the API call to create bots will return 403.

### 2. Create the manager bot and get its token

> **Important:** `MM_TOKEN` must be a Personal Access Token from a **regular user account
> with system_admin role** — not a bot account token. Bot tokens cannot call the bot
> management API.

Steps:
1. Log in as a system_admin user.
2. Go to *Account Settings → Security → Personal Access Tokens* and create a token.
3. Use that token as `MM_TOKEN` in your `.env`.

The system_admin account that owns the token needs:

- `create_bot`, `read_bots`, `manage_others_bots`
- `create_user_access_token`, `read_user_access_token`, `revoke_user_access_token`
- `edit_other_users` (for username changes)

All of these are included in the default `system_admin` role.

### 3. Create the manager bot account and get its token

Create a dedicated bot account (*Integrations → Bot Accounts*) so the manager has its own
identity in Mattermost. Copy its username — this is the account users will DM or @mention.
Generate a Personal Access Token for this bot account and use it as `MM_BOT_TOKEN`. The
manager uses this token for the WebSocket connection and for posting replies.

## Configuration

Create a `.env` file (or export variables directly) and fill it in:

| Variable        | Required | Default                           | Description                                              |
| --------------- | -------- | --------------------------------- | -------------------------------------------------------- |
| `MM_URL`        | yes      | —                                 | Base URL of the MM server (`https://...`)                |
| `MM_TOKEN`      | yes      | —                                 | system_admin PAT (bot management API)                    |
| `MM_BOT_TOKEN`  | yes      | —                                 | Manager bot account PAT (WebSocket + posting)            |
| `MM_APPROVERS`  | no       | —                                 | Comma-separated MM usernames who must approve `create`   |
| `MM_VERIFY_SSL` | no       | `true`                            | Set `false` for self-signed certificates                 |
| `DB_PATH`       | no       | `/data/mm-bot-manager.sqlite3`    | SQLite file path (ignored when `DB_URL` is set)          |
| `DB_URL`        | no       | —                                 | PostgreSQL DSN, e.g. `postgresql://user:pass@host/db`    |
| `LOG_LEVEL`     | no       | `INFO`                            | `DEBUG` / `INFO` / `WARNING` / `ERROR`                   |

## Commands

Talk to the manager bot via DM, or @-mention it in any channel.

| Command                                                                                              | What it does                                                          |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `!help`                                                                                              | Show the command cheatsheet                                           |
| `!create <username> [--display-name "..."] [--description "..."]`                                    | Create a bot; you become its owner (may require approver approval)    |
| `!list`                                                                                              | List bots you own                                                     |
| `!info <username>`                                                                                   | Details on a bot you own                                              |
| `!update <username> [--username "..."] [--display-name "..."] [--description "..."]`                 | Update attributes                                                     |
| `!delete <username>`                                                                                 | Disable the bot in Mattermost and mark it deleted here                |
| `!token create <username> [--description "..."]`                                                     | Mint a personal access token; the token value is shown **once**       |
| `!token list <username>`                                                                             | List active token IDs for a bot you own                               |
| `!token revoke <token_id>`                                                                           | Revoke a token you minted                                             |
| `!approve <id>`                                                                                      | (Approvers only) Approve a pending `create` request                   |
| `!deny <id> [reason]`                                                                                | (Approvers only) Deny a pending `create` request                      |

## Running locally

```bash
uv sync
# set required env vars or create a .env file and source it
DB_PATH=./data/mm-bot-manager.sqlite3 uv run python -m mm_bot_manager
```

To use PostgreSQL instead of SQLite, install the driver and set `DB_URL`:

```bash
uv sync --extra postgres
DB_URL=postgresql://user:pass@localhost/mm_bot_manager uv run python -m mm_bot_manager
```

## Running with Docker Compose

The compose file starts the bot alongside a PostgreSQL 15 container. Data is stored in
a named volume (`pgdata`) and survives restarts.

```bash
cp .env.example .env   # fill in MM_URL, MM_TOKEN, MM_BOT_TOKEN, etc.
docker compose up -d
docker compose logs -f
```

`POSTGRES_PASSWORD` defaults to `secret`; override it in `.env` for anything
beyond local testing.

## Development

```bash
uv sync                       # install deps + dev deps
uv run pytest -q              # unit tests
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src               # type-check
```

## Security notes

- The manager stores only token **IDs** and descriptions, never the token values. A
    minted token is returned to the user **once** in the reply post and then forgotten.
- Ownership is enforced in the local DB. If someone bypasses this manager and edits a bot
    directly via the Mattermost API, the manager cannot know — trust only bots created
    through it.
- The manager bot's own PAT is highly privileged. Keep it out of source control and scope
    access to the `.env` file appropriately.
