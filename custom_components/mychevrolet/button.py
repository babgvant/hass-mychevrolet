"""Telemetry refresh and opted-in OnStar action buttons."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MyChevroletConfigEntry, MyChevroletDataUpdateCoordinator
from .entity import MyChevroletEntity

COMMANDS = {
    "honk_flash": "alert",
    "remote_start": "start",
    "remote_stop": "cancelStart",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register one refresh button and authorized controls for each vehicle."""
    buttons = []
    for coordinator in entry.runtime_data.values():
        buttons.append(MyChevroletButton(coordinator, "refresh_data"))
        buttons.extend(
            MyChevroletButton(coordinator, key, command)
            for key, command in COMMANDS.items()
            if coordinator.command_available(command)
        )
    async_add_entities(buttons)


class MyChevroletButton(MyChevroletEntity, ButtonEntity):
    """Request a refresh or an allowed action."""

    def __init__(
        self,
        coordinator: MyChevroletDataUpdateCoordinator,
        key: str,
        command: str | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        self.command = command
        if command is None:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Dispatch explicitly to the corresponding coordinator action."""
        if self.command is None:
            await self.coordinator.async_refresh_data()
        else:
            await self.coordinator.async_execute_command(self.command)
