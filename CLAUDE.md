# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                        # runtime + dev deps (without optional PostgreSQL driver)
uv run pytest tests --ignore=tests/test_db_postgres.py  # tests that do not need Docker
uv sync --extra postgres       # install the PostgreSQL driver
uv run pytest                  # full suite (includes PostgreSQL; requires Docker)
uv run pytest tests/test_commands.py::test_create_and_list_and_info  # single test
uv run ruff check src tests    # lint
uv run ruff format --check src tests  # verify formatting
uv run mypy src                # type-check (strict)
```

Entry point: `python -m mm_bot_manager` (or `mm-bot-manager` CLI script after install).

## Architecture

The service is a long-running async process that connects to Mattermost via WebSocket and responds to DMs or @mentions.

**Request path:**
1. `ws_listener.py` — maintains a reconnecting WebSocket to `wss://<MM>/api/v4/websocket`, authenticates with `MM_BOT_TOKEN`, yields raw event dicts.
2. `dispatcher.py / MessageRouter` — filters `posted` events: passes through DM channel messages or channel posts that @mention the manager bot; strips the @mention prefix.
3. `dispatcher.py / handle_event` — dispatches to `commands.dispatch()`, then posts the reply using `bot_mm`.
4. `commands/__init__.py / dispatch()` — tokenises the message with `shlex`, matches a prefix (1 or 2 tokens) against `REGISTRY`, calls the matching handler.
5. Command handlers in `commands/` — async functions `handle(ctx, args) -> str`.

**Two HTTP clients run concurrently (both `MattermostClient`):**
- `admin_mm` — uses `MM_TOKEN` (system_admin PAT); used by command handlers for bot management REST calls (`create_bot`, `patch_bot`, `disable_bot`, token endpoints).
- `bot_mm` — uses `MM_BOT_TOKEN` (generated manager bot access token); used for WebSocket auth, posting replies, and DM-ing approvers/requesters.

**`CommandContext`** (frozen dataclass in `commands/context.py`) is built per-message by `CommandContextFactory` and carries both clients, all repos, the manager's own `user_id`, and the set of approver MM user IDs.

**Approval flow** (active when `MM_APPROVERS` is set):
- `create` stores a `pending_bot_request` row and DMs each approver via `bot_mm`.
- Approver replies `!approve <id>` or `!deny <id> [reason]`; the handler calls the MM API, updates the DB, and DMs the requester.

## Configuration

| Variable | Purpose |
|---|---|
| `MM_URL` | Mattermost server base URL |
| `MM_TOKEN` | system_admin user PAT (bot management API) |
| `MM_BOT_TOKEN` | Generated manager bot access token (WS + posting) |
| `MM_APPROVERS` | Comma-separated usernames; empty = no approval gate |
| `MM_VERIFY_SSL` | Set `false` for self-signed certs |
| `DB_PATH` | SQLite file path (default `/data/mm-bot-manager.sqlite3`) |
| `DB_URL` | PostgreSQL DSN (e.g. `postgresql://user:pass@host/db`); when set, takes precedence over `DB_PATH` |
| `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

`MM_APPROVERS` usernames are resolved to MM user IDs at startup; the service exits with a clear error if any username is not found.

## Database

SQLite (default) or PostgreSQL 15+ (`DB_URL`). Install the PostgreSQL driver with `uv sync --extra postgres`. Three tables managed in `db.py` / `repository.py`:
- `bot` — bots created through the manager, keyed by MM `user_id`, scoped to `owner_user_id`.
- `token` — token IDs (never values) minted for bots, soft-revocable.
- `pending_bot_request` — approval queue; status: `pending` → `approved` | `denied`.

Ownership is enforced in the local DB only. Because `create_bot()` does not send an explicit
`owner_id`, Mattermost records the regular system-admin account behind `MM_TOKEN` as the
Mattermost-level owner of the bots.

## Testing patterns

Tests use `FakeMM` (in `test_commands.py`) — a hand-written async stub that records calls.
Repositories are tested against in-memory SQLite (`":memory:"`) and a PostgreSQL 15
testcontainer (`test_db_postgres.py`, requires Docker and the `postgres` extra). HTTP calls
are mocked with `respx`. `CommandContext` is constructed directly in tests;
`CommandContextFactory` is not used.

To exercise the approval flow in tests, pass `approver_ids=frozenset(["approver-id"])` to the `make()` fixture helper.

## Reliability contracts

- `CommandContext` now also requires `operation_repo: OperationRepo`; `channel_type`
  defaults to unknown. Token issuance is allowed only when `channel_type == "D"`.
- `operations.py` journals creation intent before API calls. It commits bot ownership,
  approval finalization and completion atomically; failed persistence/delivery triggers
  compensation. Never store token values or secret-bearing replies in the journal.
- Token handlers deliver the secret themselves in the requesting DM and return only a
  non-secret acknowledgement. Keep transport DEBUG frame logs disabled.
- Approval claims and incoming `post.id` claims are atomic SQL operations. Never replace
  them with an in-memory lock or automatically replay interrupted posts.
- `runtime.py` owns the bounded worker queue, shutdown drain and exclusive instance lock.
  Recovery must run under that lock before WebSocket intake. SQLite lock paths are canonical.
- `recovery.py` provides operator-only list/retry/manual resolution commands. An unknown
  bot outcome must never be resolved by automatically adopting a username match.
- Regression suites cover compensation, lost API responses, crash/restart, concurrent
  approvers, durable deduplication, queue backpressure, transport logs and WebSocket auth.
  `MM_TEST_POSTGRES_DSN` selects a disposable external test DB (as in CI); never use a
  production DSN. Without it, PostgreSQL tests use a Docker testcontainer.
- GitLab CI enforces lint, formatting, strict types and at least 85% combined statement/branch
  coverage. A local pass does not establish live Mattermost compatibility or a remote CI pass.
