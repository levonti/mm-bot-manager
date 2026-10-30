# EN/RU/DE live acceptance — 2026-10-30

Result: **PASS**, 47 live checks, plus 50 automated localization tests.
Application revision: `f40c469b9280aa9f02d028a4b3539cf7b0aabdd7` (merged MR !24).

## Environment and method

An isolated local Mattermost Team Edition 10.11.24 was started with PostgreSQL 15.
The official Mattermost image is pinned by its manifest digest:
`sha256:956002f81635864b873239ee83042a803167f0fb452348fbea72b2142d7c89fc`.
The amd64 image ran with Docker's emulation on this arm64 Mac.

Three real human user accounts had EN, RU and DE server profiles. A dedicated
manager bot used a separate SQLite database and the complete application's `serve`
implementation, including its actual authenticated WebSocket listener, dispatcher,
REST clients and durable feedback delivery loop. Commands were posted by users
through the real API, and responses were read from actual Mattermost DM histories.
No application HTTP client, handler, transport or server failure was mocked.

The working Mattermost server and its configured credentials, deployment and
persistent database were not used. The driver does not read `.env`. Its server
accepts connections only through a randomly assigned loopback port. All identities,
passwords and tokens belonged exclusively to the disposable instance.

## Results

| Behavior | Coverage | Result |
| --- | --- | --- |
| Profile detection | First help resolves each of the EN/RU/DE profiles automatically | PASS |
| Explicit choice | Each of three users selects all three languages; immediate help and persisted preference match (nine combinations) | PASS |
| Auto | Each user returns to their server profile language | PASS |
| Validation and parsing | Unsupported language, unknown command, malformed quoting and invalid bot suffix in all three languages; preference remains unchanged | PASS |
| Concurrent requests | EN/RU/DE help requested concurrently; no language leakage | PASS |
| Real API error | Missing bot import produces actual HTTP 404, localized response and safe diagnostic ID in each language | PASS |
| Fallback and override | French profile falls back to configured EN; explicit DE overrides it; restoring DE and auto refreshes the profile | PASS |
| Approval notifications | Three authors × three recipients; complete notification matches recipient language independently of author | PASS |
| Self-approval and denial | Self-approval rejected; another user denies; requester receives localized denial with original multilingual reason | PASS |
| Feedback delivery | Each language creates a ticket; both other handlers receive localized durable notifications (six directions) | PASS |
| Assignment | Handler assignment notifies each author in their language, includes UTC+03:00 and updates the actual ticket | PASS |
| Restart | Stop manager, close/reopen SQLite, restart complete manager; all three explicit choices and help survive | PASS |

User-authored text `Grüße / Привет / hello`, identifiers and command spelling were
preserved. The per-check sanitized results are in the adjacent JSON report.

## Driver correction and cleanup

The first attempt passed the initial 18 checks, then stopped because the driver
rendered a parameterized expected template without arguments. This was a driver
error, not an application defect. The driver was corrected before the complete
47-check pass. Both attempts removed their containers, attached volumes and Docker
networks. Temporary application databases were discarded. No credentials or private
server addresses are stored in the report or driver.

No application defect was observed, so application behavior was not changed.

## Reproduce

Requires Docker, Python 3.14 and the project's uv virtual environment:

```sh
uv venv .venv
uv sync --frozen --extra postgres
uv run --no-sync python scripts/check_live_languages.py --output /private/tmp/mm-language-report.json
```

The script provisions a fresh server and accounts, prints check names, writes a
sanitized report and removes its own containers/volumes/network in cleanup.
The pinned server image may need downloading; amd64 emulation is needed on arm64.
Failure or incomplete cleanup gives a nonzero exit code. This heavy acceptance
check is opt-in; CI checks its formatting and lint without launching Mattermost.

## Limits

This verifies real REST/WebSocket and message content, including errors and recipient
localization. Browser, desktop and mobile rendering and independent native-speaker
copy review were not exercised. The manager ran from the checked-out source on a
new local server with SQLite, not from a newly published release image. Production
rollout, the existing server, PostgreSQL application persistence and regional profile
codes such as `de-CH` were not part of this live run; regional normalization and
SQLite/PostgreSQL upgrade behavior retain their existing automated coverage.
