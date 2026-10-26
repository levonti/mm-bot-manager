# Working-instance upgrade to v0.3.1, 2026-10-26

Result: **PASS**. The existing Docker Compose application was upgraded from
published v0.3.0 to published v0.3.1 after a stopped-application database backup
was fully restored and compared in an isolated validation server.

## Release and runtime identity

- Release revision: `adbccd5f71e8c9c290007355cf5f5a5a0f2bf55a`.
- Pinned image:
  `levonti17/mm-bot-manager@sha256:10b0b42e7558f0069b042b4564709e2ab106446ee336979e5a14d6c64d792b85`.
- Image platform: Linux amd64; existing platform overlay retained in the new
  deployment backup directory.
- The OCI labels, CLI, Mattermost `!version` and installed package all agree on
  version 0.3.1 and the complete release revision.
- All 52 installed Python/JSON source files match the exact release commit.
- Application container: `mm-bot-manager-mm-bot-manager-1`; started at
  `2026-10-26T20:12:38.439479478Z` (23:12 Moscow time).
- `.env` now pins the exact release digest. All other resolved application
  environment values match the preceding running instance; secrets were not
  printed or copied into tracked evidence.

Only the application service was recreated, using `--no-deps --no-build --pull
never`. PostgreSQL retained its original container ID, volume and start time
`2026-10-21T11:23:37.735204458Z`. No database container restart/recreation or
production database restore was performed.

## Backup and rollback material

Protected local directory:

`/Users/levonti/dev/personal/mm-bot-manager/data/release-v0.3.1/deployment-20261026T201134Z`

- Final custom-format backup: `final.dump`, **35585 bytes**.
- SHA-256:
  `7ed9dd1d75adebe70658b94cacb7e5e4b4bac4fecf419427a079deaca3bba7a0`.
- The application was stopped before this backup. `pg_restore --list` accepted
  the archive, then a full restore into a separate temporary PostgreSQL 15 server
  succeeded. The validator had no network access or published ports. Row counts
  and canonical row hashes matched for **all 14 public tables**.
- The temporary validation server was removed afterward.
- Directory permissions are 0700; database archive and configuration backup are
  0600. The backup directory is ignored by Git. The live `.env` remains 0600.
- Prior image retained as
  `mm-bot-manager:rollback-v0.3.1-deployment-20261026t201134z`.
- `env.before`, `compose.rollback.yaml` and the platform overlay retain the
  previous configuration. An automatic image/config rollback was prepared for
  startup failure; it was not needed.

These are local backups, not remote or off-site copies. Backup contents,
credentials, private server addresses and user-supplied repository URLs are
excluded from this report. Do not restore this pre-update database over later
writes for an ordinary application rollback; retain the current database.

## Live verification

Final stable check: `2026-10-26T20:15:25.248475+00:00` (23:15 Moscow time).

- `/health/live`: **200**; `/health/ready`: **200**.
- Mattermost WebSocket authentication confirmed.
- Existing operator sent `!version`, `!help` and `!list` to the manager's real DM;
  all three received valid bot responses. Version response contained 0.3.1 and
  the full release SHA; help retained feedback commands.
- No temporary accounts, access tokens, bot registrations or feedback tickets
  were created. Test posts and normal command-processing records remain as
  evidence of these checks.
- Schema versions remain `[1, 2]`; no schema migration was introduced.
- Eight protected business tables retained their pre-update row counts/hashes:
  `bot`, `token`, `pending_bot_request`, `metadata_audit`, `bot_import`,
  `bot_update_request`, `operation`, `user_preference`.
- Missing inventory metadata: **0**; pending feedback notifications: **0**.
- Application error/traceback events: **0**; restart count: **0**.
- Runtime protection check passes in the actual running container: UID/GID 1000,
  all capability sets including the bounding set zero, no-new-privileges,
  read-only root and writable `/tmp`; no setuid/setgid files or removed vulnerable
  CLI entry points. TLS CA loading and application imports work.

Detailed sanitized checks, source/config backups and the verified database dump
are retained in the protected ignored directory above. The application release
remains the previously published tag; this report does not alter that tag.
