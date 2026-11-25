"""Synthetic password/MFA journey based on observed GM field names.

No account data, cookies, transaction values, or HAR contents are fixtures.
"""

import json
import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from homeassistant import config_entries

from custom_components.mychevrolet.config_flow import MyChevroletConfigFlow
from custom_components.mychevrolet.const import DOMAIN
from custom_components.mychevrolet.onyonstar.auth import BrowserAttempt, GMAuth
from custom_components.mychevrolet.onyonstar.auth.native import (
    NativeLogin,
    NativeUnavailable,
)
from custom_components.mychevrolet.onyonstar.http import AuthError, CloudError

TENANT = "/gmb2cprod.onmicrosoft.com/B2C_1A_SEAMLESS_MOBILE_SignUpOrSignIn"


def page(api, field="otpCode", tenant=TENANT):
    return (
        "var SETTINGS = "
        + json.dumps(
            {
                "api": api,
                "csrf": "synthetic-csrf",
                "transId": "synthetic-transaction",
                "hosts": {"tenant": tenant},
            }
        )
        + "; var SA_FIELDS = "
        + json.dumps({"AttributeFields": [{"ID": field}]})
        + ";"
    )


def native():
    auth = AsyncMock(spec=GMAuth)
    auth.metadata.return_value = {
        "authorization_endpoint": "https://custlogin.gm.com/authorize"
    }
    return NativeLogin(auth)


async def test_password_mfa_and_pkce_exchange(monkeypatch):
    login = native()
    callback = "msauth.com.gm.mychevrolet://auth/?" + urlencode(
        {"state": login.attempt.state, "code": "synthetic-code"}
    )
    request = AsyncMock(
        side_effect=[
            (page("CombinedSigninAndSignup"), ""),
            ('{"status":"200"}', ""),
            (page("SelfAsserted"), ""),
            ('{"status":"200"}', ""),
            ("", callback),
        ]
    )
    monkeypatch.setattr(login, "_request", request)
    assert await login.start("test@example.invalid", "synthetic-password")
    assert 0 < login.attempt.deadline - time.monotonic() <= 180
    await login.verify("123456")
    login.auth.authorize.assert_awaited_once_with(login.attempt, callback)
    calls = request.call_args_list
    assert calls[1].kwargs["data"] == {
        "request_type": "RESPONSE",
        "logonIdentifier": "test@example.invalid",
        "password": "synthetic-password",
    }
    assert calls[3].kwargs["data"] == {"request_type": "RESPONSE", "otpCode": "123456"}
    assert "api/SelfAsserted/confirmed" in calls[4].args[1]
    assert login._settings == {}
    assert not list(login._cookies)
    assert "synthetic-password" not in repr(vars(login))


@pytest.mark.parametrize("response", ['{"status":"400"}', '{"status":"401"}'])
async def test_rejected_credentials_are_not_retried(monkeypatch, response):
    login = native()
    request = AsyncMock(
        side_effect=[(page("CombinedSigninAndSignup"), ""), (response, "")]
    )
    monkeypatch.setattr(login, "_request", request)
    with pytest.raises(AuthError):
        await login.start("test@example.invalid", "synthetic-password")
    assert request.await_count == 2
    login.auth.authorize.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        "<html>Access Denied</html>",
        page("SelfAsserted", "emailMfa"),
        page("SelfAsserted", tenant="//evil.invalid"),
    ],
)
async def test_unknown_mfa_and_changed_pages_require_browser(monkeypatch, body):
    login = native()
    request = AsyncMock(
        side_effect=[
            (page("CombinedSigninAndSignup"), ""),
            ('{"status":200}', ""),
            (body, ""),
        ]
    )
    monkeypatch.setattr(login, "_request", request)
    with pytest.raises(NativeUnavailable):
        await login.start("test@example.invalid", "synthetic-password")
    login.auth.authorize.assert_not_awaited()


async def test_foreign_redirect_is_never_followed(monkeypatch):
    login = native()
    request = AsyncMock(
        side_effect=[
            (page("CombinedSigninAndSignup"), ""),
            ('{"status":200}', ""),
            ("", "https://evil.invalid/callback?code=secret"),
        ]
    )
    monkeypatch.setattr(login, "_request", request)
    with pytest.raises(NativeUnavailable):
        await login.start("test@example.invalid", "synthetic-password")
    assert request.await_count == 3
    login.auth.authorize.assert_not_awaited()


async def test_expired_session_and_foreign_origin_never_make_request():
    login = native()
    with pytest.raises(NativeUnavailable):
        await login._request(
            "POST", "https://evil.invalid", data={"password": "secret"}
        )
    login.attempt.deadline = 0
    with pytest.raises(AuthError):
        await login._request("GET", "https://custlogin.gm.com/")


