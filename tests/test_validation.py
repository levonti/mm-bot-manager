"""Bot field rules and side-effect boundaries for commands and saved approvals."""

import json
import shlex

import pytest

from mm_bot_manager.commands import dispatch
from mm_bot_manager.i18n import error_text, validate_catalogs
from mm_bot_manager.operations import create_managed_bot
from mm_bot_manager.updates import recover_updates, snapshot, submit_update
from mm_bot_manager.validation import BotValidationError, validate_bot_fields
from tests.test_commands import ctx as ctx
from tests.test_imports import APPROVER, BOT, approver
from tests.test_updates import env as env
from tests.test_updates import propose

FLAGS = " --repo https://git.example/team/project --criticality 2"
INVALID_FIELDS = [
    ("username", "\u0441heck-bot"),
    ("username", "demo\u200b-bot"),
    ("username", "demo\u00a0-bot"),
    ("username", "demo\n-bot"),
    ("username", "Demo-bot"),
    ("username", "demo😀-bot"),
    ("username", "a" * 61 + "-bot"),
    ("username", ""),
    ("username", "all"),
    ("display_name", "я" * 65),
    ("description", "😀" * 1025),
]


def saved_update(context, changes):
    """Seed a proposal as an older version would, preserving immutable payloads."""
    bot = context.bot_repo.get(BOT)
    old = snapshot(bot)
    req = context.pending_repo.create(
        request_id="old-update",
        username=bot.username,
        display_name=bot.display_name,
        description=bot.description,
        requester_user_id=bot.owner_user_id,
        requester_username=bot.owner_username,
        git_repository=bot.git_repository,
        criticality=bot.criticality,
        kind="update",
        target_bot_user_id=BOT,
    )
    context.bot_repo.conn.execute(
        "INSERT INTO bot_update_request (request_id, bot_user_id, old_values, new_values) "
        "VALUES (?, ?, ?, ?)",
        (req.request_id, BOT, json.dumps(old), json.dumps(old | changes)),
    )
    return req


@pytest.mark.parametrize(
    "field,value", INVALID_FIELDS, ids=[f"case-{i}" for i in range(len(INVALID_FIELDS))]
)
def test_invalid_fields_are_rejected(field, value):
    with pytest.raises(BotValidationError):
        validate_bot_fields({field: value})


@pytest.mark.parametrize("username", ["all", "channel", "matterbot", "system"])
def test_reserved_usernames(username):
    with pytest.raises(BotValidationError, match="reserved"):
        validate_bot_fields({"username": username})


@pytest.mark.parametrize("value", [None, 42, [], True])
@pytest.mark.parametrize("field", ["username", "display_name", "description"])
def test_present_fields_require_text(field, value):
    with pytest.raises(BotValidationError):
        validate_bot_fields({field: value})


@pytest.mark.parametrize("username", ["a", ".bot", "_bot", "-bot", "a.b_c-9", "a" * 64])
def test_mattermost_username_format_is_not_restricted_to_alphanumeric_prefix(username):
    validate_bot_fields({"username": username})


def test_unicode_limits_empty_text_and_suffix_policy():
    validate_bot_fields({"username": "a" * 60 + "-bot"}, require_bot_suffix=True)
    validate_bot_fields({"display_name": "я" * 64, "description": "😀" * 1024})
    validate_bot_fields({"display_name": "", "description": ""})
    validate_bot_fields({})
    validate_bot_fields({"username": "legacy"})
    with pytest.raises(BotValidationError, match="-bot"):
        validate_bot_fields({"username": "legacy"}, require_bot_suffix=True)


def test_errors_are_localized_and_render_invisible_characters_safely():
    validate_catalogs()
    for char, code in [("\u0441", "U+0441"), ("\u200b", "U+200B"), ("\n", "U+000A")]:
        with pytest.raises(BotValidationError) as caught:
            validate_bot_fields({"username": "a" + char + "-bot"})
        english = error_text(caught.value, "en")
        russian = error_text(caught.value, "ru")
        assert code in english and code in russian
        assert "position 2" in english and "позиции 2" in russian
        assert char not in english


@pytest.mark.parametrize(
    "field,value", INVALID_FIELDS, ids=[f"case-{i}" for i in range(len(INVALID_FIELDS))]
)
@pytest.mark.parametrize("approval", [False, True])
async def test_invalid_create_never_persists_or_notifies(ctx, field, value, approval):
    make, mm, bot_repo, _, pending_repo = ctx
    fields = {"username": "demo-bot", field: value}
    command = "!create " + shlex.quote(fields.pop("username"))
    command += "".join(f" --{key.replace('_', '-')} {shlex.quote(v)}" for key, v in fields.items())
    context = make(approver_ids=frozenset({"reviewer"}) if approval else frozenset())
    reply = await dispatch(context, command + FLAGS)
    assert "Username" in reply or "username" in reply or "Field" in reply
    assert not mm.created and not mm.posts
    for table in ("bot", "pending_bot_request", "operation"):
        assert bot_repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert pending_repo.get("unused") is None


