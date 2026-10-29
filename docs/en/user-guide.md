# User guide

[English](user-guide.md) · [Русский](../ru/user-guide.md) · [Deutsch](../de/user-guide.md)

Send the manager a DM, then `!language en` and `!help`. Choose `ru`, `de` or `auto`
instead to use another language. Manual choice overrides your Mattermost profile.
Auto uses the profile, then MM_DEFAULT_LANGUAGE; profile changes can take five minutes.
Resetting to auto refreshes the profile immediately. Timezone is configured separately.

## Bots and tokens

Create: `!create example-bot --repo https://example.com/team/project --criticality 2`.
The requester owns the new bot. Creation requires approval when approvers are configured.
Usernames use lowercase ASCII letters, digits, periods, underscores and hyphens;
new names must end with `-bot`. `!list` lists your bots; `!info example-bot` shows details.
Use `!update example-bot --description "Service notifications"` to change a description.
Username, repository and criticality changes require approval in DM. Mixed changes wait
in full. `!delete example-bot` disables the bot. Criticality: 1 critical, 2 important,
3 auxiliary. Display names support 64 Unicode characters, descriptions 1024.

`!import example-bot --repo https://example.com/team/project --criticality 2` always
requires approval in DM. Existing server tokens are unchanged.
`!token create example-bot` issues a token in DM; it remains in message history.
Keep it secret. `!token list example-bot` lists tracked token IDs; revoke with
`!token revoke <token_id>`. Other server tokens may exist.

Attach one PNG/JPEG to `!icon set example-bot` to change its image in DM.
Maximum 5 MiB and 4096×4096, no animation. `!icon reset example-bot` restores the default;
`!avatar` is an alias. Only the owner can change images.

## Roles and feedback

Approvers use `!approve <request_id>` or `!deny <request_id> [reason]` in DM;
self-approval is forbidden. Catalog readers use `!catalog --format cards` or
`!catalog --format table`; filters include --owner, --criticality, --search and --page.
Help exposes privileged commands only to the corresponding role in DM.

When enabled, submit `!feedback new <text>`, list with `!feedback list`, inspect with
`!feedback status FB-1` or `!feedback show FB-1`, reply with `!feedback reply FB-1 <text>`.
Use `!feedback close FB-1 <reason>` or `!feedback reopen FB-1 <reason>` as needed.
Text must contain 1–3000 characters; attachments are unsupported. Never include secrets.
Handlers use `!feedback queue`, `!feedback take FB-1`, then
`!feedback set-status FB-1 waiting <question>` or `resolved <solution>`.
All replies are shared with the author; there are no private handler notes.
