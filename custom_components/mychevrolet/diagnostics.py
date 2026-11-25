"""Allowlisted diagnostics without identity or raw GM responses."""

from homeassistant.core import HomeAssistant

from .coordinator import MyChevroletConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: MyChevroletConfigEntry,
) -> dict:
    """Return operational metadata only."""
    return {
        "entry_state": entry.state.value,
        "vehicles": [
            {
                "authentication_required": coordinator.auth_failed,
                "endpoints": dict(coordinator.endpoint_status),
                "diagnostic_samples": len(coordinator.diagnostics),
                "ev_samples": len(coordinator.ev),
                "location_enabled": coordinator.location_enabled,
            }
            for coordinator in getattr(entry, "runtime_data", {}).values()
        ],
    }
