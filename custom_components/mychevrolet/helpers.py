"""Defensive parsing for GM telemetry payloads."""

from collections.abc import Iterator
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from .onyonstar.client import object_at

HEALTH_FIELDS = {
    "ODO_READ": ("ODOMETER", "ODOMETER", {"km"}),
    "CHARGE_STATE": ("EV BATTERY LEVEL", "EV BATTERY LEVEL", {"%"}),
    "EV_RANGE": ("VEHICLE RANGE", "VEHICLE RANGE", {"km"}),
    "EV_CHARGE_STATE": ("EV CHARGE STATE", "EV CHARGE STATE", set()),
    "EV_PLUG_STATE": ("EV PLUG STATE", "EV PLUG STATE", set()),
    "CHARGE_VOLTAGE": ("EV PLUG VOLTAGE", "EV PLUG VOLTAGE", {"v", "volt", "volts"}),
    "LIFETIME_ENERGY_USED": ("LIFETIME ENERGY USED", "LIFETIME ENERGY USED", {"kwh"}),
    **{
        f"{position}_TIRE_PRESSURE": (
            "TIRE PRESSURE",
            f"TIRE PRESSURE {wheel}",
            {"kpa"},
        )
        for position, wheel in {
            "LEFT_FRONT": "LF",
            "RIGHT_FRONT": "RF",
            "LEFT_REAR": "LR",
            "RIGHT_REAR": "RR",
        }.items()
    },
}


def number(value: Any) -> float | None:
    """Accept finite numeric values, including zero."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if isfinite(result) else None


def timestamp(value: Any) -> datetime | None:
    """Accept a timezone-aware timestamp or a Unix timestamp."""
    try:
        if isinstance(value, str) and not value.isdecimal():
            result = datetime.fromisoformat(value)
            return result.astimezone(UTC) if result.tzinfo is not None else None
        seconds = number(value)
        if seconds is None:
            return None
        return datetime.fromtimestamp(
            seconds / 1000 if seconds >= 1e12 else seconds, UTC
        )
    except ValueError, OverflowError, OSError:
        return None


def diagnostic_elements(payload: Any) -> Iterator[tuple[tuple[str, str], Any]]:
    """Read named scalar fields from a diagnostics response."""
    body = object_at(payload, "commandResponse", "body")
    diagnostics = body.get("diagnosticResponse")
    if not isinstance(diagnostics, list):
        return
    for group in diagnostics:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        elements = group.get("diagnosticElement")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, dict) or not isinstance(
                element.get("name"), str
            ):
                continue
            value = element.get("value")
            if group["name"] in {
                "EV BATTERY LEVEL",
                "VEHICLE RANGE",
                "EV PLUG VOLTAGE",
                "ODOMETER",
                "TIRE PRESSURE",
                "LAST TRIP ELECTRIC ECON",
            }:
                value = number(value)
                if (
                    value is None
                    or value < 0
                    or (group["name"] == "EV BATTERY LEVEL" and value > 100)
                ):
                    continue
            if isinstance(value, (str, int, float, bool)) and value != "":
                yield (group["name"], element["name"]), value


def vehicle_health_elements(
    payload: dict[str, Any],
) -> Iterator[tuple[tuple[str, str], Any, datetime]]:
    """Map timestamped v3 health fields to the existing registry identities."""
    groups = payload.get("diagnostics")
    if not isinstance(groups, list):
        return
    for group in groups:
        elements = object_at(group).get("diagnosticElements")
        if not isinstance(elements, list):
            continue
        for item in elements:
            element = object_at(item)
            name = element.get("name")
            if not isinstance(name, str) or name not in HEALTH_FIELDS:
                continue
            group_name, element_name, units = HEALTH_FIELDS[name]
            value = element.get("value")
            at = timestamp(element.get("cts"))
            if at is None:
                continue
            if units:
                reading = number(value)
                if (
                    str(element.get("uom")).lower() not in units
                    or reading is None
                    or reading < 0
                ):
                    continue
                if name == "CHARGE_STATE" and reading > 100:
                    continue
                value = reading
            elif not isinstance(value, str) or not value.strip():
                continue
            if name == "EV_PLUG_STATE":
                value = {"connect": "plugged", "disconnect": "unplugged"}.get(
                    value.lower(), value
                )
            yield (group_name, element_name), value, at


def get_location(payload: Any) -> dict[str, float] | None:
    """Use only complete and geographically valid coordinates."""
    data = object_at(payload, "commandResponse", "body", "location")
    latitude, longitude = number(data.get("latitude")), number(data.get("longitude"))
    if (
        latitude is None
        or longitude is None
        or abs(latitude) > 90
        or abs(longitude) > 180
    ):
        return None
    return {"latitude": latitude, "longitude": longitude}
