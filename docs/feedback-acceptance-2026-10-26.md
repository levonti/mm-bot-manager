# Feedback MVP: live acceptance, 2026-10-26

Result: **PASS**, 13 live acceptance checks passed.

## Environment and scope

- Server: existing Mattermost **10.11.24**; address and credentials omitted.
- Application source: `70b6edce5e08d3e89c90213795a9109790efd68d`, branch `ai/feedback-mvp`.
- Successful run: `20261026T151207Z-5f8811`, starting at 18:12 MSK (15:12 UTC).
- Isolated manager using the actual `serve` implementation, a separate SQLite database,
  and the existing server's real WebSocket and REST APIs.
- Author: the current user's existing active human account.
- Manager, handler and unauthorized participant: dedicated temporary bot accounts.
  No third-party human recipients were included.
- `MM_FEEDBACK_HANDLERS` contained only the temporary handler. No approver or catalog
  roles were configured for the isolated manager. The author was a Mattermost system
  administrator but did not receive feedback-handler privileges.
- The existing manager container continued running, with its original start time
  `2026-10-21T11:23:37.756858792Z`. Its image, database and configuration were unchanged.
- No inbound HTTP callback integration was configured. The isolated instance used the
  application's existing health listener on an automatically selected local port.

## Observed results

| Check | Result | Evidence |
| --- | --- | --- |
| Isolated identities | PASS | Three dedicated bot accounts and fresh tokens created; actual human author authenticated |
| Live command path | PASS | DM command traversed real WebSocket, dispatcher and REST response |
| Creation and notification | PASS | `FB-1` created; quotes, newline, apostrophe and literal `--` text preserved; handler received DM |
| List, status, history, queue | PASS | Author inspected own ticket; handler inspected queue through DM commands |
| Assignment | PASS | `take` set handler; author received assignment event 2 |
| Waiting and author reply | PASS | Handler question delivered; author's reply delivered; `waiting` returned to `in_progress` |
| Handler reply and resolution | PASS | Reply and solution reached author; replying to resolved ticket was rejected without changing history |
| Close and reopen | PASS | `resolved` → `closed` → `new`; assignment cleared; handler notified |
| Access isolation | PASS | Third account could neither read nor reply; foreign and nonexistent IDs produced identical errors; author/admin could not use queue or take |
| Group DM rejection | PASS | Mentioned command in a group containing only the author and test accounts was rejected; no additional ticket created |
| Language and timezone | PASS | Author switched to RU in isolated preferences; handler remained EN; subsequent author notification was RU; dates used UTC+03:00 |
| Durable retry | PASS | Controlled send failure recorded; manager stopped and database connection reopened; restarted manager delivered the queued DM through the real server |
| Final state | PASS | Test ticket closed, 11 history events, zero pending notifications |

## Restart fault injection

After earlier notifications drained, the test driver injected one `httpx.ConnectError`
for the handler's next notification immediately before its HTTP request. Command replies
and other network requests retained the real Mattermost client implementation.

The author then submitted an additional reply. The outbox persisted a pending record
with one failed attempt. The manager task was cancelled, the database connection closed,
and the same application started again with a fresh connection to the same SQLite file.
The retry subsequently reached the handler over the real Mattermost API. This verifies
recovery across an actual application restart, with a **simulated send failure**; it does
not claim that the Mattermost server itself was stopped or disconnected.

## Cleanup and retained evidence

- The successful run's three tokens were revoked; absence from each account's active
  token list was checked. All three bot accounts were disabled and `delete_at` verified.
- Two earlier driver attempts stopped before completing the suite: the first rejected
  port 0 during settings validation; the second expected the literal `ru` instead of
  the correctly localized `Русский`. These were driver issues, not observed product
  defects. The driver was corrected before the successful complete run.
- Each earlier attempt also had three test tokens revoked and three bot accounts
  disabled, with both conditions verified: nine accounts disabled and nine tokens
  revoked across all attempts.
- Test message history remains on Mattermost, subject to its normal retention policy.
  Accounts were disabled rather than deleted.
- Detailed per-run JSON and isolated databases are in the ignored local directory
  `data/feedback-acceptance/`. They are local evidence, not a remote backup. No tokens
  or passwords were written to those reports or databases.
- This committed report contains no server address, token, credential or access link.

## Limits and next step

This is functional API/WebSocket acceptance, not a visual browser/Desktop/mobile check.
The handler and unauthorized participant were bot identities; their commands used real
credentials, and delivery to their actual DM histories was verified. A separate human
handler's client UI was not exercised.

Live persistence used SQLite. PostgreSQL migrations, rollback and concurrent assignment
were already verified in the preceding automated suite: 844 passed, 1 skipped, total
statement/branch coverage 94.94%. That suite was not rerun for this report-only change.
Duplicate WebSocket delivery, large-message boundaries and Markdown/mention neutralization
remain covered by automated tests; no live duplicate-delivery fault was injected here.

The production manager still runs its earlier version without feedback. Deploy the MVP
and configure the intended human `MM_FEEDBACK_HANDLERS` before enabling it for users.
