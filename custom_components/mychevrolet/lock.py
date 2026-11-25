"""Opt-in Non-privileged GM door controls."""

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MyChevroletConfigEntry
from .entity import MyChevroletEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Expose the lock only when both actions are advertised and permitted."""
    async_add_entities(
        MyChevroletLock(coordinator, "door_lock")
        for coordinator in entry.runtime_data.values()
        if coordinator.command_available("lockDoor")
        and coordinator.command_available("unlockDoor")
    )


class MyChevroletLock(MyChevroletEntity, LockEntity):
    """Issue door commands without assuming a reported lock state."""

    @property
    def is_locked(self) -> None:
        """The API does not provide a confirmed lock state."""
        return None

    async def async_lock(self, **kwargs) -> None:
        """Lock the doors."""
        await self.coordinator.async_execute_command("lockDoor")

    async def async_unlock(self, **kwargs) -> None:
        """Unlock the doors."""
        await self.coordinator.async_execute_command("unlockDoor")
