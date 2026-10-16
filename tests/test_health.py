import asyncio
from dataclasses import dataclass

import pytest

from mm_bot_manager.health import start_health_server


@dataclass
class FakeDb:
    healthy: bool = True

    def ping(self) -> bool:
        return self.healthy


async def request(server: asyncio.Server, path: str) -> bytes:
    socket = server.sockets[0]
    host, port = socket.getsockname()[:2]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: health\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_healthy_endpoints_return_200(path: str) -> None:
    server = await start_health_server(FakeDb(), "127.0.0.1", 0)
    async with server:
        response = await request(server, path)
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response.endswith(b"ok\n")


async def test_database_failure_returns_503_without_error_details() -> None:
    server = await start_health_server(FakeDb(healthy=False), "127.0.0.1", 0)
    async with server:
        response = await request(server, "/health/ready")
    assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    assert response.endswith(b"unavailable\n")


async def test_unknown_path_returns_404() -> None:
    server = await start_health_server(FakeDb(), "127.0.0.1", 0)
    async with server:
        response = await request(server, "/metrics")
    assert response.startswith(b"HTTP/1.1 404 Not Found\r\n")