@pytest.mark.parametrize("status", [302, 403, 429, 500])
async def test_transport_does_not_follow_redirects_or_retry(monkeypatch, status):
    from unittest.mock import Mock

    login = native()
    response = AsyncMock()
    response.status = status
    response.headers = {"Location": "https://evil.invalid/", "Retry-After": "120"}
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = AsyncMock()
    session.request = Mock(return_value=context)
    factory_context = AsyncMock()
    factory_context.__aenter__.return_value = session
    factory = Mock(return_value=factory_context)
    monkeypatch.setattr(
        "custom_components.mychevrolet.onyonstar.auth.native.aiohttp.ClientSession",
        factory,
    )
    if status == 302:
        assert await login._request("GET", "https://custlogin.gm.com/") == (
            "",
            "https://evil.invalid/",
        )
    else:
        with pytest.raises(NativeUnavailable if status == 403 else CloudError) as err:
            await login._request("GET", "https://custlogin.gm.com/")
        if status == 429:
            assert err.value.retry_after == 120
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False
    assert factory.call_args.kwargs["cookie_jar"] is login._cookies
    factory_context.__aexit__.assert_awaited_once()


def test_mobile_callback_accepts_only_empty_or_root_path():
    for path in ("", "/"):
        attempt = BrowserAttempt()
        redirect = f"msauth.com.gm.mychevrolet://auth{path}?" + urlencode(
            {"state": attempt.state, "code": "test"}
        )
        assert attempt.code(redirect) == "test"
    for path in ("//", "/other", "/%2f", "/../"):
        attempt = BrowserAttempt()
        with pytest.raises(AuthError):
            attempt.code(
                f"msauth.com.gm.mychevrolet://auth{path}?"
                + urlencode({"state": attempt.state, "code": "test"})
            )


async def test_native_ha_flow_mfa_and_discovery(hass, monkeypatch):
    candidate = AsyncMock()
    login = AsyncMock()
    login.attempt = BrowserAttempt()
    login.start.return_value = True
    monkeypatch.setattr(
        MyChevroletConfigFlow, "_new_candidate", AsyncMock(return_value=candidate)
    )
    monkeypatch.setattr(
        "custom_components.mychevrolet.config_flow.NativeLogin", lambda auth: login
    )
    monkeypatch.setattr(
        MyChevroletConfigFlow,
        "_discover",
        AsyncMock(return_value={"type": "abort", "reason": "test-discovery"}),
    )
    # clear is deliberately synchronous, like the real adapter.
    from unittest.mock import Mock

    login.clear = Mock()
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert flow["menu_options"] == ["browser", "native", "token_import"]
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "native"}
    )
    assert result["step_id"] == "native"
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"username": "test@example.invalid", "password": "synthetic-password"},
    )
    assert result["step_id"] == "mfa"
    login.verify.side_effect = AuthError
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"code": "123456"}
    )
    assert result["errors"] == {"base": "invalid_mfa"}
    login.verify.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"code": "234567"}
    )
    assert result["reason"] == "test-discovery"
    login.clear.assert_called_once()


@pytest.mark.parametrize("error", [NativeUnavailable, CloudError])
async def test_native_ha_fallback_clears_transaction(hass, monkeypatch, error):
    from unittest.mock import Mock

    candidate = AsyncMock()
    login = AsyncMock()
    login.clear = Mock()
    login.stage = "credential_submission"
    login.start.side_effect = error
    monkeypatch.setattr(
        MyChevroletConfigFlow, "_new_candidate", AsyncMock(return_value=candidate)
    )
    monkeypatch.setattr(
        "custom_components.mychevrolet.config_flow.NativeLogin", lambda auth: login
    )
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "native"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"username": "test@example.invalid", "password": "synthetic-password"},
    )
    assert result["step_id"] == (
        "native_unavailable" if error is NativeUnavailable else "native"
    )
    login.clear.assert_called_once()
    if error is NativeUnavailable:
        assert "password form" in result["description_placeholders"]["detail"]


async def test_failure_detail_never_uses_server_error_text(hass, caplog):
    from unittest.mock import Mock

    flow = MyChevroletConfigFlow()
    flow.hass = hass
    login = Mock()
    login.stage = "mfa_submission"
    flow._native = login
    flow._native_failure(NativeUnavailable("password=private&code=123456"))
    result = await flow.async_step_native_unavailable()
    detail = result["description_placeholders"]["detail"]
    assert "submitting the authenticator code" in detail
    for private in ("password=private", "123456"):
        assert private not in detail
        assert private not in caplog.text
    login.clear.assert_called_once()
