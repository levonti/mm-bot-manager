# Feedback MVP: production deployment, 2026-10-26

Result: **deployed and verified** in the existing Docker Compose instance.

## Release identity

- Repository: `mm-bot-manager`; branch `ai/feedback-mvp`.
- Deployed source revision: `8bf49cda042101a97bd9431450f20f22e77c72ba`.
- Feedback implementation commit: `70b6edce5e08d3e89c90213795a9109790efd68d`.
- Release image: `mm-bot-manager:feedback-8bf49cda0421`.
- Running image ID: `sha256:19bfeb15e1fb9e344cca550766492e131cabf6c5c8d56622cd91b503e095d134`.
- Application container: `mm-bot-manager-mm-bot-manager-1`.
- Container started at `2026-10-26T15:23:19.834420705Z` (18:23 MSK).
- All 50 package Python/JSON files in the running container match
  the source tree. Image revision label matches the deployed source revision.
- Enabled `MM_FEEDBACK_HANDLERS=levonti` in the existing ignored `.env` file;
  the account was verified as the current user's active human account on Mattermost.
- Existing approver, catalog, credential, server and database settings were preserved.
  The Compose defaults for worker limits, language and display timezone now apply;
  those settings were unsupported by the previous running image.
- The existing `.env` is mode 0600. It is not tracked by Git or included in the image.
- No new inbound HTTP callback endpoint was configured.

## Backup and migration

The previous running database had three tables. A custom-format PostgreSQL backup was
created and checked with `pg_restore --list`. It was restored into a separate validation
database, and the new image successfully applied migrations 1 and 2 there. Original
columns and records were compared by canonical row hashes; all matched. The validation
database was then removed.

Before switching the application, it was stopped and a final backup taken. PostgreSQL
was not restarted or recreated: its start time remains
`2026-10-21T11:23:37.735204458Z`. The new application applied migrations on startup.
Only the application service was recreated, with `--no-deps --no-build --pull never`.

After deployment and the smoke test, original columns and rows still matched the final
pre-deployment snapshot:

| Original table | Rows preserved |
| --- | ---: |
| `bot` | 2 |
| `pending_bot_request` | 2 |
| `token` | 1 |

No existing bot, ownership, approval record or token metadata was rewritten. Existing
legacy records retain NULL repository/criticality metadata until explicit backfill.
A further verified custom-format backup was taken after migration and the smoke test.

Backup directory, ignored by Git and protected with mode 0700:
`/Users/levonti/dev/personal/mm-bot-manager/data/feedback-deployment/20261026T151742Z`.

| Backup | Bytes | SHA-256 |
| --- | ---: | --- |
| `final_backup.dump` (application stopped) | 5737 | `cd9b966d247fb1ab7ee1300933c3110d0fa2903552bdca061d91de7d26079081` |
| `post_backup.dump` (after verification) | 33808 | `c99bbeecd148a7cccd36e2d4cf35234b6f0087e89a7f45ab4380c1f8dd0629b5` |

Archives and the configuration backup `env.before` have mode 0600. Their contents,
server addresses, passwords and tokens are not included in this report.
These are local backups; no remote backup or registry publication was performed.

## Production checks

Six smoke-test groups passed on the actual production manager and PostgreSQL database:

1. `!help` includes feedback commands.
2. A temporary author created an explicitly labelled deployment-check ticket, and
   the real human handler received its notification.
3. The author exercised list/status/history and the human handler exercised queue.
4. Assignment, waiting for details, author reply and handler reply all succeeded;
   real Mattermost DMs were observed in both directions.
5. Resolution, closure, reopening, assignment reset and final closure succeeded.
   Test ticket `FB-1` finished closed with 9 history events and no pending
   notifications. Its history is retained as test evidence.
6. The existing `!list` command still returned successfully for the human operator.

The temporary author's token was revoked and verified absent from its active token
list; its account was disabled and the deactivation verified. No test handler role
remains in production: `levonti` is the configured human handler.

Final checks at `2026-10-26T15:26:35.979839+00:00` (18:26 MSK):

- `/health/live`: 200; `/health/ready`: 200.
- WebSocket authentication acknowledged by Mattermost.
- PostgreSQL healthy; schema migration versions `[1, 2]`.
- Running container environment equals the resolved Compose environment.
- Container restart count: 0; observed application error events: 0.
- Global pending feedback notifications: 0.

The earlier isolated live acceptance covered 13 groups on Mattermost 10.11.24. The
preceding automated suite passed 844 tests with 1 skip and 94.94% coverage. Source code
was unchanged for this deployment/report task; the entire automated suite was not rerun.

## Rollback material and follow-up

- Retained previous image: `mm-bot-manager:rollback-feedback-20261026t151742z`.
- Previous image ID: `sha256:5ae8c629af91fe0f42c5ca6e8b1cf212167614d19f7a6959c35f7cfa1becfed4`.
- Protected previous environment: `env.before` in the backup directory above.
- The switch procedure had a fallback to the previous image and configuration if
  startup checks failed; fallback was not needed. No database restore was performed.
- Returning to the previous application image would disable feedback. Keep all added
  tables and new records intact; restoring a pre-deployment database would discard
  subsequent writes and requires a separate recovery decision.
- Git commits and Docker images remain local; no push, merge request or remote pipeline
  was created by this deployment.

Next task: obtain authoritative repository URLs and criticality values for the two
legacy bots and two legacy request records, then backfill them through the documented
metadata workflow. Values were not guessed or changed during this deployment.
