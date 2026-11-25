"""Browser PKCE, token import, and serialized GM session refresh."""

from __future__ import annotations

import asyncio
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import jwt

from ..http import GM_HOST, AuthError, CloudError, request_json, trusted_url
from .constants import (
    AUTH_HOST,
    AUTH_REDIRECT_URI,
    CLIENT_ID,
    DISCOVERY_URL,
    GM_TOKEN_ENDPOINT,
    GM_TOKEN_SCOPE,
    OIDC_ISSUER,
    SCOPE_STRING,
)

SaveToken = Callable[[str], Awaitable[None]]


@dataclass
class BrowserAttempt:
    """In-memory PKCE challenge bound to one Home Assistant flow."""

    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    deadline: float = field(default_factory=lambda: time.monotonic() + 600)
    consumed: bool = False

    def url(self, endpoint: str) -> str:
        """Build the GM sign-in URL."""
        challenge = urlsafe_b64encode(sha256(self.verifier.encode()).digest())
        return f"{endpoint}?{
            urlencode(
                {
                    'client_id': CLIENT_ID,
                    'redirect_uri': AUTH_REDIRECT_URI,
                    'response_type': 'code',
                    'response_mode': 'query',
                    'scope': SCOPE_STRING,
                    'code_challenge': challenge.rstrip(b'=').decode(),
                    'code_challenge_method': 'S256',
                    'state': self.state,
                    'nonce': self.nonce,
                    'bundleID': 'com.gm.myChevrolet',
                    'channel': 'lightreg',
                    'ui_locales': 'en-US',
                    'brand': 'chevrolet',
                }
            )
        }"

    def code(self, redirect: str) -> str:
        """Validate redirect, CSRF token and single-use authorization code."""
        if self.consumed or time.monotonic() >= self.deadline or len(redirect) > 32768:
            raise AuthError
        try:
            target = urlsplit(redirect.strip())
            params = parse_qs(target.query, keep_blank_values=True, max_num_fields=16)
        except ValueError:
            raise AuthError from None
        expected = urlsplit(AUTH_REDIRECT_URI)
        states = params.get("state", [])
        codes = params.get("code", [])
        if (
            (target.scheme, target.netloc) != (expected.scheme, expected.netloc)
            or target.path not in (expected.path, "/")
            or target.fragment
            or "error" in params
            or len(states) != 1
            or not secrets.compare_digest(states[0].encode(), self.state.encode())
            or len(codes) != 1
            or not codes[0]
        ):
            raise AuthError
        self.consumed = True
        return codes[0]


