from mm_bot_manager.dispatcher import MessageRouter


def test_dm_is_treated_as_command():
    router = MessageRouter(manager_user_id="mgr", manager_username="botmgr")
    text = router.extract_command(
        {"user_id": "u1", "message": "!help"},
        channel_type="D",
    )
    assert text == "!help"


def test_channel_mention_is_stripped():
    router = MessageRouter(manager_user_id="mgr", manager_username="botmgr")
    text = router.extract_command(
        {"user_id": "u1", "message": "@botmgr !list"},
        channel_type="O",
    )
    assert text == "!list"


def test_channel_without_mention_ignored():
    router = MessageRouter(manager_user_id="mgr", manager_username="botmgr")
    text = router.extract_command(
        {"user_id": "u1", "message": "unrelated chatter"},
        channel_type="O",
    )
    assert text is None


def test_self_message_ignored():
    router = MessageRouter(manager_user_id="mgr", manager_username="botmgr")
    text = router.extract_command(
        {"user_id": "mgr", "message": "!help"},
        channel_type="D",
    )
    assert text is None
