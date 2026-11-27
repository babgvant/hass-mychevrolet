"""Vehicle telemetry and freshness sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPressure,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    MyChevroletConfigEntry,
    MyChevroletDataUpdateCoordinator,
    Sample,
)
from .entity import MyChevroletEntity, TelemetryEntity
from .helpers import number, timestamp

MEASUREMENT = SensorStateClass.MEASUREMENT


def text(value: Any) -> str | None:
    """Normalize GM's string state while preserving unknown categories."""
    if isinstance(value, str) and value.strip():
        return value.strip().lower().replace(" ", "_")
    return None


def charge_state(value: Any) -> str | None:
    """Normalize documented unplugged and charging values."""
    state = text(value)
    if state in {
        "unconnected",
        "not_charging",
        "charging_complete",
        "fully_charged",
        "charging",
    }:
        return {
            "unconnected": "not_charging",
            "charging_complete": "fully_charged",
        }.get(state, state)
    return state


@dataclass(frozen=True)
class Metric:
    """A sensor backed by a diagnostic element, EV sample, or both."""

    key: str
    diagnostic: tuple[str, str] | None = None
    ev: str | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = MEASUREMENT
    parse: Callable[[Any], Any] = number


def diag(name: str) -> tuple[str, str]:
    """Use the group itself as the common element name."""
    return name, name


