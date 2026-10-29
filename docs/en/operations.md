# Installation and operations

[English](operations.md) · [Русский](../ru/operations.md) · [Deutsch](../de/operations.md)

## Prepare Mattermost

Enable Bot Account Creation and Personal Access Tokens in System Console.
Create a personal token from a regular system_admin user: MM_TOKEN. A bot token
cannot create other bots. Create a dedicated manager bot: its token is MM_BOT_TOKEN.
Add the manager to teams/channels where mentions should work. DMs need no channel setup.

## Configure and start

Copy `.env.example` to `.env`, restrict its permissions, and set MM_URL, MM_TOKEN,
MM_BOT_TOKEN. Never commit credentials. Set MM_DEFAULT_LANGUAGE=en (en/ru/de),
MM_DISPLAY_TIMEZONE=UTC or an IANA timezone. Keep MM_VERIFY_SSL=true with trusted CA.
MM_APPROVERS, MM_CATALOG_VIEWERS and MM_FEEDBACK_HANDLERS are comma-separated usernames
with independent roles. Empty feedback handlers disables tickets. Restart after changes.

Database precedence: DB_URL, then complete DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD,
then DB_PATH (SQLite). Persist database storage. For Compose, configure the pinned
MM_BOT_MANAGER_IMAGE and run `docker compose up -d`. Inspect `docker compose logs`.
The container runs without root and with a read-only filesystem. Do not override this.
For local development use Python 3.14, `uv venv .venv`, `uv sync --frozen --extra postgres`,
then `uv run --no-sync mm-bot-manager --language en`.

For Kubernetes, set mattermost.defaultLanguage to en/ru/de in a site overlay; provide
credentials through existing Secrets or External Secrets Operator. Select database,
storage and kube-context before deployment. Follow the detailed
[Kubernetes runbook](../kubernetes.md) and [chart reference](../../deploy/helm/mm-bot-manager/README.md)
for commands and required site settings. The deployment helper validates the saved site
selection before installation. These platform-specific references are maintained in English.

## Upgrade, backup and recovery

Stop the old manager before upgrading; never run two managers against one database.
Back up PostgreSQL with pg_dump and verify the archive with pg_restore --list plus a
restore into an isolated database. For SQLite, stop the manager and use the SQLite
backup API or `.backup` command; do not copy an active database without its WAL.
Keep backups private. Verify bot ownership, requests, preferences and feedback after restore.
For managed PostgreSQL in Kubernetes use scripts/kubernetes-db.sh and its verification
procedure in the runbook. A restore overwrites subsequent writes; test in isolation first.

Migration 3 expands language preferences and preserves language and catalog format.
It runs transactionally on startup. Before rolling the application back, stop it and
run `UPDATE user_preference SET language = 'auto' WHERE language = 'de';` against the
same database. Keep the expanded constraint and migration record; do not downgrade the
schema. Restoring an older backup loses newer writes. Back up before every change.

## Diagnose

Check `!version`, startup logs and the configured local health endpoint. A 403 during
bot creation usually means MM_TOKEN is not a regular administrator PAT or bot creation
is disabled. For API failures retain the diagnostic ID, never raw tokens.
For unknown write outcomes do not blindly repeat commands. Stop the manager and inspect
`uv run --no-sync python -m mm_bot_manager.recovery --language en --help` first.
Administrative CLI help works without server credentials. Use --language ru/de as needed.
A language change does not grant permissions or translate user-submitted text.
