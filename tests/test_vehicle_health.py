"""Sanitized GM vehicle-health and garage coverage."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.mychevrolet.helpers import vehicle_health_elements
from custom_components.mychevrolet.onyonstar.client import GARAGE, Account, OnStar
from custom_components.mychevrolet.onyonstar.http import CloudError

VIN = "1GC00000000000001"
TIME = "2026-10-01T12:00:00+00:00"


def test_v3_vehicle_health_uses_per_sample_timestamps_and_units():
    health = {
        "diagnostics": [
            None,
            {
                "diagnosticElements": [
                    {"name": "CHARGE_STATE", "value": "0", "uom": "%", "cts": TIME},
                    {"name": "EV_PLUG_STATE", "value": "Disconnect", "cts": TIME},
                    {
                        "name": "LEFT_FRONT_TIRE_PRESSURE",
                        "value": "270",
                        "uom": "kPa",
                        "cts": TIME,
                    },
                    {
                        "name": "RIGHT_FRONT_TIRE_PRESSURE",
                        "value": "39",
                        "uom": "psi",
                        "cts": TIME,
                    },
                    {"name": "EV_RANGE", "value": "20", "uom": "km", "cts": None},
                ]
            },
        ],
    }
    at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert list(vehicle_health_elements(health)) == [
        (("EV BATTERY LEVEL", "EV BATTERY LEVEL"), 0, at),
        (("EV PLUG STATE", "EV PLUG STATE"), "unplugged", at),
        (("TIRE PRESSURE", "TIRE PRESSURE LF"), 270, at),
    ]


async def test_v3_garage_discovery_coalesced_across_vehicles():
    account = Account(None)
    account.api.api_request = AsyncMock(
        side_effect=[
            CloudError(404),
            {"data": {"vehicles": [{"vin": VIN, "vehicleType": "BEV"}]}},
        ]
    )
    first, second = await asyncio.gather(account.vehicles(), account.vehicles())
    assert first is second
    assert OnStar(account, VIN, first[VIN]).electric
    assert account.api.api_request.await_count == 2
    assert account.api.api_request.await_args.args[:2] == ("POST", GARAGE)


async def test_vehicle_health_never_uses_legacy_fallback_for_rate_limits():
    account = Account(None)
    account.api.api_request = AsyncMock(side_effect=CloudError(429, 1800))
    vehicle = OnStar(
        account,
        VIN,
        {
            "commands": {
                "command": [
                    {
                        "name": "diagnostics",
                        "url": f"/account/vehicles/{VIN}/commands/diagnostics",
                    }
                ]
            }
        },
    )
    with pytest.raises(CloudError) as error:
        await vehicle.diagnostics()
    assert error.value.retry_after == 1800
    assert account.api.api_request.await_count == 1
