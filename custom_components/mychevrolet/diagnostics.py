"""Allowlisted diagnostics without identity or raw GM responses."""

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant

from .coordinator import MyChevroletConfigEntry
from .sensor import METRICS

_SAMPLE_FIELDS = {
    field for metric in METRICS for field in (metric.ev, metric.diagnostic) if field
} | {"clocSet", "clocAt", "disEnabled"}


def sample_details(coordinator, samples) -> list[dict]:
    """Expose sample identity and freshness, never its value or vehicle ID."""
    now = datetime.now(UTC)
    return [
        {
            "field": ": ".join(key) if isinstance(key, tuple) else key,
            "sample_at": sample.at.isoformat(),
            "age_seconds": max(0, round((now - sample.at).total_seconds())),
            "timestamp_source": sample.source,
            "fresh": coordinator.fresh(sample),
        }
        for key, sample in sorted(samples.items())
        if key in _SAMPLE_FIELDS
    ]


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
                "freshness_limit_seconds": coordinator.freshness_limit,
                "diagnostic_sample_details": sample_details(
                    coordinator, coordinator.diagnostics
                ),
                "ev_sample_details": sample_details(coordinator, coordinator.ev),
                "location_enabled": coordinator.location_enabled,
            }
            for coordinator in getattr(entry, "runtime_data", {}).values()
        ],
    }