class GMAuth:
    """One serialized token session per GM account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        device_id: str,
        refresh_token: str = "",
        *,
        subject: str | None = None,
        save: SaveToken | None = None,
    ) -> None:
        self.session = session
        self.device_id = device_id
        self.refresh_token = refresh_token
        self.subject = subject
        self.save = save
        self._lock = asyncio.Lock()
        self._gm_token = ""
        self._gm_expires = 0.0
        self._ms_token = ""
        self._ms_expires = 0.0
        self._metadata: dict[str, Any] = {}
        self._jwks: dict[str, Any] = {}
        self._persist_pending = False
        self._blocked_until = 0.0
        self._revoked = False

    async def metadata(self) -> dict[str, Any]:
        """Use the official policy's pinned issuer and same-origin endpoints."""
        if not self._metadata:
            data = await request_json(self.session, "GET", DISCOVERY_URL, AUTH_HOST)
            if data.get("issuer") != OIDC_ISSUER or any(
                not isinstance(data.get(key), str)
                or not trusted_url(data[key], AUTH_HOST)
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")
            ):
                raise CloudError
            self._metadata = data
        return self._metadata

    async def _verify(self, token: Any, *, nonce: str | None = None) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise AuthError
        metadata = await self.metadata()
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._jwks.get(kid)
            if key is None:
                jwks = await request_json(
                    self.session, "GET", metadata["jwks_uri"], AUTH_HOST
                )
                self._jwks = {
                    item["kid"]: jwt.PyJWK.from_dict(item, algorithm="RS256").key
                    for item in jwks["keys"]
                    if isinstance(item, dict)
                    and item.get("kty") == "RSA"
                    and item.get("use", "sig") == "sig"
                }
                key = self._jwks[kid]
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=OIDC_ISSUER,
                audience=CLIENT_ID,
                options={"require": ["iss", "aud", "sub", "exp"]},
            )
            if nonce is not None and claims.get("nonce") != nonce:
                raise AuthError
            audience = claims["aud"]
            if (
                isinstance(audience, list)
                and len(audience) > 1
                and claims.get("azp") != CLIENT_ID
            ):
                raise AuthError
            if not claims["sub"] or not isfinite(claims["exp"]):
                raise AuthError
            return claims
        except jwt.PyJWTError, KeyError, TypeError, ValueError:
            raise AuthError from None

    async def _post_ms(self, **form: str) -> dict[str, Any]:
        metadata = await self.metadata()
        try:
            return await request_json(
                self.session,
                "POST",
                metadata["token_endpoint"],
                AUTH_HOST,
                data={"client_id": CLIENT_ID, **form},
            )
        except CloudError as error:
            if error.invalid_grant or error.status in (401, 403):
                raise AuthError from None
            raise

    async def _accept(
        self, tokens: dict[str, Any], *, nonce: str | None = None
    ) -> None:
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token", self.refresh_token)
        if not isinstance(refresh, str) or not refresh:
            raise AuthError
        claims = await self._verify(access)
        if nonce is not None:
            identity = await self._verify(tokens.get("id_token"), nonce=nonce)
            if claims["sub"] != identity["sub"]:
                raise AuthError
            if "at_hash" in identity:
                digest = sha256(access.encode("ascii")).digest()
                expected = (
                    urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()
                )
                if identity["at_hash"] != expected:
                    raise AuthError
        if self.subject is not None and claims["sub"] != self.subject:
            raise AuthError
        self.subject = claims["sub"]
        self._ms_token = access
        self._ms_expires = claims["exp"]
        self._persist_pending |= self.refresh_token != refresh
        self.refresh_token = refresh
        await self._persist()

    async def _persist(self) -> None:
        if self._persist_pending and self.save:
            await self.save(self.refresh_token)
            self._persist_pending = False

    async def authorize(self, attempt: BrowserAttempt, redirect: str) -> None:
        """Exchange a validated callback for an account session."""
        code = attempt.code(redirect)
        tokens = await self._post_ms(
            grant_type="authorization_code",
            code=code,
            redirect_uri=AUTH_REDIRECT_URI,
            code_verifier=attempt.verifier,
            scope=SCOPE_STRING,
        )
        await self._accept(tokens, nonce=attempt.nonce)

    async def import_tokens(self, tokens: dict[str, Any]) -> None:
        """Confirm a locally obtained refresh token with GM before accepting it."""
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh or len(refresh) > 16384:
            raise AuthError
        # A supplied access or ID token has no locally bound nonce. Use a fresh
        # Microsoft response and never trust identity claims from the import.
        returned = await self._post_ms(
            grant_type="refresh_token", refresh_token=refresh
        )
        await self._accept({"refresh_token": refresh, **returned})

    async def access_token(self, *, rejected: str | None = None) -> str:
        """Coalesce concurrent renewals and persist rotations before reuse."""
        async with self._lock:
            if self._revoked:
                raise AuthError
            now = time.monotonic()
            if now < self._blocked_until:
                raise CloudError(429, self._blocked_until - now)
            try:
                await self._persist()
                if self._gm_token != rejected and now + 60 < self._gm_expires:
                    return self._gm_token
                if time.time() + 60 >= self._ms_expires:
                    if not self.refresh_token:
                        raise AuthError
                    result = await self._post_ms(
                        grant_type="refresh_token",
                        refresh_token=self.refresh_token,
                    )
                    await self._accept(result)
                return await self._exchange()
            except AuthError:
                self._revoked = True
                raise
            except CloudError as error:
                self._blocked_until = now + max(30, error.retry_after)
                raise
            except OSError:
                self._blocked_until = now + 30
                raise CloudError from None

    async def _exchange(self) -> str:
        try:
            result = await request_json(
                self.session,
                "POST",
                GM_TOKEN_ENDPOINT,
                GM_HOST,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": self._ms_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "scope": GM_TOKEN_SCOPE,
                    "device_id": self.device_id,
                },
            )
        except CloudError as error:
            if error.invalid_grant or error.status in (401, 403):
                raise AuthError from None
            raise
        token = result.get("access_token")
        try:
            lifetime = float(result["expires_in"])
        except KeyError, ValueError, TypeError, OverflowError:
            raise CloudError from None
        if (
            not isinstance(token, str)
            or not token
            or not isfinite(lifetime)
            or lifetime <= 0
        ):
            raise CloudError
        # This is a service bearer from GM over TLS. Identity and entitlement
        # decisions use the verified upstream subject and authenticated API data.
        self._gm_token = token
        self._gm_expires = time.monotonic() + lifetime
        return token

    async def replace(self, other: GMAuth) -> None:
        """Replace a linked account session without racing an in-flight rotation."""
        async with self._lock:
            if self.subject != other.subject:
                raise AuthError
            if self.save:
                await self.save(other.refresh_token)
            self.refresh_token = other.refresh_token
            self._ms_token = other._ms_token
            self._ms_expires = other._ms_expires
            self._gm_token = ""
            self._gm_expires = self._blocked_until = 0
            self._revoked = self._persist_pending = False

    async def remove(self, delete: Callable[[], Awaitable[None]]) -> None:
        """Prevent an in-flight rotation from recreating an unlinked token file."""
        async with self._lock:
            self._revoked = True
            self.refresh_token = self._ms_token = self._gm_token = ""
            self.save = None
            await delete()
