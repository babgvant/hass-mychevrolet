"""EV connection, charging location and discharge flags."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    MyChevroletConfigEntry,
    MyChevroletDataUpdateCoordinator,
    Sample,
)
from .entity import TelemetryEntity
from .sensor import PLUG, select_sample, text

FLAGS = {
    "charge_location_set": "clocSet",
    "at_charge_location": "clocAt",
    "discharge_enabled": "disEnabled",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add binary entities once their backing capabilities are known."""
    for coordinator in entry.runtime_data.values():
        _watch_flags(entry, coordinator, async_add_entities)


def _watch_flags(
    entry: MyChevroletConfigEntry,
    coordinator: MyChevroletDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    added = set()

    @callback
    def add_supported() -> None:
        entities = []
        if "plug_state" not in added and (
            "EV PLUG STATE" in coordinator.onstar.diagnostics_supported
            or select_sample(coordinator, PLUG) is not None
        ):
            added.add("plug_state")
            entities.append(VehicleFlag(coordinator, "plug_state"))
        for key, field in FLAGS.items():
            if key not in added and field in coordinator.ev:
                added.add(key)
                entities.append(VehicleFlag(coordinator, key))
        if entities:
            async_add_entities(entities)

    add_supported()
    entry.async_on_unload(coordinator.async_add_listener(add_supported))


class VehicleFlag(TelemetryEntity, BinarySensorEntity):
    """A reported vehicle setting or connection state."""

    def __init__(self, coordinator: MyChevroletDataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        if key == "plug_state":
            self._attr_device_class = BinarySensorDeviceClass.PLUG

    @property
    def sample(self) -> Sample | None:
        """Use the source backing this flag."""
        if self.key == "plug_state":
            return select_sample(self.coordinator, PLUG)
        return self.coordinator.ev.get(FLAGS[self.key])

    @property
    def is_on(self) -> bool | None:
        """Preserve explicitly reported false and unknown states."""
        if self.sample is None:
            return None
        value = self.sample.value
        if self.key == "plug_state":
            return {"plugged": True, "unplugged": False}.get(text(value))
        return value if isinstance(value, bool) else None
