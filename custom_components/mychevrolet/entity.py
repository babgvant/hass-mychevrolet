"""Common identity and freshness for myChevrolet entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyChevroletDataUpdateCoordinator, Sample


class MyChevroletEntity(CoordinatorEntity[MyChevroletDataUpdateCoordinator]):
    """Preserve the upstream VIN-based registry identifiers."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyChevroletDataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_{key}"
        self._attr_translation_key = key

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the entity to the selected vehicle."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.vin)},
            name=self.coordinator.label,
            manufacturer="GM",
        )


class TelemetryEntity(MyChevroletEntity):
    """Make an individual stale sample unavailable."""

    @property
    def sample(self) -> Sample | None:
        """Return the entity's current source sample."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """Keep sample availability independent of other endpoints."""
        return self.coordinator.fresh(self.sample)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Report when the particular value was last observed."""
        if (sample := self.sample) is None:
            return {}
        return {"sample_at": sample.at.isoformat(), "timestamp_source": sample.source}
