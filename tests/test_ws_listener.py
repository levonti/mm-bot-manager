import asyncio
import json
import ssl
from collections import deque

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from mm_bot_manager import ws_listener as ws


class FakeSocket:
    def __init__(self, frames=(), *, error=None):
        self.frames = deque(frames)
        self.sent = []
        self.error = error
        self.closed = False

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if self.frames:
            return self.frames.popleft()
        await asyncio.Event().wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.popleft()


def ack():
    return json.dumps({"seq_reply": 1, "status": "OK"})


def frame():
    return json.dumps({"event": "posted", "data": {"post": "test"}})


@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_plain_websocket_does_not_receive_tls_context(monkeypatch, verify_ssl):
    socket = FakeSocket([ack(), frame()])
    seen = {}

    def connect(url, **kwargs):
        seen.update(kwargs)
        return socket

    monkeypatch.setattr(ws.websockets, "connect", connect)
    events = ws.stream_events(
        "ws://example.invalid/api/v4/websocket", "test", verify_ssl=verify_ssl
    )
    try:
        assert (await anext(events))["event"] == "posted"
        assert seen["ssl"] is None
    finally:
        await events.aclose()
    assert socket.closed


@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_secure_websocket_tls_settings(monkeypatch, verify_ssl):
    socket = FakeSocket([ack(), frame()])
    seen = {}

    def connect(url, **kwargs):
        seen.update(kwargs)
        return socket

    monkeypatch.setattr(ws.websockets, "connect", connect)
    events = ws.stream_events(
        "wss://example.invalid/api/v4/websocket", "test", verify_ssl=verify_ssl
    )
    try:
        await anext(events)
        if verify_ssl:
            assert seen["ssl"] is True
        else:
            assert seen["ssl"].verify_mode == ssl.CERT_NONE
            assert not seen["ssl"].check_hostname
    finally:
        await events.aclose()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_temporary_handshake_error_reconnects_with_delay(monkeypatch, status):
    sockets = deque(
        [
            FakeSocket(error=InvalidStatus(Response(status, "Unavailable", Headers()))),
            FakeSocket([ack(), frame()]),
        ]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: sockets.popleft())
    monkeypatch.setattr(ws.asyncio, "sleep", sleep)
    events = ws.stream_events("wss://example.invalid", "test")
    try:
        assert (await anext(events))["event"] == "posted"
        assert len(delays) == 1
        assert 1 <= delays[0] <= 1.25
    finally:
        await events.aclose()


async def test_fatal_handshake_error_is_not_retried(monkeypatch):
    socket = FakeSocket(error=InvalidStatus(Response(401, "Unauthorized", Headers())))
    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: socket)
    events = ws.stream_events("wss://example.invalid", "test")
    with pytest.raises(InvalidStatus):
        await anext(events)
    assert not socket.sent


async def test_authentication_rejection_is_fatal_and_does_not_echo_payload(monkeypatch):
    socket = FakeSocket(
        [json.dumps({"seq_reply": 1, "status": "FAIL", "error": "secret-value"}), frame()]
    )
    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: socket)
    events = ws.stream_events("wss://example.invalid", "test")
    with pytest.raises(ws.AuthenticationError) as exc:
        await anext(events)
    assert "secret-value" not in str(exc.value)
    assert socket.closed


async def test_events_before_authentication_ack_are_buffered(monkeypatch):
    socket = FakeSocket([frame(), ack()])
    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: socket)
    events = ws.stream_events("wss://example.invalid", "test")
    try:
        assert (await anext(events))["event"] == "posted"
        assert not socket.frames
        assert socket.sent[0]["action"] == "authentication_challenge"
    finally:
        await events.aclose()


async def test_authentication_ack_has_a_timeout():
    with pytest.raises(TimeoutError):
        await ws._authenticate(FakeSocket(), "test", timeout=0.001)


async def test_authentication_buffer_is_bounded():
    socket = FakeSocket([frame()] * 101)
    with pytest.raises(ws.AuthenticationError, match="acknowledgement"):
        await ws._authenticate(socket, "test")


async def test_clean_close_reconnects_with_backoff(monkeypatch):
    sockets = deque([FakeSocket([ack()]), FakeSocket([ack(), frame()])])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: sockets.popleft())
    monkeypatch.setattr(ws.asyncio, "sleep", sleep)
    events = ws.stream_events("wss://example.invalid", "test")
    try:
        assert (await anext(events))["event"] == "posted"
        assert len(delays) == 1
        assert delays[0] >= 1
    finally:
        await events.aclose()


async def test_network_errors_back_off_until_authenticated(monkeypatch):
    sockets = deque(
        [
            FakeSocket(error=OSError("offline")),
            FakeSocket(error=OSError("offline")),
            FakeSocket([ack(), frame()]),
        ]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: sockets.popleft())
    monkeypatch.setattr(ws.asyncio, "sleep", sleep)
    events = ws.stream_events("wss://example.invalid", "test")
    try:
        await anext(events)
        assert 1 <= delays[0] <= 1.25
        assert 2 <= delays[1] <= 2.5
    finally:
        await events.aclose()


async def test_bad_frames_are_skipped_without_logging_payload(monkeypatch, capsys):
    socket = FakeSocket([ack(), "secret-value invalid json", b"\xff", "[]", frame()])
    monkeypatch.setattr(ws.websockets, "connect", lambda *a, **kw: socket)
    events = ws.stream_events("wss://example.invalid", "test")
    try:
        assert (await anext(events))["event"] == "posted"
        assert "secret-value" not in capsys.readouterr().out
    finally:
        await events.aclose()


async def test_actual_loopback_socket_authenticates_and_receives_event(monkeypatch):
    from websockets.asyncio.server import serve

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    received = []

    async def server_handler(socket):
        challenge = json.loads(await socket.recv())
        received.append(challenge)
        await socket.send(ack())
        await socket.send(frame())
        await socket.wait_closed()

    async with serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        events = ws.stream_events(f"ws://127.0.0.1:{port}/api/v4/websocket", "test-token")
        try:
            assert (await asyncio.wait_for(anext(events), 2))["event"] == "posted"
            assert received[0]["data"]["token"] == "test-token"
        finally:
            await events.aclose()
