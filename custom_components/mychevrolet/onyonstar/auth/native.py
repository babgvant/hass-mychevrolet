"""Interactive HTTP login for the observed GM password/authenticator journey.

This is an optional website adapter, not a supported GM authentication API.
Cookies and transaction secrets live only in the config flow. No page scripts
are executed, and unknown journeys require browser sign-in instead.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp

from ..http import AuthError, CloudError, retry_delay, trusted_url
from .constants import AUTH_HOST, POLICY
from .gm_auth import BrowserAttempt, GMAuth


class NativeUnavailable(Exception):
    """GM requires a browser or returned an unsupported login journey."""


def page_object(body: str, name: str) -> dict[str, Any]:
    """Read a JSON page variable without evaluating JavaScript."""
    match = re.search(rf"\bvar\s+{name}\s*=\s*", body)
    if match:
        try:
            value, _ = json.JSONDecoder().raw_decode(body[match.end() :])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise NativeUnavailable


class NativeLogin:
    """One user-driven password/MFA transaction, with no credential retries."""

    def __init__(self, auth: GMAuth) -> None:
        self.auth = auth
        self.attempt = BrowserAttempt()
        self._cookies = aiohttp.CookieJar(quote_cookie=False)
        self._settings: dict[str, Any] = {}
        self._mfa = False

    def clear(self) -> None:
        """Discard the login session after success, failure or cancellation."""
        self._cookies.clear()
        self._settings.clear()
        self.attempt.deadline = 0

    async def _request(
        self, method: str, url: str, *, data: dict[str, str] | None = None
    ) -> tuple[str, str]:
        if time.monotonic() >= self.attempt.deadline:
            raise AuthError
        if not trusted_url(url, AUTH_HOST):
            raise NativeUnavailable
        headers = {"Accept": "text/html, application/json", "Accept-Language": "en-US"}
        if data is not None:
            headers.update(
                {
                    "Origin": f"https://{AUTH_HOST}",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-TOKEN": self._settings["csrf"],
                }
            )
        try:
            # A private jar avoids mixing concurrent accounts or HA's shared
            # cookies. Each request closes its session; the flow owns the jar.
            async with (
                aiohttp.ClientSession(cookie_jar=self._cookies) as session,
                session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                if response.status == 429:
                    raise CloudError(
                        429, retry_delay(response.headers.get("Retry-After", ""))
                    )
                if response.status in (301, 302, 303, 307, 308):
                    return "", response.headers.get("Location", "")
                if response.status in (401, 403):
                    raise NativeUnavailable
                if response.status != 200:
                    raise CloudError(response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise NativeUnavailable
                return body.decode("utf-8"), ""
        except aiohttp.ClientError, TimeoutError, UnicodeError:
            raise CloudError from None

    def _page(self, body: str, api: str) -> None:
        settings = page_object(body, "SETTINGS")
        hosts = settings.get("hosts", {})
        tenant = hosts.get("tenant", "") if isinstance(hosts, dict) else ""
        expected = f"/gmb2cprod.onmicrosoft.com/{POLICY}"
        if (
            settings.get("api") != api
            or not isinstance(tenant, str)
            or tenant.lower() != expected
            or not all(
                isinstance(settings.get(k), str) and settings[k]
                for k in ("csrf", "transId")
            )
        ):
            raise NativeUnavailable
        self._settings = {k: settings[k] for k in ("csrf", "transId")}
        self._settings["tenant"] = tenant

    def _url(self, path: str, *, confirmed: bool = False) -> str:
        params = {"tx": self._settings["transId"], "p": POLICY}
        if confirmed:
            params["csrf_token"] = self._settings["csrf"]
        return (
            f"https://{AUTH_HOST}{self._settings['tenant']}/{path}?{urlencode(params)}"
        )

    async def _submit(self, fields: dict[str, str]) -> None:
        body, redirect = await self._request(
            "POST",
            self._url("SelfAsserted"),
            data={"request_type": "RESPONSE", **fields},
        )
        try:
            result = json.loads(body)
        except ValueError:
            raise NativeUnavailable from None
        if redirect or not isinstance(result, dict):
            raise NativeUnavailable
        if str(result.get("status")) != "200":
            raise AuthError

    async def _confirm(self, api: str) -> tuple[str, bool]:
        body, redirect = await self._request(
            "GET", self._url(f"api/{api}/confirmed", confirmed=True)
        )
        if redirect:
            # Only the registered callback can carry an authorization code.
            # Intermediate HTTPS redirects are unsupported, never followed.
            try:
                scheme = urlsplit(redirect).scheme
            except ValueError:
                raise NativeUnavailable from None
            if scheme != "msauth.com.gm.mychevrolet":
                raise NativeUnavailable
            await self.auth.authorize(self.attempt, redirect)
            self.clear()
            return "", True
        return body, False

    async def start(self, username: str, password: str) -> bool:
        """Submit credentials once; return whether authenticator MFA is needed."""
        metadata = await self.auth.metadata()
        body, redirect = await self._request(
            "GET", self.attempt.url(metadata["authorization_endpoint"])
        )
        if redirect:
            raise NativeUnavailable
        self._page(body, "CombinedSigninAndSignup")
        await self._submit({"logonIdentifier": username, "password": password})
        body, complete = await self._confirm("CombinedSigninAndSignup")
        if complete:
            return False
        self._page(body, "SelfAsserted")
        fields = page_object(body, "SA_FIELDS").get("AttributeFields", [])
        if not isinstance(fields, list) or not any(
            isinstance(field, dict) and field.get("ID") == "otpCode" for field in fields
        ):
            raise NativeUnavailable
        self._mfa = True
        self.attempt.deadline = min(self.attempt.deadline, time.monotonic() + 180)
        return True

    async def verify(self, code: str) -> None:
        """Submit a user-provided authenticator code without retaining it."""
        if not self._mfa or not re.fullmatch(r"[0-9]{6}", code):
            raise AuthError
        await self._submit({"otpCode": code})
        _, complete = await self._confirm("SelfAsserted")
        if not complete:
            raise NativeUnavailable
