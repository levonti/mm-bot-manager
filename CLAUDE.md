# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                        # install all deps (runtime + dev)
uv run pytest                  # run all tests
uv run pytest tests/test_commands.py::test_create_and_list_and_info  # single test
uv run ruff check src tests    # lint
uv run ruff format src tests   # format
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
- `bot_mm` — uses `MM_BOT_TOKEN` (manager bot account PAT); used for WebSocket auth, posting replies, and DM-ing approvers/requesters.

**`CommandContext`** (frozen dataclass in `commands/context.py`) is built per-message by `CommandContextFactory` and carries both clients, all repos, the manager's own `user_id`, and the set of approver MM user IDs.

**Approval flow** (active when `MM_APPROVERS` is set):
- `create` stores a `pending_bot_request` row and DMs each approver via `bot_mm`.
- Approver replies `approve <id>` or `deny <id> [reason]`; the handler calls the MM API, updates the DB, and DMs the requester.

## Configuration

| Variable | Purpose |
|---|---|
| `MM_URL` | Mattermost server base URL |
| `MM_TOKEN` | system_admin user PAT (bot management API) |
| `MM_BOT_TOKEN` | Manager bot account PAT (WS + posting) |
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

Ownership is enforced in the local DB only. The MM API records the manager bot as the MM-level owner of all bots it creates.

## Testing patterns

Tests use `FakeMM` (in `test_commands.py`) — a hand-written async stub that records calls. Repos are tested against an in-memory SQLite (`":memory:"`). HTTP calls are mocked with `respx`. `CommandContext` is constructed directly in tests; `CommandContextFactory` is not used.

To exercise the approval flow in tests, pass `approver_ids=frozenset(["approver-id"])` to the `make()` fixture helper.
