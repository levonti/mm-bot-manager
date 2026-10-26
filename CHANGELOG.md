# Changelog

## [Unreleased]

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
- Tag pipelines for Python, Helm and container artifacts and GitLab Releases.

## [0.1.0] - 2026-10-26

- Recorded the existing package version as the baseline for versioning.
  This entry does not imply that an earlier tagged release was published.