@pytest.mark.parametrize(
    "field,value", INVALID_FIELDS, ids=[f"case-{i}" for i in range(len(INVALID_FIELDS))]
)
@pytest.mark.parametrize("mixed", [False, True])
async def test_invalid_update_is_atomic_before_proposal_or_immediate_patch(
    env, field, value, mixed
):
    before = env.bot_repo.get(BOT)
    command = f"!update legacy --{field.replace('_', '-')} {shlex.quote(value)}"
    if mixed:
        command += " --criticality 1"
    reply = await dispatch(env, command)
    assert "Username" in reply or "username" in reply or "Field" in reply
    assert env.bot_repo.get(BOT) == before
    env.mm.assert_no_bot_mutations()
    assert not env.mm.posts
    for table in ("pending_bot_request", "bot_update_request"):
        assert env.bot_repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value", INVALID_FIELDS, ids=[f"case-{i}" for i in range(len(INVALID_FIELDS))]
)
async def test_saved_invalid_creation_stays_pending_without_operation(ctx, field, value):
    make, mm, bot_repo, _, pending_repo = ctx
    fields = {"username": "demo-bot", "display_name": None, "description": None, field: value}
    pending_repo.create(
        request_id="old-create",
        **fields,
        requester_user_id="alice-id",
        requester_username="alice",
        git_repository="https://git.example/team/project",
        criticality=2,
    )
    reply = await dispatch(
        make("reviewer", "reviewer", frozenset({"reviewer"})), "!approve old-create"
    )
    assert "Could not create bot" in reply
    assert pending_repo.get("old-create").status == "pending"
    assert bot_repo.conn.execute("SELECT COUNT(*) FROM operation").fetchone()[0] == 0
    assert not mm.created and not mm.posts


@pytest.mark.parametrize(
    "field,value", INVALID_FIELDS, ids=[f"case-{i}" for i in range(len(INVALID_FIELDS))]
)
async def test_saved_invalid_update_is_rechecked_before_claim_or_any_remote_patch(
    env, field, value
):
    req = saved_update(env, {"username": "renamed", "description": "Valid change", field: value})
    before = env.bot_repo.get(BOT)
    posts = list(env.mm.posts)
    reply = await dispatch(approver(env), f"!approve {req.request_id}")
    assert "Username" in reply or "username" in reply or "Field" in reply
    assert env.pending_repo.get(req.request_id).status == "pending"
    assert env.bot_repo.get(BOT) == before
    assert env.mm.posts == posts
    env.mm.assert_no_bot_mutations()


async def test_service_entry_points_cannot_bypass_validation(env):
    with pytest.raises(BotValidationError):
        await create_managed_bot(
            env,
            username="\u0441-bot",
            display_name=None,
            description=None,
            git_repository="https://git.example/team/project",
            criticality=2,
        )
    with pytest.raises(BotValidationError):
        await submit_update(env, env.bot_repo.get(BOT), {"username": "\u0441-bot"})
    env.mm.assert_no_bot_mutations()
    assert env.bot_repo.conn.execute("SELECT COUNT(*) FROM operation").fetchone()[0] == 0
    assert env.bot_repo.conn.execute("SELECT COUNT(*) FROM pending_bot_request").fetchone()[0] == 0


async def test_unicode_creation_boundaries_survive_approval(ctx):
    make, mm, _, _, pending_repo = ctx
    context = make(approver_ids=frozenset({"reviewer"}))
    username, title, description = "a" * 60 + "-bot", "я" * 64, "😀" * 1024
    await dispatch(
        context, f"!create {username} --display-name {title} --description {description}" + FLAGS
    )
    req_id = pending_repo._conn.execute("SELECT request_id FROM pending_bot_request").fetchone()[0]
    assert "Approved" in await dispatch(
        make("reviewer", "reviewer", frozenset({"reviewer"})), f"!approve {req_id}"
    )
    assert mm.created == [{"username": username, "display_name": title, "description": description}]


async def test_unicode_update_boundaries_and_explicit_clearing(env):
    await dispatch(env, f"!update legacy --display-name {'я' * 64} --description {'😀' * 1024}")
    assert env.bot_repo.get(BOT).display_name == "я" * 64
    assert env.bot_repo.get(BOT).description == "😀" * 1024
    req = await propose(env, '--username renamed --display-name "" --description ""')
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).display_name == ""
    assert env.bot_repo.get(BOT).description == ""
    assert env.bot_repo.get(BOT).username == "renamed"


async def test_legacy_unchanged_fields_do_not_block_partial_updates(env):
    env.bot_repo.patch(BOT, display_name="я" * 65)
    env.mm.bots[BOT]["display_name"] = "я" * 65
    assert "Updated" in await dispatch(env, '!update legacy --description "Changed"')
    req = await propose(env, "--criticality 1")
    assert "Approved" in await dispatch(approver(env), f"!approve {req.request_id}")
    assert env.bot_repo.get(BOT).display_name == "я" * 65


async def test_valid_fields_still_obey_review_message_size_limit(env):
    # JSON-escaped controls can exceed the review limit despite valid field lengths.
    env.bot_repo.patch(BOT, description="\x00" * 1024)
    reply = await dispatch(
        env, '!update legacy --criticality 1 --description "' + "\x01" * 1024 + '"'
    )
    assert "too large to review" in reply
    assert env.bot_repo.get(BOT).criticality == 2
    env.mm.assert_no_bot_mutations()
    assert not env.mm.posts


async def test_recovery_rejects_invalid_saved_update_before_partial_writes(env):
    req = saved_update(env, {"username": "\u0441-bot", "description": "Valid change"})
    env.pending_repo.claim(req.request_id, APPROVER)
    # Simulate a partially applied old operation: recovery must not apply its invalid rename.
    env.mm.bots[BOT]["description"] = "Valid change"
    assert await recover_updates(env) == 1
    assert env.pending_repo.get(req.request_id).status == "needs_review"
    env.mm.assert_no_bot_mutations()
