"""Home Assistant config flow, lifecycle, migration and fresh telemetry."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry, entity_registry
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mychevrolet import async_migrate_entry
from custom_components.mychevrolet.account import account_id, accounts
from custom_components.mychevrolet.const import DOMAIN
from custom_components.mychevrolet.diagnostics import async_get_config_entry_diagnostics
from custom_components.mychevrolet.onyonstar.auth import GMAuth
from custom_components.mychevrolet.onyonstar.client import Account, OnStar
from custom_components.mychevrolet.onyonstar.http import AuthError, CloudError

VIN = "1GC00000000000001"
SECOND = "1GC00000000000002"
ICE = "1GC00000000000003"
KEY = account_id("test-subject")


def command(name, **extra):
    return {"name": name, "url": f"/account/vehicles/{VIN}/commands/{name}", **extra}


def vehicle(vin, *, electric=True):
    return {
        "vin": vin,
        "name": "Test EV" if electric else "Test truck",
        "commands": {
            "command": [
                command(
                    "diagnostics",
                    commandData={
                        "supportedDiagnostics": {
                            "supportedDiagnostic": [
                                "EV BATTERY LEVEL",
                                "EV PLUG STATE",
                                "ODOMETER",
                            ]
                            if electric
                            else ["ODOMETER"]
                        }
                    },
                ),
                command("location"),
                command("lockDoor", isPrivSessionRequired=False),
                command("unlockDoor", isPrivSessionRequired="false"),
                command("start", isPrivSessionRequired=True),
            ]
        },
    }


def diag(*groups):
    return {
        "commandResponse": {
            "status": "success",
            "body": {
                "diagnosticResponse": [
                    {
                        "name": name,
                        "diagnosticElement": [{"name": name, "value": value}],
                    }
                    for name, value in groups
                ]
            },
        }
    }


@pytest.fixture
def cloud(monkeypatch):
    """Mock the vehicle and OAuth layers so no GM network is used."""
    available = {vin: vehicle(vin, electric=vin != ICE) for vin in (VIN, SECOND, ICE)}

    async def import_tokens(self, tokens):
        if tokens["refresh_token"] != "initial":
            raise AuthError
        self.subject = "test-subject"
        self.refresh_token = "test-refresh"

    mock = {
        "vehicles": AsyncMock(return_value=available),
        "access_token": AsyncMock(return_value="mock-bearer"),
        "diagnostics": AsyncMock(return_value=diag(("ODOMETER", "1.5"))),
        "ev_metrics": AsyncMock(
            return_value={
                "utc": datetime.now(UTC).timestamp() * 1000,
                "soc": 0,
                "tcl": 80,
                "cplug": "plugged",
                "cstate": "CHARGING",
                "disEnabled": False,
                "extraMetrics": {"cpwrEst": None},
            }
        ),
        "location": AsyncMock(
            return_value={
                "commandResponse": {
                    "body": {"location": {"latitude": 0, "longitude": 0}}
                }
            }
        ),
    }
    monkeypatch.setattr(GMAuth, "import_tokens", import_tokens)
    for cls, methods in (
        (Account, ("vehicles",)),
        (GMAuth, ("access_token",)),
        (OnStar, ("diagnostics", "ev_metrics", "location")),
    ):
        for method in methods:
            monkeypatch.setattr(cls, method, mock[method])
    return mock


async def create_entry(
    hass, vins=None, *, options=None, secret="test-refresh", subject="test-subject"
):
    key = account_id(subject)
    await (
        accounts(hass)
        .store(key)
        .async_save(
            {
                "refresh_token": secret,
                "subject": subject,
                "device_id": "stable-device",
            }
        )
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Test vehicle",
        unique_id=key,
        data={"account": key, "vehicles": vins or [VIN]},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def registered(hass, platform, vin, suffix):
    result = entity_registry.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{vin}_{suffix}"
    )
    return result


async def test_import_setup_multi_vehicle_and_reload(hass, cloud):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert flow["type"] is FlowResultType.MENU
    flow_id = flow["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "token_import"}
    )
    assert result["step_id"] == "token_import"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"tokens": "invalid"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"tokens": "initial"}
    )
    assert result["step_id"] == "vehicles"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"vehicles": [VIN, ICE]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    battery = registered(hass, "sensor", VIN, "battery_level")
    assert battery and float(hass.states.get(battery).state) == 0
    assert registered(hass, "sensor", ICE, "battery_level") is None
    assert registered(hass, "lock", VIN, "door_lock") is None
    assert registered(hass, "device_tracker", VIN, "location") is None
    assert cloud["location"].await_count == 0
    registry = device_registry.async_get(hass)
    assert registry.async_get_device_by_identifier((DOMAIN, VIN), entry.entry_id)
    assert registry.async_get_device_by_identifier((DOMAIN, ICE), entry.entry_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, "sensor", VIN, "battery_level") == battery
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_removes_legacy_secrets_and_keeps_unique_id(hass, cloud):
    legacy = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=VIN,
        data={
            "vin": VIN,
            "username": "secret@example.invalid",
            "password": "old-secret",
            "totp_secret": "old-otp",
            "onstar_pin": "0000",
            "device_id": "old-device",
        },
        options={"cheater_mode": True, "scan_interval": 30},
    )
    legacy.add_to_hass(hass)
    assert await async_migrate_entry(hass, legacy)
    assert legacy.data == {"vehicles": [VIN], "device_id": "old-device"}
    assert legacy.unique_id == VIN
    assert "cheater_mode" not in legacy.options

    legacy.async_start_reauth(hass)
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    flow_id = flow["flow_id"]
    await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "token_import"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"tokens": "initial"}
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert legacy.unique_id == VIN
    assert legacy.data == {"vehicles": [VIN], "account": KEY}
    assert registered(hass, "sensor", VIN, "battery_level")


async def test_read_only_and_entitlements(hass, cloud):
    entry = await create_entry(hass)
    coordinator = entry.runtime_data[VIN]
    assert not coordinator.command_available("lockDoor")
    with pytest.raises(CloudError) as err:
        await coordinator.onstar.execute_command("lockDoor")
    assert err.value.status == 403
    assert not hass.services.has_service(DOMAIN, "execute_command")

    hass.config_entries.async_update_entry(
        entry, options={"remote_controls": True, "location": True}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registered(hass, "lock", VIN, "door_lock")
    assert registered(hass, "button", VIN, "remote_start") is None
    assert cloud["location"].await_count == 1


async def test_partial_stale_and_rate_limited_responses(hass, cloud):
    entry = await create_entry(hass)
    coordinator = entry.runtime_data[VIN]
    old_soc = coordinator.ev["soc"]
    coordinator._last_attempt.clear()
    cloud["diagnostics"].side_effect = CloudError(429, retry_after=7200)
    cloud["ev_metrics"].return_value = {
        "utc": datetime.now(UTC).timestamp() * 1000,
        "soc": None,
        "tcl": 90,
        "cplug": {"malformed": "data"},
    }
    await coordinator.async_refresh()
    assert coordinator.ev["soc"] == old_soc
    assert coordinator.ev["tcl"].value == 90
    assert coordinator.endpoint_status["diagnostics"] == "rate_limited"
    assert coordinator.endpoint_status["ev_metrics"] == "ok"
    await coordinator.async_refresh()
    assert cloud["diagnostics"].await_count == 2
    coordinator.ev["soc"] = type(old_soc)(
        old_soc.value, old_soc.at - timedelta(hours=4), old_soc.source
    )
    assert not coordinator.fresh(coordinator.ev["soc"])
    coordinator.async_update_listeners()
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "battery_level")).state == "0.0"
    )


async def test_authentication_health_and_diagnostics_redaction(hass, cloud):
    entry = await create_entry(hass, options={"location": True})
    cloud["access_token"].side_effect = AuthError
    await entry.runtime_data[VIN].async_refresh()
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "health")).state
        == "reauth_required"
    )
    diagnostic = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for sensitive in ("test-refresh", "mock-bearer", "latitude", "longitude", VIN, KEY):
        assert sensitive not in diagnostic


async def test_stale_ev_data_is_visible_in_health_and_diagnostics(hass, cloud):
    stale_time = datetime.now(UTC) - timedelta(hours=4)
    cloud["ev_metrics"].return_value = {
        "utc": stale_time.timestamp() * 1000,
        "soc": 63,
        "tcl": 80,
        "cplug": "plugged",
        "disEnabled": False,
    }
    entry = await create_entry(hass)
    coordinator = entry.runtime_data[VIN]
    assert coordinator.endpoint_status == {"diagnostics": "ok", "ev_metrics": "stale"}
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "odometer")).state
        != "unavailable"
    )
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "battery_level")).state
        == "63.0"
    )
    assert hass.states.get(registered(hass, "sensor", VIN, "health")).state == "stale"
    battery = hass.states.get(registered(hass, "sensor", VIN, "battery_level"))
    assert battery.attributes["stale"] is True
    assert battery.attributes["sample_age_seconds"] >= 14400
    assert battery.attributes["sample_at"] == stale_time.isoformat()
    assert (
        hass.states.get(registered(hass, "binary_sensor", VIN, "plug_state")).state
        == "on"
    )
    assert (
        hass.states.get(
            registered(hass, "binary_sensor", VIN, "discharge_enabled")
        ).state
        == "off"
    )
    # Unknown field names from upstream diagnostics must not be exported.
    coordinator.diagnostics[("private-email", "private-vin")] = next(
        iter(coordinator.ev.values())
    )
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    vehicle_data = diagnostic["vehicles"][0]
    assert vehicle_data["freshness_limit_seconds"] == 3600
    soc = next(
        item for item in vehicle_data["ev_sample_details"] if item["field"] == "soc"
    )
    assert soc["sample_at"] == stale_time.isoformat()
    assert soc["age_seconds"] >= 14400
    assert soc["fresh"] is False
    assert "value" not in soc
    assert "private-email" not in json.dumps(diagnostic)
    assert "private-vin" not in json.dumps(diagnostic)
    # A later fresh EV response restores availability and health.
    cloud["ev_metrics"].return_value = {
        "utc": datetime.now(UTC).timestamp() * 1000,
        "soc": 63,
        "tcl": 80,
    }
    coordinator._last_attempt.clear()
    await coordinator.async_refresh()
    assert coordinator.endpoint_status["ev_metrics"] == "ok"
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "battery_level")).state
        == "63.0"
    )
    assert hass.states.get(registered(hass, "sensor", VIN, "health")).state == "ok"
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "battery_level")).attributes[
            "stale"
        ]
        is False
    )


async def test_missing_battery_sample_remains_unavailable(hass, cloud):
    cloud["ev_metrics"].return_value = {
        "utc": datetime.now(UTC).timestamp() * 1000,
        "soc": None,
        "tcl": 80,
    }
    await create_entry(hass)
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "battery_level")).state
        == "unavailable"
    )
    assert (
        hass.states.get(registered(hass, "sensor", VIN, "target_charge_level")).state
        == "80.0"
    )


async def test_private_account_storage_and_isolation(hass, cloud):
    first = await create_entry(hass)
    second = await create_entry(
        hass, [SECOND], secret="different-refresh", subject="other-subject"
    )
    assert (
        first.runtime_data[VIN].onstar.account
        is not second.runtime_data[SECOND].onstar.account
    )
    path = Path(accounts(hass).store(KEY).path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "test-refresh" in path.read_text()
    other_path = Path(accounts(hass).store(second.data["account"]).path)
    assert "different-refresh" in other_path.read_text()
    await hass.config_entries.async_remove(first.entry_id)
    assert not path.exists()
    assert other_path.exists()
