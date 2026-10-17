"""Verify the chart's SSL_CERT_FILE contract with real HTTP and WebSocket clients."""

import asyncio
import json
import shutil
import ssl
import subprocess
from contextlib import aclosing

import httpx
import pytest
from websockets.asyncio.server import serve

from mm_bot_manager.mm_client import MattermostClient
from mm_bot_manager.ws_listener import stream_events


async def test_private_ca_bundle_is_used_by_both_mattermost_clients(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is required for the local test CA")
    key, cert = tmp_path / "key.pem", tmp_path / "ca.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    async def http_handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b'{"id":"test-manager"}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def ws_handler(connection):
        challenge = json.loads(await connection.recv())
        assert challenge["action"] == "authentication_challenge"
        await connection.send(json.dumps({"seq_reply": 1, "status": "OK"}))
        await connection.send(json.dumps({"event": "test_tls"}))
        await connection.wait_closed()

    async with await asyncio.start_server(http_handler, "127.0.0.1", 0, ssl=context) as http_server:
        port = http_server.sockets[0].getsockname()[1]
        url = f"https://127.0.0.1:{port}/api/v4"
        async with MattermostClient(url, "test-only-token") as client:
            with pytest.raises(httpx.ConnectError):
                await client.get_me()
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        async with MattermostClient(url, "test-only-token") as client:
            assert (await client.get_me())["id"] == "test-manager"
        async with serve(ws_handler, "127.0.0.1", 0, ssl=context) as ws_server:
            ws_port = ws_server.sockets[0].getsockname()[1]
            async with aclosing(
                stream_events(f"wss://127.0.0.1:{ws_port}", "test-only-token")
            ) as events:
                assert (await asyncio.wait_for(anext(events), timeout=3))["event"] == "test_tls"
