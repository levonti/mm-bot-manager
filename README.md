# mm-bot-manager

A Mattermost bot that lets any Mattermost user manage their own bot accounts by chatting
with it. The manager bot listens over the Mattermost WebSocket; users DM it (or @-mention
it in a channel) to create bots, edit their attributes, and mint/revoke access tokens.
Ownership metadata is stored in a SQLite or PostgreSQL database so users can only modify
bots they created through this manager.

## Requirements

- Python 3.14 (only needed for local dev; the container image bundles its own)
- [uv](https://docs.astral.sh/uv/) 0.11+
- A Mattermost server and an admin who can create the manager bot account

## Mattermost setup

### 1. Enable bot account creation

In *System Console → Integrations → Bot Accounts*, set **Enable Bot Account Creation** to
**true**. Without this, the API call to create bots will return 403. See Mattermost's
[bot account documentation](https://developers.mattermost.com/integrate/reference/bot-accounts/).

### 2. Create an administrator token

> **Important:** `MM_TOKEN` must be a Personal Access Token from a **regular user account
> with system_admin role** — not a bot account token. Bot accounts cannot create other
> bot accounts.

Steps:

1. Log in as a system_admin user.
2. In *System Console → Integrations → Integration Management*, enable
   **Personal Access Tokens** if they are disabled.
3. Go to *Profile → Security → Personal Access Tokens* and create a token.
4. Use that token as `MM_TOKEN` in your `.env`.

See Mattermost's
[Personal Access Token documentation](https://developers.mattermost.com/integrate/reference/personal-access-token/)
for the server and account settings.

The API calls made by this service require:

- `create_bot` and `manage_bots`
- `create_user_access_token` and `revoke_user_access_token`
- `edit_other_users` (tokens and username changes affect bot users)

All of these are included in the default `system_admin` role.

### 3. Create the manager bot account and get its token

Create a dedicated bot account (*Product menu → Integrations → Bot Accounts*) so the manager
has its own identity in Mattermost. Copy the generated bot access token before closing the
success screen and use it as `MM_BOT_TOKEN`. The manager uses this token for the WebSocket
connection and for posting replies.

Add the manager bot to every team and channel where users should be able to @mention it.
Users can also talk to it in a direct-message channel.

## Configuration

Create a `.env` file (or export variables directly) and fill it in:

| Variable        | Required | Default                           | Description                                              |
| --------------- | -------- | --------------------------------- | -------------------------------------------------------- |
| `MM_URL`        | yes      | —                                 | Base URL of the MM server (`https://...`)                |
| `MM_TOKEN`      | yes      | —                                 | system_admin PAT (bot management API)                    |
| `MM_BOT_TOKEN`  | yes      | —                                 | Manager bot access token (WebSocket + posting)            |
| `MM_APPROVERS`  | no       | empty                             | Comma-separated approver usernames; mandatory for imports |
| `MM_VERIFY_SSL` | no       | `true`                            | Set `false` for self-signed certificates                 |
| `DB_PATH`       | no       | `/data/mm-bot-manager.sqlite3`    | SQLite fallback (lowest database precedence)             |
| `DB_URL`        | no       | —                                 | PostgreSQL DSN (highest database precedence)             |
| `DB_HOST`       | no       | —                                 | PostgreSQL host; requires all component values below     |
| `DB_PORT`       | no       | `5432`                            | PostgreSQL port for component configuration              |
| `DB_NAME`       | no       | —                                 | PostgreSQL database name                                 |
| `DB_USER`       | no       | —                                 | PostgreSQL user                                          |
| `DB_PASSWORD`   | no       | —                                 | PostgreSQL password                                      |
| `HEALTH_HOST`   | no       | `127.0.0.1`                       | Local health listener bind address                       |
| `HEALTH_PORT`   | no       | `8080`                            | Local health listener port                               |
| `LOG_LEVEL`     | no       | `INFO`                            | `DEBUG` / `INFO` / `WARNING` / `ERROR`                   |
| `MAX_WORKERS` | no | `4` | Concurrent command handlers (1–32) |
| `EVENT_QUEUE_SIZE` | no | `100` | Bounded event queue (1–10000) |
| `SHUTDOWN_TIMEOUT` | no | `30` | Seconds to drain commands before cancellation (1–300) |

Database selection precedence is `DB_URL`, then the complete component PostgreSQL
configuration (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`), then
`DB_PATH` for SQLite.

## Commands

Talk to the manager bot via DM, or @-mention it in any channel.
Every command must start with `!`. New bot usernames passed to `!create` must end in `-bot`.

| Command                                                                                              | What it does                                                          |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `!help`                                                                                              | Show the command cheatsheet                                           |
| `!create <username> [--display-name "..."] [--description "..."]`                                    | Create a bot; you become its owner (may require approver approval)    |
| `!import <existing-bot-username>`                                                                     | **DM only:** request an existing bot import; always requires approval |
| `!list`                                                                                              | List bots you own                                                     |
| `!info <username>`                                                                                   | Details on a bot you own                                              |
| `!update <username> [--username "..."] [--display-name "..."] [--description "..."]`                 | Update attributes                                                     |
| `!delete <username>`                                                                                 | Disable the bot in Mattermost and mark it deleted here                |
| `!token create <username> [--description "..."]`                                                     | **DM only:** mint and deliver an access token in that DM               |
| `!token list <username>`                                                                             | List active token IDs tracked by this manager                        |
| `!token revoke <token_id>`                                                                           | Revoke a token you minted                                             |
| `!approve <id>`                                                                                      | (Approvers only) Approve a pending creation or import request         |
| `!deny <id> [reason]`                                                                                | (Approvers only) Deny a pending creation or import request            |

## Importing existing bots

In a DM, send `!import legacy-integration` (legacy names need no `-bot` suffix).
The requester becomes the proposed **local** owner. A configured `MM_APPROVERS`
user reviews the bot ID, current remote owner and proposed local owner, then sends
`!approve <request_id>` in a DM, or `!deny <request_id> [reason]`.
With no approvers configured, imports are disabled even though creation can be immediate.

Approval grants the local owner access to `!update`, `!delete` and `!token create`.
Import itself only reads Mattermost and registers the binding in a local transaction:
it does not rename, reassign, disable, create, or modify remote tokens. The original
remote owner stays unchanged. Old token values cannot be recovered; existing tokens
remain outside this manager's token registry. `!token list` reports only tracked tokens.

The manager revalidates the immutable bot ID and active human owners at approval.
Disabled accounts, plugin-owned bots, system/protected bots, elevated bot roles and
the manager itself are excluded. A changed username or remote owner requires denial
and a fresh request. An existing binding to the same local owner is a no-op;
another owner's binding or a locally deleted record requires a separate explicit
transfer/recovery procedure. `!info` shows import time, submitter and approver IDs.

### Bulk import CLI

Use the manager's environment and database. Prepare UTF-8 CSV with immutable
Mattermost user IDs (26 lowercase letters/digits), one explicit local human owner per bot:

```csv
bot_user_id,owner_user_id
bbbbbbbbbbbbbbbbbbbbbbbbbb,aaaaaaaaaaaaaaaaaaaaaaaaaa
cccccccccccccccccccccccccc,dddddddddddddddddddddddddd
```

```bash
# Read-only preview is the default; --dry-run makes it explicit.
uv run python -m mm_bot_manager.import_bots mapping.csv --dry-run

# Stop the manager first: submission uses its exclusive instance lock.
uv run python -m mm_bot_manager.import_bots mapping.csv --submit
```

The installed console command is `mm-bot-manager-import` with the same arguments.
Dry-run sends only GET requests and makes no registry/schema changes. It opens an
existing SQLite database read-only, or uses an in-memory schema if the file does not
exist; PostgreSQL uses a read-only session and must already have an initialized schema.
Preview can run alongside the manager and is informational: submission and approval
revalidate state. It produces JSON with resolved names, IDs and existing local bindings.

`--submit` validates the whole batch, then atomically saves pending requests (maximum
1000 rows; duplicate bot IDs are rejected). The authenticated `MM_TOKEN` account is
recorded as submitter; the CSV owner is the proposed beneficiary. Identical pending
requests reuse their IDs, while conflicts require denial first. Notifications are
best effort; JSON always includes the durable request IDs on successful submission.
No bots are imported by this command. Restart the manager and have an approver send
`!approve <id>` for **each** request. Batch submission is atomic; individual approvals
are independent. There is no CLI switch that bypasses this confirmation.

Startup/submission applies additive SQLite/PostgreSQL schema migrations, preserving
existing creation requests, bots and token records. Import audit data is committed
with the local binding and approval; failures roll back locally without remote cleanup.

## Running locally

```bash
uv sync
# export the required variables, or create a .env file (loaded automatically)
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

## Kubernetes

For Kubernetes `>= 1.21.1`, Helm 3 installation, pre-created Secrets, external or
single-instance internal PostgreSQL, Longhorn/local-path storage considerations,
Harbor in a closed contour, backup, restore, upgrade, and removal procedures, see
the [Kubernetes deployment runbook](docs/kubernetes.md).

## Development

```bash
uv sync                                                   # runtime + dev dependencies
uv run pytest -q tests --ignore=tests/test_db_postgres.py # fast SQLite/unit test suite
uv sync --extra postgres                                  # add PostgreSQL driver
uv run pytest -q                                          # full suite; requires Docker
uv run ruff check src tests                               # lint
uv run ruff format --check src tests                      # verify formatting
uv run mypy src                                           # type-check
uv run pytest --cov=mm_bot_manager --cov-branch --cov-fail-under=85
```

## Security notes

- The local database and operation journal store token **IDs**, never token values.
  Token creation is rejected in public, private and group channels. The secret is sent
  only in the requesting user's DM and is omitted from the ordinary command reply.
  **The DM remains in Mattermost history** and is subject to its access and retention
  policies; this is not a disappearing or one-time retrieval mechanism.
- Application DEBUG logging does not enable HTTP/WebSocket frame dumps. Incoming command
  bodies and remote exception bodies are omitted from dispatch and recovery logs.
- A failed or uncertain token delivery triggers revocation. If revocation fails, the
  journal retains the operation for recovery and blocks further issuance for that bot.
- Ownership is enforced in the local DB. If someone bypasses this manager and edits a bot
    directly via the Mattermost API, the manager cannot know — trust only bots created
    through it.
- `MM_TOKEN` grants system-admin access and is the most sensitive credential. `MM_BOT_TOKEN`
    can act as the manager bot in every channel that bot can access. Keep both out of source
    control and restrict access to the `.env` file.

## Reliability and recovery

Run one active instance per database. A PostgreSQL session advisory lock or SQLite
file lock prevents a second instance from running startup recovery against live commands.
SQLite storage and its adjacent `.lock` file must be on a local filesystem.

The additive startup schema creates `operation` and `processed_post` tables without
changing existing bot, token or request rows. Back up the database before upgrading;
retain the new tables with the other application data.

- Each API creation is preceded by a durable operation intent. Ownership, approval
  finalization and operation completion commit together. Failed persistence triggers
  disabling the incomplete bot or revoking the token.
- Approval claims use an atomic `pending → provisioning` transition. Concurrent approval
  or denial cannot override it. A definitive API rejection returns the request to
  `pending`; failed provisioning after creating a bot ends in `failed`.
- Startup recovery compensates unfinished operations with known remote IDs before
  accepting commands. Token descriptions receive a non-secret `[mm-bot-manager:<id>]`
  suffix so recovery can find and revoke a token after a lost creation response.
- An unknown bot-creation outcome becomes `needs_review` and blocks another creation
  for that username. The manager never adopts or disables an existing bot merely by name.
- Incoming `post.id` values are claimed atomically and retained across restarts. Duplicate
  events never rerun commands. Interrupted or failed-reply commands are not automatically
  replayed; inspect their outcome before sending a new command. This provides at-most-once
  execution of received posts, not guaranteed delivery: Mattermost events missed while
  disconnected and queued events lost on a forced stop are not backfilled.
- WebSocket authentication is acknowledged before events are dispatched. Network failures
  and temporary HTTP 500/502/503/504 responses reconnect with backoff and jitter. Invalid
  credentials fail closed. `ws://` works without a TLS context; use `https://` in production.
- SIGTERM/SIGINT stop intake and drain bounded workers. After `SHUTDOWN_TIMEOUT`, active
  commands are cancelled and incomplete creations remain recoverable. Compose allows
  65 seconds by default; set `STOP_GRACE_PERIOD` to at least `SHUTDOWN_TIMEOUT + 30` seconds
  if increasing the drain timeout.

Operator recovery commands require the same configuration and database as the manager.
Stop the manager first; the CLI takes the same instance lock. Never point test configuration
at the application database.

```bash
uv run python -m mm_bot_manager.recovery list
uv run python -m mm_bot_manager.recovery retry
```

`retry` exits nonzero while any operation remains unresolved. For `needs_review` without
a remote ID, inspect the Mattermost bot/token records and audit logs. Only after verifying
that no resource was created (and no original API request is still in flight), acknowledge
its absence to release the block:

```bash
uv run python -m mm_bot_manager.recovery acknowledge-missing <operation_id> --reviewed-no-resource
```

If an incomplete bot exists, verify its user ID, creation time and ownership against the
failed request. Explicitly attach that reviewed ID to the journal and disable it:

```bash
uv run python -m mm_bot_manager.recovery attach-bot <operation_id> <verified_bot_user_id> --reviewed
```

The command validates that the ID belongs to the bot username in the journal before
disabling it. The requester must use a different username or have an administrator resolve
the disabled account before creating another bot with that name.

## Continuous integration

GitLab CI runs lint, formatting, strict typing and the complete test suite with a disposable
PostgreSQL 15 service. The job requires at least **85% statement/branch coverage** and publishes
JUnit and coverage reports. No checks are allowed to fail silently.

Locally, PostgreSQL tests use testcontainers and Docker. CI supplies `MM_TEST_POSTGRES_DSN`
for its disposable service instead; this variable is only for a dedicated test database.
The repository does not change GitLab branch-protection or merge settings.
