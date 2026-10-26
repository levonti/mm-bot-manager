from time import monotonic
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

import httpx

from .api_diagnostics import response_fields, route_template
from .logging import get_logger


class MattermostError(RuntimeError):
    def __init__(
        self, status: int, message: str, payload: Any = None, *, diagnostic_id: str | None = None
    ) -> None:
        super().__init__(f"Mattermost API error {status}: {message}")
        self.status = status
        self.payload = payload
        self.diagnostic_id = diagnostic_id


class MattermostClient:
    def __init__(
        self, rest_base: str, token: str, *, timeout: float = 15.0, verify_ssl: bool = True
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=rest_base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify_ssl,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        started = monotonic()
        response = await self._client.request(method, path, json=json, files=files)
        return self._decode_response(response, method, path, started)

    def _decode_response(
        self, response: httpx.Response, method: str, path: str, started: float
    ) -> Any:
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, str):
                message = "Unexpected error response"
            diagnostic_id = uuid4().hex
            log = get_logger(__name__, stderr=True)
            write_log = log.error if response.status_code >= 500 else log.warning
            write_log(
                "mattermost_api_error",
                diagnostic_id=diagnostic_id,
                method=method if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "unknown",
                route=route_template(path),
                status=response.status_code,
                elapsed_ms=round((monotonic() - started) * 1000, 2),
                **response_fields(payload, response.headers.get("X-Request-ID")),
            )
            raise MattermostError(
                response.status_code, message, payload, diagnostic_id=diagnostic_id
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get_file_info(self, file_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/files/{file_id}/info")  # type: ignore[no-any-return]

    async def download_image(self, file_id: str, limit: int) -> bytes:
        from .i18n import UserFacingError

        path = f"/files/{file_id}"
        started = monotonic()
        async with self._client.stream("GET", path) as response:
            if response.status_code >= 400:
                # Error bodies are untrusted too: do not buffer an arbitrary body.
                self._decode_response(
                    httpx.Response(response.status_code, headers=response.headers),
                    "GET",
                    path,
                    started,
                )
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(content) + len(chunk) > limit:
                    raise UserFacingError("images.invalid")
                content.extend(chunk)
            return bytes(content)

    async def set_profile_image(self, bot_id: str, data: bytes) -> None:
        await self._request(
            "POST", f"/users/{bot_id}/image", files={"image": ("avatar.png", data, "image/png")}
        )

    async def reset_profile_image(self, bot_id: str) -> None:
        await self._request("DELETE", f"/users/{bot_id}/image")

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/users/me")  # type: ignore[no-any-return]

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/users/{user_id}")  # type: ignore[no-any-return]

    async def get_bot(self, bot_user_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/bots/{bot_user_id}")  # type: ignore[no-any-return]

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        return await self._request("GET", f"/users/username/{username}")  # type: ignore[no-any-return]

    async def create_direct_channel(self, user_id_a: str, user_id_b: str) -> dict[str, Any]:
        return await self._request(  # type: ignore[no-any-return]
            "POST", "/channels/direct", json=[user_id_a, user_id_b]
        )

    async def create_bot(
        self,
        *,
        username: str,
        display_name: str | None = None,
        description: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"username": username}
        if display_name is not None:
            body["display_name"] = display_name
        if description is not None:
            body["description"] = description
        if owner_id is not None:
            body["owner_id"] = owner_id
        return await self._request("POST", "/bots", json=body)  # type: ignore[no-any-return]

    async def patch_bot(
        self,
        bot_user_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if username is not None:
            body["username"] = username
        if display_name is not None:
            body["display_name"] = display_name
        if description is not None:
            body["description"] = description
        return await self._request("PUT", f"/bots/{bot_user_id}", json=body)  # type: ignore[no-any-return]

    async def patch_user(self, user_id: str, *, username: str) -> dict[str, Any]:
        return await self._request(  # type: ignore[no-any-return]
            "PUT", f"/users/{user_id}/patch", json={"username": username}
        )

    async def disable_bot(self, bot_user_id: str) -> None:
        await self._request("POST", f"/bots/{bot_user_id}/disable")

    async def create_user_access_token(self, user_id: str, description: str) -> dict[str, Any]:
        return await self._request(  # type: ignore[no-any-return]
            "POST", f"/users/{user_id}/tokens", json={"description": description}
        )

    async def revoke_user_access_token(self, token_id: str) -> None:
        try:
            await self._request("POST", "/users/tokens/revoke", json={"token_id": token_id})
        except MattermostError as exc:
            if exc.status != 400:
                raise
            # A repeated revoke can return 400. Verify absence rather than treating
            # every bad request as success or blocking recovery forever.
            try:
                await self._request("GET", f"/users/tokens/{token_id}")
            except MattermostError as lookup_exc:
                if lookup_exc.status == 404:
                    return
                raise
            raise exc

    async def list_user_access_tokens(self, user_id: str) -> list[dict[str, Any]]:
        tokens: list[dict[str, Any]] = []
        page = 0
        while True:
            batch = await self._request("GET", f"/users/{user_id}/tokens?page={page}&per_page=200")
            tokens.extend(batch)
            if len(batch) < 200:
                return tokens
            page += 1

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        return await self._request("POST", "/posts", json=body)  # type: ignore[no-any-return]
