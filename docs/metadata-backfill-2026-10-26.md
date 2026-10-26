# Legacy bot metadata backfill, 2026-10-26

Result: **PASS**. Owner-supplied repository and criticality values were applied to
exactly two existing bot records in the production PostgreSQL database.

| Bot | Criticality | Repository |
| --- | ---: | --- |
| `mm-test6-bot` | 2 (important) | Exact owner-supplied value retained in the protected reviewed CSV |
| `mm-test7-bot` | 3 (auxiliary) | Exact owner-supplied value retained in the protected reviewed CSV |

Repository URLs are omitted from this committed evidence. This was an inventory update;
repository contents, reachability and ownership were not fetched or independently verified.

## Procedure and verification

- Checkout before this task: branch `ai/feedback-mvp`, revision
  `618d8372f59881a16dba53effc95220866624d98`. Application source was unchanged.
- Running application image remained `sha256:19bfeb15e1fb9e344cca550766492e131cabf6c5c8d56622cd91b503e095d134`.
- The shipped metadata CLI dry-run accepted both records with zero uncovered records.
- The manager was stopped before apply; the CLI acquired its exclusive database lock.
- A custom-format `pg_dump` was saved before apply and verified with `pg_restore --list`.
- The shipped CLI authenticated the submitting account through Mattermost and applied
  the two records atomically, creating two `metadata_audit` entries with source `migration`.
- Row comparisons verified that only the requested repository/criticality fields and
  the automatic bot revision increments changed. Submitter IDs matched the bot owner.
- All other tables were unchanged at transaction verification, including token records,
  feedback history and approved request history.
- An identical second apply was verified to be a complete no-op: no additional audit
  entries or revision increments.
- The manager restarted successfully, readiness returned 200 and Mattermost WebSocket
  authentication was acknowledged.
- Real production `!info mm-test6-bot`, `!info mm-test7-bot` and `!list` responses displayed
  the exact requested repository values and correct criticality levels.
- Final official metadata `check` returned `{"missing": []}` with exit status 0.
- Final liveness and readiness both returned 200; container restart count was 0.
- Apply completed at `2026-10-26T15:53:31.234268+00:00`; live checks completed at
  `2026-10-26T15:54:35.196187+00:00`. Timestamps are UTC.

## Scope correction

The two historical creation requests are already **approved**. The official backfill
workflow covers bot records (including archived ones) and pending requests only; it
forbids editing approved/denied request history. Accordingly, both approved requests
remain unchanged. The earlier deployment report's count of two requests with NULL
attributes described historical rows, not two outstanding backfill targets.

## Local operator evidence

Protected directory: `/Users/levonti/dev/personal/mm-bot-manager/data/metadata-backfill/20261026T154122Z` (mode 0700).

- Reviewed input: `metadata.reviewed.csv` (mode 0600), SHA-256
  `9eddbb3c94f030715f970539dd06b23e2f493472e81e8e9cc4e2fd7f4cbc4887`.
- Pre-apply backup: `before-backfill.dump` (mode 0600), 33808 bytes,
  SHA-256 `e1db5b13800ec47ab440c95c96ca25119b7a8f96bdb25b0484227ed3b34dd1f3`; archive verification passed.
- Private baseline: `before-private.json`; sanitized execution result: `apply-report.json`.
- The original empty export was preserved. No credentials were added to the CSV or
  this report. Private operator files remain ignored by Git and are local evidence,
  not a remote backup.

No live bot was disabled, no Mattermost token was changed, and no request was approved
or denied by this backfill. The three read-only DM verification commands used the owner's
existing private conversation with the production manager. No outbound repository fetch,
Git push, merge request or remote pipeline was performed.
