# Version 0.3.0: working-instance verification, 2026-10-26

Result: **PASS**. The working instance runs the published **0.3.0** release and passed
fresh functional checks through the real Mattermost server.

## Release and deployment identity

- Tag: `v0.3.0`; release revision `01039535c35a47f3b3a9771f8766fc5bdd3e1ba6`.
- GitLab release pipeline 462 succeeded on that exact revision.
- Image: `levonti17/mm-bot-manager@sha256:f7ced962d60eed00814afc571e9368239800df90b58b8667c596eb131d05135a`; architecture `amd64`.
- The working container's OCI version and revision labels, installed package version,
  CLI output and chat `!version` all agree.
- Docker Hub tag `0.3.0` resolves to the running image digest. The same digest is
  persisted in the ignored `.env` through `MM_BOT_MANAGER_IMAGE`.
- Checksums of all 52 installed Python/JSON source files match
  the exact Git release commit, not merely its image labels.
- Existing Compose deployment uses the saved `linux/amd64` platform overlay in
  `data/release-v0.3.0/20261026T170447Z/deployment-20261026T172253Z/compose-platform.yaml`.

The requested version had **already been deployed by the preceding process** when
this task started. Its container start time is `2026-10-26T17:23:08.517928004Z`
(20:23 MSK). This verification did not pull a different image, recreate the application,
or modify runtime configuration. PostgreSQL retains its original start time
`2026-10-21T11:23:37.735204458Z`.

## Fresh production checks

Eight check groups passed:

1. `!version` reports 0.3.0 and the release's full revision.
2. `!help` includes feedback commands.
3. A temporary author creates a labelled test ticket and the real human handler
   receives its notification.
4. The author can list, inspect status and read history; the handler can read the queue.
5. Assignment, waiting for details and replies work, with real DM delivery in both directions.
6. Resolution, closure, reopening, assignment reset and final closure work, and queued
   notifications drain.
7. The existing `!list` command succeeds.
8. `!info` for both legacy bots retains the exact owner-supplied repositories and
   criticality 2/3 respectively.

Test ticket `FB-2` finished **closed**, with 9 history events and zero
pending notifications. The temporary author's token was revoked and verified absent
from its active token list. Its account was disabled and deactivation verified. The
real human `levonti` remains the configured feedback handler.

Control hashes of eight protected tables remained unchanged across this test:
`bot`, `token`, `pending_bot_request`, `metadata_audit`, `bot_import`,
`bot_update_request`, `operation`, `user_preference`. Test changes were confined to
feedback records and normal command-processing records.

## Final state and backup verification

At `2026-10-26T19:36:29.799635+00:00` (22:36 MSK):

- `/health/live` and `/health/ready`: 200.
- Mattermost WebSocket authenticated; observed application error events: 0.
- Container restart count: 0.
- Schema migration versions: `[1, 2]`.
- Missing inventory metadata: 0; pending feedback notifications: 0.

The preceding deployment's protected database backup was independently rechecked:

- Path: `/Users/levonti/dev/personal/mm-bot-manager/data/release-v0.3.0/20261026T170447Z/deployment-20261026T172253Z/final.dump`.
- Size: 34087 bytes.
- SHA-256: `5901db87596b8b79ff28dad24197acffd2916b583c787dbd3d59c8bbfbd3de96`; matches the saved checksum.
- `pg_restore --list` successfully reads the archive. No database restore was performed.

Detailed sanitized checks are in the ignored local directory `/Users/levonti/dev/personal/mm-bot-manager/data/runtime-v0.3.0-verification/20261026T193424Z`.
Database backups and local operator evidence are not remote backups. Tokens, passwords,
private server addresses and user-supplied repository URLs are excluded from this report.
No source code changes, additional release publication or redeployment were required.