METRICS = (
    Metric(
        "odometer",
        diag("ODOMETER"),
        "odo",
        UnitOfLength.KILOMETERS,
        SensorDeviceClass.DISTANCE,
        SensorStateClass.TOTAL_INCREASING,
    ),
    Metric(
        "battery_level",
        diag("EV BATTERY LEVEL"),
        "soc",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
    ),
    Metric(
        "target_charge_level",
        ev="tcl",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
    ),
    Metric(
        "ev_range",
        diag("VEHICLE RANGE"),
        "ravg",
        UnitOfLength.KILOMETERS,
        SensorDeviceClass.DISTANCE,
    ),
    Metric(
        "reported_battery_energy",
        ev="kwh",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
    ),
    Metric(
        "charge_state",
        diag("EV CHARGE STATE"),
        "cstate",
        state_class=None,
        parse=charge_state,
    ),
    Metric("plug_state", diag("EV PLUG STATE"), "cplug", state_class=None, parse=text),
    Metric(
        "plug_voltage",
        diag("EV PLUG VOLTAGE"),
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
    ),
    Metric(
        "charger_power_level", diag("CHARGER POWER LEVEL"), state_class=None, parse=text
    ),
    Metric(
        "charge_complete",
        diag("EV ESTIMATED CHARGE END"),
        "ceta",
        device_class=SensorDeviceClass.TIMESTAMP,
        state_class=None,
        parse=timestamp,
    ),
    Metric("charge_mode", ev="cmode", state_class=None, parse=text),
    Metric("last_trip_efficiency", diag("LAST TRIP ELECTRIC ECON")),
    Metric("trip_consumption", ev="tripcons", unit="kWh/100km"),
    Metric(
        "lifetime_reported_energy",
        diag("LIFETIME ENERGY USED"),
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    Metric(
        "discharge_min_soc",
        ev="disMinSoc",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
    ),
    *(
        Metric(
            f"tire_pressure_{wheel.lower()}",
            ("TIRE PRESSURE", f"TIRE PRESSURE {wheel}"),
            unit=UnitOfPressure.KPA,
            device_class=SensorDeviceClass.PRESSURE,
        )
        for wheel in ("LF", "RF", "LR", "RR")
    ),
)


def select_sample(
    coordinator: MyChevroletDataUpdateCoordinator, metric: Metric
) -> Sample | None:
    """Prefer an attributable vehicle timestamp over a receipt timestamp."""
    diagnostics = (
        coordinator.diagnostics.get(metric.diagnostic) if metric.diagnostic else None
    )
    ev = coordinator.ev.get(metric.ev) if metric.ev else None
    if diagnostics and metric.parse(diagnostics.value) is None:
        diagnostics = None
    if ev and metric.parse(ev.value) is not None:
        if diagnostics and diagnostics.source == "vehicle" and diagnostics.at > ev.at:
            return diagnostics
        return ev
    return diagnostics


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register supported sensors, including capabilities learned after setup."""
    for coordinator in entry.runtime_data.values():
        async_add_entities(
            [
                HealthSensor(coordinator, "health"),
                FreshnessSensor(coordinator, "last_successful_update"),
                FreshnessSensor(coordinator, "telemetry_age"),
            ]
        )
        _watch_metrics(entry, coordinator, async_add_entities)


def _watch_metrics(
    entry: MyChevroletConfigEntry,
    coordinator: MyChevroletDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    added: set[str] = set()

    @callback
    def add_supported() -> None:
        entities = []
        for metric in METRICS:
            if metric.key in added:
                continue
            supported = (
                metric.diagnostic
                and metric.diagnostic[0] in coordinator.onstar.diagnostics_supported
            )
            if supported or select_sample(coordinator, metric) is not None:
                added.add(metric.key)
                entities.append(MetricSensor(coordinator, metric))
        if "time_to_complete" not in added and select_sample(
            coordinator, CHARGE_COMPLETE
        ):
            added.add("time_to_complete")
            entities.append(ChargeTimeSensor(coordinator, "time_to_complete"))
        if entities:
            async_add_entities(entities)

    add_supported()
    entry.async_on_unload(coordinator.async_add_listener(add_supported))


class MetricSensor(TelemetryEntity, SensorEntity):
    """A measurement with individually tracked freshness."""

    def __init__(
        self, coordinator: MyChevroletDataUpdateCoordinator, metric: Metric
    ) -> None:
        super().__init__(coordinator, metric.key)
        self.metric = metric
        self._attr_device_class = metric.device_class
        self._attr_state_class = metric.state_class
        self._attr_native_unit_of_measurement = metric.unit

    @property
    def sample(self) -> Sample | None:
        """Select the best available data source."""
        return select_sample(self.coordinator, self.metric)

    @property
    def native_value(self) -> Any:
        """Return the parsed measurement."""
        return self.metric.parse(self.sample.value) if self.sample else None


CHARGE_COMPLETE = next(metric for metric in METRICS if metric.key == "charge_complete")
PLUG = next(metric for metric in METRICS if metric.key == "plug_state")


class ChargeTimeSensor(TelemetryEntity, SensorEntity):
    """Derive time remaining from a fresh GM completion estimate."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = MEASUREMENT

    @property
    def sample(self) -> Sample | None:
        """Use the timestamp estimate rather than a nominal charge rate."""
        return select_sample(self.coordinator, CHARGE_COMPLETE)

    @property
    def native_value(self) -> int | None:
        """Return minutes remaining where the estimate is still applicable."""
        estimate = timestamp(self.sample.value) if self.sample else None
        plug = select_sample(self.coordinator, PLUG)
        if (
            estimate is None
            or not self.coordinator.fresh(plug)
            or text(plug.value) != "plugged"
        ):
            return None
        remaining = (estimate - datetime.now(UTC)).total_seconds()
        return round(remaining / 60) if remaining >= 0 else None


class FreshnessSensor(MyChevroletEntity, SensorEntity):
    """Report the last successful API read or the age of the newest sample."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MyChevroletDataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        if key == "last_successful_update":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        else:
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS

    @property
    def available(self) -> bool:
        """Keep freshness accessible after a GM failure."""
        return True

    @property
    def native_value(self) -> datetime | int | None:
        """Use vehicle time where supplied and response time otherwise."""
        if self.key == "last_successful_update":
            return self.coordinator.last_successful_update
        sample_times = [sample.at for sample in self.coordinator.diagnostics.values()]
        sample_times.extend(sample.at for sample in self.coordinator.ev.values())
        if not sample_times:
            return None
        return max(0, round((datetime.now(UTC) - max(sample_times)).total_seconds()))


class HealthSensor(MyChevroletEntity, SensorEntity):
    """Surface auth, endpoint failures and stale input explicitly."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["reauth_required", "rate_limited", "degraded", "stale", "ok"]

    @property
    def available(self) -> bool:
        """Keep the health signal visible after auth failure."""
        return True

    @property
    def native_value(self) -> str:
        """Return the most actionable current condition."""
        coordinator = self.coordinator
        if coordinator.auth_failed:
            return "reauth_required"
        statuses = set(coordinator.endpoint_status.values())
        if statuses.intersection({"rate_limited", "backoff"}):
            return "rate_limited"
        if (
            statuses.intersection({"unreachable"})
            or not coordinator.last_update_success
        ):
            return "degraded"
        if "stale" in statuses:
            return "stale"
        if any(
            coordinator.fresh(sample)
            for samples in (coordinator.ev, coordinator.diagnostics)
            for sample in samples.values()
        ):
            return "ok"
        return "stale"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Provide independent endpoint status for automations."""
        return {"endpoints": dict(self.coordinator.endpoint_status)}
