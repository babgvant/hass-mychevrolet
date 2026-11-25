"""Exercise the OAuth trust boundary without contacting GM."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from custom_components.mychevrolet.onyonstar.api import OnStarAPIClient
from custom_components.mychevrolet.onyonstar.auth import BrowserAttempt, GMAuth, gm_auth
from custom_components.mychevrolet.onyonstar.auth.constants import (
    AUTH_REDIRECT_URI,
    CLIENT_ID,
    DISCOVERY_URL,
    GM_TOKEN_ENDPOINT,
    OIDC_ISSUER,
)
from custom_components.mychevrolet.onyonstar.http import (
    AuthError,
    CloudError,
    retry_delay,
)

AUTHORIZE = "https://custlogin.gm.com/authorize"
TOKEN = "https://custlogin.gm.com/token"
KEYS = "https://custlogin.gm.com/keys"


@pytest.fixture
def key():
    """Generate a disposable signing key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed(key, **claims):
    return jwt.encode(
        {
            "iss": OIDC_ISSUER,
            "aud": CLIENT_ID,
            "exp": int(time.time()) + 3600,
            "sub": "synthetic-subject",
            **claims,
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


@pytest.fixture
def endpoint(monkeypatch, key):
    """Provide signed tokens and record fake GM requests."""
    calls = []
    responses = {}
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    defaults = {
        DISCOVERY_URL: {
            "issuer": OIDC_ISSUER,
            "authorization_endpoint": AUTHORIZE,
            "token_endpoint": TOKEN,
            "jwks_uri": KEYS,
        },
        KEYS: {"keys": [{**public, "kid": "test-key", "use": "sig"}]},
        TOKEN: {"access_token": signed(key), "refresh_token": "rotated"},
        GM_TOKEN_ENDPOINT: {"access_token": "gm-service-bearer", "expires_in": 600},
    }

    async def request(session, method, url, host, **kwargs):
        calls.append((url, kwargs))
        await asyncio.sleep(0)
        response = responses.get(url, defaults[url])
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(gm_auth, "request_json", request)
    return calls, responses


def redirect(attempt, **params):
    return f"{AUTH_REDIRECT_URI}?{urlencode({'code': 'test-code', 'state': attempt.state, **params})}"


def test_pkce_and_redirect_validation():
    attempt = BrowserAttempt()
    query = parse_qs(urlsplit(attempt.url(AUTHORIZE)).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == [attempt.nonce]
    assert query["code_challenge"] != [attempt.verifier]
    for invalid in (
        redirect(attempt, state="other"),
        redirect(attempt) + "&state=duplicate",
        redirect(attempt) + "#fragment",
        redirect(attempt).replace("://auth", "://evil"),
    ):
        with pytest.raises(AuthError):
            attempt.code(invalid)
    assert attempt.code(redirect(attempt)) == "test-code"
    with pytest.raises(AuthError):
        attempt.code(redirect(attempt))
    expired = BrowserAttempt(deadline=-1)
    with pytest.raises(AuthError):
        expired.code(redirect(expired))


async def test_interactive_exchange_binds_nonce_and_redirect(endpoint, key):
    calls, responses = endpoint
    attempt = BrowserAttempt()
    responses[TOKEN] = {
        "access_token": signed(key),
        "id_token": signed(key, nonce=attempt.nonce),
        "refresh_token": "interactive-refresh",
    }
    auth = GMAuth(None, "stable-device")
    await auth.authorize(attempt, redirect(attempt))
    assert auth.subject == "synthetic-subject"
    params = next(data["data"] for url, data in calls if url == TOKEN)
    assert params["redirect_uri"] == AUTH_REDIRECT_URI
    assert params["code_verifier"] == attempt.verifier
    assert await auth.access_token() == "gm-service-bearer"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://unexpected.example"},
        {"aud": "wrong-client"},
        {"exp": 1},
        {"sub": 42},
    ],
)
async def test_rejects_invalid_oidc_claims(endpoint, key, claims):
    _, responses = endpoint
    responses[TOKEN] = {"access_token": signed(key, **claims), "refresh_token": "next"}
    with pytest.raises(AuthError):
        await GMAuth(None, "device").import_tokens({"refresh_token": "initial"})


async def test_rejects_bad_signature_and_nonce(endpoint, key):
    _, responses = endpoint
    auth = GMAuth(None, "device")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses[TOKEN] = {"access_token": signed(other_key), "refresh_token": "next"}
    with pytest.raises(AuthError):
        await auth.import_tokens({"refresh_token": "initial"})
    attempt = BrowserAttempt()
    responses[TOKEN] = {
        "access_token": signed(key),
        "id_token": signed(key, nonce="wrong"),
        "refresh_token": "next",
    }
    with pytest.raises(AuthError):
        await auth.authorize(attempt, redirect(attempt))


async def test_concurrent_refresh_rotation_and_restart(endpoint):
    calls, responses = endpoint
    stored = []

    async def save(value):
        await asyncio.sleep(0)
        stored.append(value)

    auth = GMAuth(
        None, "stable-device", "initial", subject="synthetic-subject", save=save
    )
    assert (
        await asyncio.gather(*(auth.access_token() for _ in range(8)))
        == ["gm-service-bearer"] * 8
    )
    assert stored == ["rotated"]
    assert [url for url, _ in calls].count(GM_TOKEN_ENDPOINT) == 1
    assert [url for url, _ in calls].count(TOKEN) == 1
    responses[TOKEN] = {**responses.get(TOKEN, {}), "access_token": auth._ms_token}
    restart = GMAuth(None, "stable-device", stored[-1], subject=auth.subject, save=save)
    await restart.access_token()
    assert [
        kwargs["data"]["refresh_token"] for url, kwargs in calls if url == TOKEN
    ] == ["initial", "rotated"]


async def test_import_exchanges_refresh_and_disregards_supplied_claims(endpoint):
    auth = GMAuth(None, "stable-device")
    await auth.import_tokens({"refresh_token": "initial", "access_token": "untrusted"})
    assert auth.subject == "synthetic-subject"
    assert auth.refresh_token == "rotated"


async def test_revocation_stops_refresh(endpoint):
    calls, responses = endpoint
    responses[TOKEN] = CloudError(400, invalid_grant=True)
    auth = GMAuth(None, "device", "revoked")
    for _ in range(2):
        with pytest.raises(AuthError):
            await auth.access_token()
    assert [url for url, _ in calls].count(TOKEN) == 1


@pytest.mark.parametrize(
    "address",
    [
        "https://bad.example/api/v1",
        "https://na-mobile-api.gm.com@bad.example/api/v1",
        "//bad.example/api/v1",
        "https://na-mobile-api.gm.com:444/api/v1",
        "http://na-mobile-api.gm.com/api/v1",
    ],
)
def test_api_disallows_foreign_bearer_destinations(address):
    with pytest.raises(CloudError):
        OnStarAPIClient._url(address)


def test_retry_after_parsing():
    assert retry_delay("90") == 90
    assert retry_delay("Wed, 01 Jan 2099 00:00:00 GMT") > 0
    assert retry_delay("NaN") == 0
