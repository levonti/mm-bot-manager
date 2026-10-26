# Bot image live acceptance check

Date: 2026-10-26 (Europe/Moscow).
Tested application commit: `06c91a9cbcefed821bfcc587039758f823278f14`.
Result: **PASS**, 24 checks.

## Scope and method

The code from the local checkout was exercised against the configured live
Mattermost server. A dedicated temporary bot was created for this check.
A separate in-memory SQLite registry bound only that bot to the authenticated
human test caller. The running manager was not restarted or upgraded, and its
persistent database was not opened or modified.

The check used the configured human administrator and manager bot credentials
without printing or saving them. TLS verification followed the existing local
configuration, where verification was disabled; certificate validation was not
part of this check.

For each command, the driver posted a real direct message, received its actual
`posted` event through the application's authenticated WebSocket listener, and
passed that event to the current `handle_event` implementation with the isolated
registry and real API clients. The current implementation posted its real
success reply. This verifies the local handlers and live API path; it does not
establish that this application commit is deployed in the running manager.

## Verified behavior

| Operation | Live observations | Result |
| --- | --- | --- |
| `!icon set <test-bot>` | PNG uploaded and attached by the caller; real DM event received; manager account downloaded the attachment; administrative client installed it; success reply posted; downloaded server image matched the test colors | PASS |
| `!icon reset <test-bot>` | Real DM event received; success reply posted; downloaded server image matched `/users/{id}/image/default` pixel for pixel | PASS |
| `!avatar set <test-bot>` | Same upload, event, handler and server image checks through the alias | PASS |
| `!avatar reset <test-bot>` | Same event, handler and default-image checks through the alias | PASS |

The test image was a generated 320 by 180 PNG with two distinct color regions.
The check sampled both regions from the image returned by Mattermost after each
installation, rather than relying solely on an HTTP success status. After each
reset, it compared the returned image dimensions and RGB pixels against the
server-generated default. The final default also matched the initial image.
Bot ID, username, roles and active status remained unchanged throughout all four
image operations.

## Cleanup and limits

- The test bot's image was reset and the bot was disabled; disabled status was
  confirmed by a subsequent API read. The disabled account remains as an audit
  artifact; it was not permanently deleted.
- All eight posts created by the driver (four commands and four handler replies)
  were deleted through the Mattermost API. Attachments remain subject to the
  server's normal retention policy.
- The in-memory registry was closed. Existing bot profiles and the persistent
  manager database were not modified.
- No new access tokens were created. No credentials, server addresses, human
  account identifiers or access-bearing links are included in this report.
- This check covered PNG uploads. JPEG and invalid-input behavior are covered by
  the automated suite, but were not repeated against the live server.
