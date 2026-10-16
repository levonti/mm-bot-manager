import logging

from mm_bot_manager.logging import configure_logging


def test_debug_does_not_expose_transport_authentication_frames(caplog):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    logging.getLogger("websockets.client").debug(
        '> TEXT {"authentication_challenge": "synthetic-sensitive-value"}'
    )
    logging.getLogger("httpcore").debug("headers: synthetic-sensitive-value")
    assert "synthetic-sensitive-value" not in caplog.text
