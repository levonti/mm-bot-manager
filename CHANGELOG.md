# Changelog

## [Unreleased]

## [0.3.1] - 2026-10-26

### Security

- Remove mount, nsenter and infocmp entry points, unused system libpq dependencies
  and setuid/setgid bits from the runtime image. Run Compose as UID/GID 1000 with
  no capabilities, no privilege escalation, a read-only root filesystem and a
  temporary writable `/tmp`; runtime package installation and privileged CLI
  maintenance require a separate diagnostic container.
- Update vulnerable AnyIO, certifi, idna, pydantic-settings and development
  urllib3 dependencies, with minimum secure versions enforced by the resolver.
- Remove pip and its vendored dependencies from the final runtime image.

### Added

- Trivy dependency scans in every pipeline and release-image scans by digest before
  GitLab Release creation, with full reports and a gate for fixable HIGH/CRITICAL CVEs.

### Upgrade notes

- No database schema change; the migration 2 schema from v0.3.0 is retained.
- Compose now enforces UID/GID 1000, a read-only root filesystem, dropped
  capabilities and no-new-privileges. Keep application state in PostgreSQL and
  temporary files in `/tmp`; custom writable paths require explicit volumes.
- Use a separate diagnostic container for package installation or privileged
  maintenance. Root/privileged overrides invalidate the reviewed security profile.
- Raw scans retain Debian package-version findings; the documented applicability
  review does not suppress them or claim that retained libraries are patched.

## [0.3.0] - 2026-10-26

### Added

- Direct-message feedback tickets with author/handler access checks, assignment,
  state transitions, history and durable delivery of notifications.
- Optional `MM_FEEDBACK_HANDLERS` configuration for Compose and Helm.
- Compose release image pinning through `MM_BOT_MANAGER_IMAGE`.

### Upgrade notes

- Database migration 2 adds feedback tables and indexes on SQLite and PostgreSQL.
  Stop the previous manager before upgrading and retain a verified backup.
- Existing bot, request, token and preference data is preserved. Keep feedback
  tables and records intact during an application rollback; database downgrades
  and restoration over newer writes require a separate recovery procedure.
- Configure active human Mattermost users as handlers to enable feedback delivery.


### Added

- DM-only feedback tickets with creation, status/history, shared replies, handler queues,
  assignment, resolution, closure and reopening, localized in English and Russian.
- Independent feedback handler configuration through `MM_FEEDBACK_HANDLERS` and Helm
  `mattermost.feedbackHandlers`; empty configuration keeps feedback disabled.
- Transactional feedback event history and notification outbox with retry after restart.

### Upgrade notes

- Startup migration 2 adds `feedback_ticket`, `feedback_event` and `feedback_outbox` on
  SQLite and PostgreSQL. Back up the database before upgrading and retain the new tables.
  Returning to an older application image does not undo the migration; restoring an old
  database backup would discard subsequent feedback. Notification delivery is at least
  once, so a crash after sending can produce a duplicate DM with the same event number.
- Helm chart version advances to 0.3.0 for the optional feedback configuration.

## [0.2.0] - 2026-10-26

### Added

- Explicit SemVer release preparation and CI consistency checks.
- Version reporting through CLI, chat and startup logs.
- Tag pipelines for Python, Helm and Docker Hub container artifacts and GitLab Releases.

## [0.1.0] - 2026-10-26

- Recorded the existing package version as the baseline for versioning.
  This entry does not imply that an earlier tagged release was published.
