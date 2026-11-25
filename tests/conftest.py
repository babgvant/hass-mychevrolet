"""Use isolated Home Assistant configuration directories for integration tests."""

import pytest


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    """Isolate config entries, registries and storage for every test."""
    return hass_tmp_config_dir
