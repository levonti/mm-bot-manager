import pytest

from mm_bot_manager.db import connect, init_schema
from mm_bot_manager.repository import BotRepo, TokenRepo


@pytest.fixture()
def repos():
    conn = connect(":memory:")
    init_schema(conn)
    yield BotRepo(conn), TokenRepo(conn)
    conn.close()


def test_insert_and_fetch_bot(repos):
    bots, _ = repos
    b = bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name="Demo",
        description="d",
        owner_user_id="owner1",
        owner_username="alice",
    )
    assert b.bot_user_id == "u1"

    fetched = bots.get_by_owner_and_username("owner1", "demo")
    assert fetched is not None
    assert fetched.username == "demo"


def test_ownership_isolation(repos):
    bots, _ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="alice-id",
        owner_username="alice",
    )
    # another user cannot see it
    assert bots.get_by_owner_and_username("bob-id", "demo") is None
    assert bots.list_by_owner("bob-id") == []
    assert len(bots.list_by_owner("alice-id")) == 1


def test_patch_bot(repos):
    bots, _ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name="old",
        description="old",
        owner_user_id="o",
        owner_username="o",
    )
    bots.patch("u1", display_name="new", description="new-desc")
    fetched = bots.get_by_owner_and_username("o", "demo")
    assert fetched is not None
    assert fetched.display_name == "new"
    assert fetched.description == "new-desc"


def test_soft_delete(repos):
    bots, _ = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="o",
        owner_username="o",
    )
    bots.mark_deleted("u1")
    assert bots.get_by_owner_and_username("o", "demo") is None


def test_token_lifecycle(repos):
    bots, tokens = repos
    bots.insert(
        bot_user_id="u1",
        username="demo",
        display_name=None,
        description=None,
        owner_user_id="o",
        owner_username="o",
    )
    t = tokens.record(token_id="tok1", bot_user_id="u1", description="ci")
    assert t.token_id == "tok1"
    assert len(tokens.list_active_for_bot("u1")) == 1

    tokens.mark_revoked("tok1")
    assert tokens.list_active_for_bot("u1") == []
    rec = tokens.get("tok1")
    assert rec is not None
    assert rec.revoked_at is not None
