"""JSON transport with fixed origins and sanitized errors."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import aiohttp

GM_HOST = "na-mobile-api.gm.com"
EV_HOST = "eve-vcn.ext.gm.com"


class CloudError(Exception):
    """GM returned no usable response."""

    def __init__(
        self, status: int = 0, retry_after: float = 0, *, invalid_grant: bool = False
    ) -> None:
        super().__init__(f"GM HTTP {status}" if status else "GM response unavailable")
        self.status = status
        self.retry_after = retry_after
        self.invalid_grant = invalid_grant


class AuthError(Exception):
    """GM requires renewed authentication."""


def trusted_url(url: str, host: str) -> bool:
    """Check that credentials can be sent to this exact HTTPS origin."""
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.netloc.lower() == host
            and not parts.fragment
            and not any(ord(char) < 33 or char == "\\" for char in url)
        )
    except ValueError:
        return False


def retry_delay(header: str) -> float:
    """Parse either HTTP Retry-After format."""
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except ValueError, TypeError, OverflowError:
            return 0
    return max(0, seconds) if isfinite(seconds) else 0


async def request_json(
    session: aiohttp.ClientSession, method: str, url: str, host: str, **kwargs: Any
) -> dict[str, Any]:
    """Make one bounded request without following redirects or logging payloads."""
    if not trusted_url(url, host):
        raise CloudError
    try:
        async with session.request(
            method,
            url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=30),
            **kwargs,
        ) as response:
            status = response.status
            if not 200 <= status < 300:
                invalid_grant = False
                if status == 400 and kwargs.get("data"):
                    try:
                        body = await response.json(content_type=None)
                    except ValueError, aiohttp.ClientError:
                        body = None
                    invalid_grant = (
                        isinstance(body, dict) and body.get("error") == "invalid_grant"
                    )
                raise CloudError(
                    status,
                    retry_delay(response.headers.get("Retry-After", "")),
                    invalid_grant=invalid_grant,
                )
            result = await response.json(content_type=None)
    except aiohttp.ClientError, TimeoutError, ValueError:
        raise CloudError from None
    if not isinstance(result, dict):
        raise CloudError
    return result
