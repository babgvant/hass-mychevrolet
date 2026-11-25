"""Constants for myChevrolet."""

from homeassistant.const import Platform

DOMAIN = "mychevrolet"
CONF_VIN = "vin"
CONF_VEHICLES = "vehicles"
CONF_ACCOUNT = "account"
CONF_DEVICE_ID = "device_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LOCATION = "location"
CONF_CONTROLS = "remote_controls"
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 15
UPDATE_TIMEOUT_SECONDS = 120

PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.LOCK,
    Platform.SENSOR,
)
