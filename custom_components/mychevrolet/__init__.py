"""Native Home Assistant lifecycle for myChevrolet."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .account import accounts
from .config_flow import vehicle_label
from .const import (
    CONF_ACCOUNT,
    CONF_CONTROLS,
    CONF_DEVICE_ID,
    CONF_VEHICLES,
    CONF_VIN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import MyChevroletConfigEntry, MyChevroletDataUpdateCoordinator
from .onyonstar.client import OnStar
from .onyonstar.http import AuthError, CloudError

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register a telemetry-only refresh action."""

    async def refresh(_call: ServiceCall) -> None:
        await asyncio.gather(
            *(
                coordinator.async_refresh_data()
                for entry in hass.config_entries.async_loaded_entries(DOMAIN)
                for coordinator in entry.runtime_data.values()
            )
        )

    hass.services.async_register(DOMAIN, "refresh_data", refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MyChevroletConfigEntry) -> bool:
    """Load the shared account and one coordinator per selected VIN."""
    key = entry.data.get(CONF_ACCOUNT)
    if key is None:
        raise ConfigEntryAuthFailed("Sign in to GM to migrate this entry")
    try:
        account = await accounts(hass).get(key)
        records = await account.vehicles()
    except AuthError:
        raise ConfigEntryAuthFailed("GM sign-in must be renewed") from None
    except CloudError, OSError:
        raise ConfigEntryNotReady("GM vehicle discovery is unavailable") from None

    coordinators = {}
    for vin in entry.data[CONF_VEHICLES]:
        if vin not in records:
            _LOGGER.warning("A configured vehicle is missing from the GM account")
            continue
        record = records[vin]
        label = vehicle_label(record, vin)
        client = OnStar(
            account, vin, record, controls=entry.options.get(CONF_CONTROLS, False)
        )
        coordinators[vin] = MyChevroletDataUpdateCoordinator(hass, client, entry, label)
    if not coordinators:
        raise ConfigEntryNotReady(
            "No configured vehicles are present in the GM account"
        )

    await asyncio.gather(
        *(
            coordinator.async_config_entry_first_refresh()
            for coordinator in coordinators.values()
        )
    )
    entry.runtime_data = coordinators
    for vin, coordinator in coordinators.items():
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, vin)},
            name=coordinator.label,
            manufacturer="GM",
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MyChevroletConfigEntry
) -> bool:
    """Unload entities and let Home Assistant stop their coordinators."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete account secrets when the last entry referencing them is removed."""
    if (key := entry.data.get(CONF_ACCOUNT)) and not any(
        other.entry_id != entry.entry_id and other.data.get(CONF_ACCOUNT) == key
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await accounts(hass).remove(key)


def _remove_legacy_tokens(path: Path) -> None:
    for name in (
        "microsoft_tokens.json",
        "gm_tokens.json",
        "microsoft_tokens.old",
        "gm_tokens.old",
    ):
        (path / name).unlink(missing_ok=True)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Preserve registry identifiers and discard pre-token-only secrets."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        try:
            await hass.async_add_executor_job(
                _remove_legacy_tokens, Path(hass.config.path(".storage", DOMAIN))
            )
        except OSError:
            _LOGGER.warning("Legacy GM token cache could not be removed")
        options = {
            key: value
            for key, value in entry.options.items()
            if key in {"scan_interval", "location"}
        }
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_VEHICLES: [entry.data[CONF_VIN]],
                CONF_DEVICE_ID: entry.data.get(CONF_DEVICE_ID) or str(uuid4()),
            },
            options=options,
            version=2,
            minor_version=1,
        )
    return True
