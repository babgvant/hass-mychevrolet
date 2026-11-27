"""GM account discovery and vehicle telemetry client."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .api import OnStarAPIClient
from .auth import GMAuth
from .commands import CommandFactory
from .http import EV_HOST, AuthError, CloudError, cloud_error_summary, request_json

VEHICLES = "/account/vehicles?includeCommands=true&includeEntitlements=true&includeModules=true"
GARAGE = "/mbff/garage/v1"
EV_BASE = f"https://{EV_HOST}/api/gmone/v1"
VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def object_at(data: Any, *path: str) -> dict[str, Any]:
    """Read a nested object defensively."""
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


class Account:
    """Share discovery, scheduling and authentication across the account."""

    def __init__(self, auth: GMAuth) -> None:
        self.auth = auth
        self.api = OnStarAPIClient(auth)
        self.requests = asyncio.Semaphore(2)
        self._vehicles: dict[str, dict[str, Any]] = {}
        self._discovery_expires = 0.0
        self._discovery_lock = asyncio.Lock()
        self.endpoint_deadlines: dict[str, float] = {}
        self.discovery_diagnostics: list[str] = []

    async def _discovery_request(
        self, stage: str, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            return await self.api.api_request(method, path, **kwargs)
        except CloudError as error:
            if not error.stage:
                error.stage = stage
            self.discovery_diagnostics.append(cloud_error_summary(error))
            raise

    async def _discover(self) -> list:
        self.discovery_diagnostics = []
        try:
            result = await self._discovery_request("vehicle_list", "GET", VEHICLES)
        except CloudError as error:
            # One alternate discovery request can work when the legacy list
            # endpoint fails. Never route around auth failures, throttling,
            # an explicit Retry-After, or failures in the token exchange.
            if (
                error.stage != "vehicle_list"
                or error.retry_after > 0
                or not (error.status in (403, 404, 410) or 500 <= error.status <= 599)
            ):
                raise
        else:
            vehicles = object_at(result, "vehicles").get("vehicle")
            if isinstance(vehicles, list) and vehicles:
                return vehicles
            self.discovery_diagnostics.append(
                "Vehicle list returned no usable vehicle array; trying garage fallback."
            )
        result = await self._discovery_request(
            "garage_list",
            "POST",
            GARAGE,
            text_body="query getVehiclesMBFF { vehicles { vin make model nickName year onstarCapable vehicleType } }",
        )
        vehicles = object_at(result, "data").get("vehicles")
        if result.get("errors") or not isinstance(vehicles, list):
            raise CloudError(
                200,
                stage="garage_list",
                reason="graphql_errors"
                if result.get("errors")
                else "unexpected_payload",
            )
        return [
            {**vehicle, "garage_record": True}
            for vehicle in vehicles
            if isinstance(vehicle, dict)
        ]

    async def vehicle_details(self, vin: str) -> dict[str, Any]:
        """Resolve a garage-only vehicle's advertised powertrain."""
        if not VIN_PATTERN.fullmatch(vin):
            raise CloudError
        async with self.requests:
            response = await self.api.api_request(
                "POST",
                GARAGE,
                text_body=(
                    'query getMbffGarageVehicleDetails { vehicleDetails(vin: "'
                    + vin
                    + '") { vin vehicleMetaData { propulsionAndFuelType { fuelCategory propulsionType } } } }'
                ),
            )
        details = object_at(response, "data", "vehicleDetails")
        returned_vin = details.get("vin")
        if (
            response.get("errors")
            or not isinstance(returned_vin, str)
            or returned_vin.upper() != vin
        ):
            raise CloudError
        return object_at(details, "vehicleMetaData", "propulsionAndFuelType")

    async def vehicles(self) -> dict[str, dict[str, Any]]:
        """Discover the garage once per freshness window."""
        async with self._discovery_lock:
            now = time.monotonic()
            if now < self._discovery_expires:
                return self._vehicles
            deadline = self.endpoint_deadlines.get("vehicles", 0)
            if now < deadline:
                raise CloudError(
                    retry_after=deadline - now,
                    stage="discovery_cooldown",
                    reason="cooldown",
                )
            try:
                vehicles = await self._discover()
            except CloudError as error:
                summary = cloud_error_summary(error)
                if summary not in self.discovery_diagnostics:
                    self.discovery_diagnostics.append(summary)
                self.endpoint_deadlines["vehicles"] = time.monotonic() + max(
                    1800 if error.status == 429 else 60, error.retry_after
                )
                raise
            self._vehicles = {
                item["vin"].upper(): item
                for item in vehicles
                if isinstance(item, dict)
                and isinstance(item.get("vin"), str)
                and VIN_PATTERN.fullmatch(item["vin"].upper())
            }
            self._discovery_expires = time.monotonic() + 3600
            return self._vehicles

    def reuse_discovery(self, other: Account) -> None:
        """Reuse the authorized garage response after linking."""
        self._vehicles = other._vehicles
        self._discovery_expires = other._discovery_expires


