"""Per-vehicle polling and freshness with account-scoped scheduling."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_CONTROLS,
    CONF_LOCATION,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    UPDATE_TIMEOUT_SECONDS,
)
from .helpers import (
    diagnostic_elements,
    get_location,
    number,
    timestamp,
    vehicle_health_elements,
)
from .onyonstar.client import OnStar, object_at
from .onyonstar.http import AuthError, CloudError

_LOGGER = logging.getLogger(__name__)

type MyChevroletConfigEntry = ConfigEntry[dict[str, MyChevroletDataUpdateCoordinator]]


@dataclass(frozen=True)
class Sample:
    """A value with its last known sample or observation time."""

    value: Any
    at: datetime
    source: str


class MyChevroletDataUpdateCoordinator(DataUpdateCoordinator[dict[str, str]]):
    """Maintain independent freshness and retry state for each endpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        onstar: OnStar,
        config_entry: MyChevroletConfigEntry,
        label: str,
    ) -> None:
        self.scan_minutes = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=self.scan_minutes),
        )
        self.onstar = onstar
        self.vin = onstar.vin
        self.label = label
        self.location_enabled = config_entry.options.get(CONF_LOCATION, False)
        self.diagnostics: dict[tuple[str, str], Sample] = {}
        self.ev: dict[str, Sample] = {}
        self.location: Sample | None = None
        self.last_successful_update: datetime | None = None
        self.auth_failed = False
        self.endpoint_status: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._last_attempt: dict[str, float] = {}
        self._cycle_lock = asyncio.Lock()
        self._vehicle_details_due = 0.0

    @property
    def freshness_limit(self) -> float:
        """Age beyond which a signal is unavailable."""
        return max(3600, self.scan_minutes * 120)

    def fresh(self, sample: Sample | None) -> bool:
        """Check individual sample age rather than the last polling result."""
        return (
            not self.auth_failed
            and sample is not None
            and (datetime.now(UTC) - sample.at).total_seconds() <= self.freshness_limit
        )

    def command_available(self, name: str) -> bool:
        """Check current options and vehicle-specific entitlement."""
        return self.config_entry.options.get(
            CONF_CONTROLS, False
        ) and self.onstar.command_available(name)

    def _record(self, endpoint: str, payload: Any) -> bool:
        observed = datetime.now(UTC)
        if endpoint == "diagnostics":
            body = object_at(payload, "commandResponse", "body")
            at = timestamp(body.get("timestamp"))
            values = {
                key: Sample(
                    value, at or observed, "vehicle" if at else "onstar_response"
                )
                for key, value in diagnostic_elements(payload)
            }
            values.update(
                {
                    key: Sample(value, sample_time, "vehicle")
                    for key, value, sample_time in vehicle_health_elements(payload)
                }
            )
            values = {
                key: sample
                for key, sample in values.items()
                if sample.at <= observed + timedelta(minutes=5)
            }
            if not values:
                return False
            for key, sample in values.items():
                previous = self.diagnostics.get(key)
                if previous is None or (
                    sample.at >= previous.at
                    and (sample.source == "vehicle" or previous.source != "vehicle")
                ):
                    self.diagnostics[key] = sample
        elif endpoint == "ev_metrics":
            at = timestamp(payload.get("utc")) or timestamp(payload.get("ts"))
            if at is None or at > observed + timedelta(minutes=5):
                return False
            values = {}
            for key in ("soc", "tcl", "ravg", "kwh", "odo", "tripcons", "disMinSoc"):
                if (value := number(payload.get(key))) is not None and value >= 0:
                    if key not in {"soc", "tcl", "disMinSoc"} or value <= 100:
                        values[key] = value
            for key in ("cplug", "cstate", "cmode"):
                if isinstance(value := payload.get(key), str) and value.strip():
                    values[key] = value
            for key in ("clocSet", "clocAt", "disEnabled"):
                if isinstance(value := payload.get(key), bool):
                    values[key] = value
            if (estimate := timestamp(payload.get("ceta"))) is not None:
                values["ceta"] = estimate.isoformat()
            if not values:
                return False
            for key, value in values.items():
                previous = self.ev.get(key)
                if previous is None or at >= previous.at:
                    self.ev[key] = Sample(value, at, "vehicle")
        else:
            if not (location := get_location(payload)):
                return False
            self.location = Sample(location, observed, "onstar_response")
            return True
        self.last_successful_update = observed
        return True

    async def _fetch(
        self,
        endpoint: str,
        fetch: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        now = time.monotonic()
        deadlines = self.onstar.account.endpoint_deadlines
        if now < deadlines.get(endpoint, 0):
            self.endpoint_status[endpoint] = "backoff"
            return
        if (
            now
            < self._last_attempt.get(endpoint, float("-inf")) + MIN_SCAN_INTERVAL * 60
        ):
            return
        self._last_attempt[endpoint] = now
        try:
            async with (
                asyncio.timeout(UPDATE_TIMEOUT_SECONDS),
                self.onstar.account.requests,
            ):
                data = await fetch()
            if not self._record(endpoint, data):
                raise CloudError
        except (CloudError, TimeoutError) as error:
            failures = min(7, self._failures.get(endpoint, 0) + 1)
            self._failures[endpoint] = failures
            status = error.status if isinstance(error, CloudError) else 0
            retry_after = error.retry_after if isinstance(error, CloudError) else 0
            pause = min(21600, (1800 if status == 429 else 900) * 2 ** (failures - 1))
            # HTTP 429 applies to all VINs using this endpoint. Transient
            # vehicle errors must leave other VINs free to use it.
            next_due = time.monotonic() + max(
                retry_after, pause + random.uniform(0, 30)
            )
            if status == 429 or retry_after:
                deadlines[endpoint] = max(deadlines.get(endpoint, 0), next_due)
            else:
                self._last_attempt[endpoint] = next_due - MIN_SCAN_INTERVAL * 60
            self.endpoint_status[endpoint] = (
                "rate_limited" if status == 429 else "unreachable"
            )
        else:
            self._failures.pop(endpoint, None)
            self.endpoint_status[endpoint] = "ok"

    async def _async_update_data(self) -> dict[str, str]:
        """Refresh independent sources and retain useful cached samples."""
        async with self._cycle_lock:
            try:
                await self.onstar.account.auth.access_token()
                if (
                    self.onstar.vehicle.get("garage_record")
                    and time.monotonic() >= self._vehicle_details_due
                ):
                    self._vehicle_details_due = time.monotonic() + 21600
                    try:
                        self.onstar.vehicle.update(
                            await self.onstar.account.vehicle_details(self.vin)
                        )
                    except CloudError as error:
                        self._vehicle_details_due = time.monotonic() + max(
                            1800, error.retry_after
                        )
            except AuthError:
                self.auth_failed = True
                raise ConfigEntryAuthFailed("GM sign-in must be renewed") from None
            except CloudError:
                self.endpoint_status["session"] = "unreachable"
                return dict(self.endpoint_status)
            self.endpoint_status.pop("session", None)
            jobs = {"diagnostics": self.onstar.diagnostics}
            if self.onstar.electric or any(
                group.startswith("EV ") for group, _ in self.diagnostics
            ):
                jobs["ev_metrics"] = self.onstar.ev_metrics
            if self.location_enabled and "location" in self.onstar.commands:
                jobs["location"] = self.onstar.location
            results = await asyncio.gather(
                *(self._fetch(name, fetch) for name, fetch in jobs.items()),
                return_exceptions=True,
            )
            if any(isinstance(result, AuthError) for result in results):
                self.auth_failed = True
                raise ConfigEntryAuthFailed("GM sign-in must be renewed")
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            self.auth_failed = False
            return dict(self.endpoint_status)

    async def async_execute_command(self, name: str) -> None:
        """Execute only enabled and entitled non-privileged controls."""
        if not self.command_available(name):
            raise HomeAssistantError("Remote action is disabled or unavailable")
        try:
            async with (
                asyncio.timeout(UPDATE_TIMEOUT_SECONDS),
                self.onstar.account.requests,
            ):
                await self.onstar.execute_command(name)
        except AuthError:
            self.auth_failed = True
            self.async_update_listeners()
            self.config_entry.async_start_reauth_if_available(self.hass)
            raise HomeAssistantError("GM sign-in must be renewed") from None
        except CloudError, TimeoutError:
            raise HomeAssistantError("GM did not complete the remote action") from None

    async def async_refresh_data(self) -> None:
        """Request a refresh subject to the same cooldown as a scheduled poll."""
        await self.async_request_refresh()
