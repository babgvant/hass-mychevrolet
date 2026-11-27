"""Setup diagnostics identify the failed request without account payloads."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant import config_entries

from custom_components.mychevrolet.config_flow import MyChevroletConfigFlow
from custom_components.mychevrolet.const import DOMAIN
from custom_components.mychevrolet.onyonstar.auth import GMAuth, gm_auth
from custom_components.mychevrolet.onyonstar.auth.constants import GM_TOKEN_ENDPOINT
from custom_components.mychevrolet.onyonstar.client import Account
from custom_components.mychevrolet.onyonstar.http import (
    CloudError,
    cloud_error_summary,
    request_json,
)


def account():
    auth = Mock(spec=GMAuth)
    auth.subject = "synthetic-subject"
    auth.import_tokens = AsyncMock()
    result = Account(auth)
    result.api.api_request = AsyncMock()
    return result


@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
async def test_fallback_preserves_both_endpoint_failures(status):
    candidate = account()
    candidate.api.api_request.side_effect = [CloudError(status), CloudError(503, 90)]
    with pytest.raises(CloudError) as caught:
        await candidate.vehicles()
    assert caught.value.stage == "garage_list"
    details = " ".join(candidate.discovery_diagnostics)
    assert "GET /api/v1/account/vehicles" in details and f"HTTP {status}" in details
    assert "POST /mbff/garage/v1" in details and "HTTP 503" in details
    assert "90 seconds" in details
    with pytest.raises(CloudError) as cooldown:
        await candidate.vehicles()
    assert cooldown.value.reason == "cooldown"
    assert cooldown.value.status == 0  # Local cooldown is not a GM HTTP 429.
    assert candidate.api.api_request.await_count == 2
    assert "HTTP 503" in " ".join(candidate.discovery_diagnostics)


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_token_exchange_error_keeps_its_stage_and_skips_fallback(status):
    candidate = account()
    candidate.api.api_request.side_effect = CloudError(
        status, stage="gm_token_exchange"
    )
    with pytest.raises(CloudError) as caught:
        await candidate.vehicles()
    assert caught.value.stage == "gm_token_exchange"
    assert candidate.api.api_request.await_count == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_falls_back_to_garage_and_caches_success(status):
    candidate = account()
    vin = "1GC00000000000001"
    candidate.api.api_request.side_effect = [
        CloudError(status),
        {"data": {"vehicles": [{"vin": vin, "model": "Synthetic EV"}]}},
    ]
    records = await candidate.vehicles()
    assert records[vin]["garage_record"] is True
    calls = candidate.api.api_request.call_args_list
    assert calls[0].args[0] == "GET"
    assert calls[1].args == ("POST", "/mbff/garage/v1")
    assert "getVehiclesMBFF" in calls[1].kwargs["text_body"]
    assert await candidate.vehicles() == records
    assert candidate.api.api_request.await_count == 2
    assert "vehicles" not in candidate.endpoint_deadlines
    assert f"HTTP {status}" in " ".join(candidate.discovery_diagnostics)


@pytest.mark.parametrize(
    ("status", "retry_after"), [(401, 0), (429, 0), (429, 120), (503, 120), (404, 120)]
)
async def test_no_fallback_for_auth_rate_limit_or_explicit_retry_after(
    status, retry_after
):
    candidate = account()
    candidate.api.api_request.side_effect = CloudError(status, retry_after)
    with pytest.raises(CloudError):
        await candidate.vehicles()
    assert candidate.api.api_request.await_count == 1
    with pytest.raises(CloudError) as caught:
        await candidate.vehicles()
    assert caught.value.reason == "cooldown"
    assert candidate.api.api_request.await_count == 1


async def test_gm_exchange_tags_http_and_malformed_token_errors(monkeypatch):
    auth = GMAuth(None, "synthetic-device")
    request = AsyncMock(side_effect=CloudError(400))
    monkeypatch.setattr(gm_auth, "request_json", request)
    with pytest.raises(CloudError) as caught:
        await auth._exchange()
    assert caught.value.stage == "gm_token_exchange"
    assert caught.value.status == 400
    assert request.call_args.args[2] == GM_TOKEN_ENDPOINT
    request.side_effect = None
    request.return_value = {"access_token": "private-token", "expires_in": "bad"}
    with pytest.raises(CloudError) as caught:
        await auth._exchange()
    assert caught.value.reason == "invalid_token"
    assert "private-token" not in cloud_error_summary(caught.value)


async def test_graphql_payload_errors_do_not_leak():
    candidate = account()
    candidate.api.api_request.side_effect = [
        {"vehicles": {}},
        {"errors": [{"message": "private-vin private-email private-token"}]},
    ]
    with pytest.raises(CloudError) as caught:
        await candidate.vehicles()
    assert caught.value.reason == "graphql_errors"
    detail = " ".join(candidate.discovery_diagnostics)
    assert "GraphQL errors" in detail
    assert "private" not in detail


async def test_setup_screen_and_warning_include_sanitized_discovery_failure(
    hass, monkeypatch, caplog
):
    candidate = account()
    candidate.api.api_request.side_effect = [CloudError(502), CloudError(502)]
    monkeypatch.setattr(
        MyChevroletConfigFlow, "_new_candidate", AsyncMock(return_value=candidate)
    )
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "token_import"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"tokens": "private-refresh-token"}
    )
    assert result["step_id"] == "retry_discovery"
    detail = result["description_placeholders"]["detail"]
    assert "HTTP 502" in detail and "Vehicle list" in detail
    assert "60 seconds" in detail
    assert "GM setup discovery:" in caplog.text
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert "HTTP 502" in result["description_placeholders"]["detail"]
    assert (
        "Local vehicle-discovery cooldown"
        in result["description_placeholders"]["detail"]
    )
    assert candidate.api.api_request.await_count == 2
    assert "private-refresh-token" not in detail + caplog.text


@pytest.mark.parametrize(
    "payload", [ValueError("private-response"), ["private-response"]]
)
async def test_transport_keeps_http_200_for_bad_json(payload):
    response = AsyncMock()
    response.status = 200
    if isinstance(payload, Exception):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = Mock()
    session.request.return_value = context
    with pytest.raises(CloudError) as caught:
        await request_json(
            session, "GET", "https://custlogin.gm.com/test", "custlogin.gm.com"
        )
    assert caught.value.status == 200
    assert caught.value.reason in ("invalid_json", "unexpected_payload")
    assert "private-response" not in cloud_error_summary(caught.value)


def test_summary_does_not_render_unknown_strings():
    error = CloudError(503, 15, stage="private-token", reason="private-email")
    assert (
        cloud_error_summary(error)
        == "Vehicle discovery: request failed; HTTP 503; retry in 15 seconds."
    )