class OnStar:
    """Access telemetry and explicitly permitted controls for one VIN."""

    def __init__(
        self,
        account: Account,
        vin: str,
        vehicle: dict[str, Any],
        *,
        controls: bool = False,
    ) -> None:
        self.account = account
        self.vin = vin
        self.vehicle = vehicle
        self.controls = controls
        self._ev_token = ""
        self._ev_vehicle = ""
        self._ev_until = 0.0
        self._legacy_health_until = 0.0

    @property
    def commands(self) -> dict[str, dict[str, Any]]:
        """Read vehicle-specific capabilities from its account record."""
        commands = object_at(self.vehicle, "commands").get("command")
        if not isinstance(commands, list):
            return {}
        return {
            item["name"]: item
            for item in commands
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("url"), str)
        }

    @property
    def diagnostics_supported(self) -> list[str]:
        """Return the vehicle-advertised diagnostic groups."""
        items = object_at(
            self.commands.get("diagnostics"), "commandData", "supportedDiagnostics"
        ).get("supportedDiagnostic")
        return (
            [item for item in items if isinstance(item, str)]
            if isinstance(items, list)
            else []
        )

    @property
    def electric(self) -> bool:
        """Find evidence of an EV powertrain or EV diagnostics."""
        return any(
            name.startswith(("EV ", "CHARGER ")) for name in self.diagnostics_supported
        ) or any(
            str(self.vehicle.get(key, "")).upper()
            in {
                "BEV",
                "EV",
                "BATTERY_ELECTRIC",
                "ELECTRIC",
                "PHEV",
            }
            for key in ("propulsionType", "fuelCategory", "vehicleType")
        )

    def command_available(self, name: str) -> bool:
        """Require an enabled, supported and explicitly non-privileged action."""
        command = self.commands.get(name)
        return (
            self.controls
            and command is not None
            and str(command.get("isPrivSessionRequired", "")).lower() == "false"
        )

    async def _command(
        self,
        name: str,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = self.commands.get(name)
        if command is None:
            raise CloudError(404)
        try:
            path = urlsplit(command["url"]).path
        except ValueError:
            raise CloudError from None
        if not path.endswith(f"/{self.vin}/commands/{name}"):
            raise CloudError
        return await self.account.api.api_request(
            "POST", command["url"], json_body=body, poll=True
        )

    async def diagnostics(self) -> dict[str, Any]:
        """Prefer cached vehicle health and use legacy diagnostics when needed."""
        if time.monotonic() >= self._legacy_health_until:
            try:
                response = await self.account.api.api_request(
                    "GET", f"/api/v1/vh/vehiclehealth/v1/healthstatus/{self.vin}"
                )
            except CloudError as error:
                if error.status not in (403, 404, 410) or error.retry_after:
                    raise
            else:
                if (
                    isinstance(response.get("diagnostics"), list)
                    and response["diagnostics"]
                ):
                    return response
            if self.diagnostics_supported:
                self._legacy_health_until = time.monotonic() + 21600
        if not self.diagnostics_supported:
            raise CloudError(404)
        return await self._command(
            "diagnostics", CommandFactory.diagnostics(self.diagnostics_supported)
        )

    async def location(self) -> dict[str, Any]:
        """Fetch location through the vehicle's advertised telemetry command."""
        return await self._command("location")

    async def execute_command(self, name: str) -> dict[str, Any]:
        """Restrict control actions even when the entity is already registered."""
        if name not in {
            "lockDoor",
            "unlockDoor",
            "start",
            "cancelStart",
            "alert",
        } or not self.command_available(name):
            raise CloudError(403)
        self.vehicle = (await self.account.vehicles()).get(self.vin, {})
        if not self.command_available(name):
            raise CloudError(403)
        bodies = {
            "lockDoor": CommandFactory.lock_door,
            "unlockDoor": CommandFactory.unlock_door,
            "alert": CommandFactory.alert,
        }
        factory = bodies.get(name)
        return await self._command(name, factory() if factory else None)

    def _ev_params(self) -> dict[str, str]:
        return {
            "vehicleVin": self.vin,
            "clientVersion": "8.20.0.8164",
            "clientType": "bev-myowner",
            "buildType": "r",
            "clientLocale": "en-US",
            "deviceId": self.account.auth.device_id,
            "os": "a",
            "ts": str(int(time.time() * 1000)),
            "sid": secrets.token_hex(8).upper(),
            "pid": secrets.token_hex(8).upper(),
        }

    async def _ev_request(self, mobile_token: str) -> dict[str, Any]:
        session = self.account.auth.session
        if time.monotonic() >= self._ev_until:
            result = await request_json(
                session,
                "POST",
                f"{EV_BASE}/admin/initSession",
                EV_HOST,
                params=self._ev_params(),
                data={"token": mobile_token, "vehicleVIN": self.vin},
            )
            results = result.get("results")
            item = results[0] if isinstance(results, list) and results else None
            token = object_at(item, "loginResponse").get("token")
            vehicle = object_at(item, "getVehicleChargingMetricsResponse").get(
                "vehicleId"
            )
            if (
                not token
                or not isinstance(token, str)
                or not vehicle
                or not isinstance(vehicle, str)
            ):
                raise CloudError
            self._ev_token, self._ev_vehicle = token, vehicle
            self._ev_until = time.monotonic() + 1800
        data = await request_json(
            session,
            "GET",
            f"{EV_BASE}/vehicle/getVehicleChargingMetrics",
            EV_HOST,
            params={
                **self._ev_params(),
                "vehicleId": self._ev_vehicle,
                "varch": "globalb",
            },
            headers={"x-gm-mobiletoken": mobile_token, "x-gm-token": self._ev_token},
        )
        results = data.get("results")
        if not isinstance(results, list):
            raise CloudError
        for item in results:
            if isinstance(item, dict) and item.get("vehicleId") == self._ev_vehicle:
                return item
        raise CloudError

    async def ev_metrics(self) -> dict[str, Any]:
        """Read cached EV metrics without invoking the forced vehicle refresh."""
        token = await self.account.auth.access_token()
        try:
            return await self._ev_request(token)
        except CloudError as error:
            if error.status != 401:
                raise
            self._ev_until = 0
            return await self._ev_request(token)
        except AuthError:
            self._ev_until = 0
            raise
