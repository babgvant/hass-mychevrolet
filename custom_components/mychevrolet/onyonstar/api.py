"""Async OnStar transport and bounded status polling."""

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .auth import GMAuth
from .http import GM_HOST, AuthError, CloudError, request_json, trusted_url

API_BASE = f"https://{GM_HOST}/api/v1"


class OnStarAPIClient:
    """Use one account token session for all vehicle requests."""

    def __init__(self, auth: GMAuth) -> None:
        self.auth = auth

    async def api_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        text_body: str | None = None,
        poll: bool = False,
    ) -> dict[str, Any]:
        """Send a request and poll its status URL, if supplied by GM."""
        url = self._url(path)
        token = await self.auth.access_token()
        try:
            result = await self._request(token, method, url, json_body, text_body)
        except CloudError as error:
            if error.status != 401:
                raise
            token = await self.auth.access_token(rejected=token)
            try:
                result = await self._request(token, method, url, json_body, text_body)
            except CloudError as retry:
                if retry.status == 401:
                    raise AuthError from None
                raise

        if not poll:
            return result
        for _ in range(7):
            command = result.get("commandResponse")
            if not isinstance(command, dict):
                return result
            status = command.get("status")
            if status == "success":
                return result
            if status not in (
                "pending",
                "inProgress",
                "in_progress",
                "in progress",
            ) or not isinstance(command.get("url"), str):
                raise CloudError
            status_url = self._url(command["url"])
            await asyncio.sleep(12)
            result = await self._request(token, "GET", status_url)
        raise CloudError

    async def _request(
        self,
        token: str,
        method: str,
        url: str,
        body: Any = None,
        text_body: str | None = None,
    ) -> dict[str, Any]:
        return await request_json(
            self.auth.session,
            method,
            url,
            GM_HOST,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            **({"json": body} if text_body is None else {"data": text_body}),
        )

    @staticmethod
    def _url(path: str) -> str:
        if path.startswith("/") and not path.startswith("//"):
            if path.startswith(("/api/", "/veh/", "/mbff/")):
                return f"https://{GM_HOST}{path}"
            return f"{API_BASE}{path}"
        if trusted_url(path, GM_HOST) and urlsplit(path).path.startswith(
            ("/api/", "/veh/", "/mbff/")
        ):
            return path
        raise CloudError
