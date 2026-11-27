"""Optional vehicle GPS tracker."""

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MyChevroletConfigEntry, Sample
from .entity import TelemetryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Expose GPS only for vehicles opted in to location collection."""
    async_add_entities(
        MyChevroletTracker(coordinator, "location")
        for coordinator in entry.runtime_data.values()
        if coordinator.location_enabled and "location" in coordinator.onstar.commands
    )


class MyChevroletTracker(TelemetryEntity, ScannerEntity):
    """Report a fresh location when available."""

    _attr_source_type = SourceType.GPS

    @property
    def available(self) -> bool:
        """Keep location freshness separate from last-known telemetry display."""
        return self.coordinator.fresh(self.sample)

    @property
    def sample(self) -> Sample | None:
        """Return the complete GPS sample."""
        return self.coordinator.location

    @property
    def latitude(self) -> float | None:
        """Return the vehicle's latitude."""
        return self.sample.value["latitude"] if self.sample else None

    @property
    def longitude(self) -> float | None:
        """Return the vehicle's longitude."""
        return self.sample.value["longitude"] if self.sample else None
