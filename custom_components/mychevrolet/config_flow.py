"""Home Assistant setup, manual browser authorization and reauthentication."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .account import account_id, accounts
from .const import (
    CONF_ACCOUNT,
    CONF_CONTROLS,
    CONF_DEVICE_ID,
    CONF_LOCATION,
    CONF_SCAN_INTERVAL,
    CONF_VEHICLES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .onyonstar.auth import BrowserAttempt, GMAuth
from .onyonstar.auth.native import NativeLogin, NativeUnavailable
from .onyonstar.client import Account
from .onyonstar.http import AuthError, CloudError

SECRET_INPUT = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


class MyChevroletConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Authenticate a GM account and select its vehicles."""

    VERSION = 2
    _attempt: BrowserAttempt | None = None
    _candidate: Account | None = None
    _vehicles: dict[str, str]
    _reauth: bool = False
    _native: NativeLogin | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer browser sign-in or a locally obtained token set."""
        return self.async_show_menu(
            step_id="user", menu_options=["browser", "native", "token_import"]
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Keep the existing entry, VINs and entity identities."""
        self._reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self) -> ConfigFlowResult:
        """Present reauthentication methods."""
        return self.async_show_menu(
            step_id="reauth_confirm", menu_options=["browser", "native", "token_import"]
        )

    def _clear_native(self) -> None:
        if self._native is not None:
            self._native.clear()
            self._native = None

    async def async_step_native(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Try the observed password/authenticator journey without saving credentials."""
        error = None
        if user_input is not None:
            self._clear_native()
            self._candidate = await self._new_candidate()
            self._native = NativeLogin(self._candidate.auth)
            try:
                needs_mfa = await self._native.start(
                    user_input["username"].strip(), user_input["password"]
                )
            except NativeUnavailable:
                self._clear_native()
                return await self.async_step_native_unavailable()
            except (AuthError, CloudError, OSError) as err:
                error = (
                    "invalid_auth" if isinstance(err, AuthError) else "cannot_connect"
                )
                self._clear_native()
            else:
                if needs_mfa:
                    return await self.async_step_mfa()
                self._clear_native()
                return await self._discover()
        return self.async_show_form(
            step_id="native",
            data_schema=vol.Schema(
                {vol.Required("username"): str, vol.Required("password"): SECRET_INPUT}
            ),
            errors={"base": error} if error else {},
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the current authenticator code, never an authenticator seed."""
        if self._native is None:
            return await self.async_step_native()
        error = None
        if user_input is not None:
            try:
                await self._native.verify(user_input["code"].strip())
            except NativeUnavailable:
                self._clear_native()
                return await self.async_step_native_unavailable()
            except AuthError:
                if (
                    self._native.attempt.consumed
                    or time.monotonic() >= self._native.attempt.deadline
                ):
                    self._clear_native()
                    return await self.async_step_native()
                error = "invalid_mfa"
            except CloudError, OSError:
                self._clear_native()
                return await self.async_step_native_unavailable()
            else:
                self._clear_native()
                return await self._discover()
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required("code"): SECRET_INPUT}),
            errors={"base": error} if error else {},
        )

    async def async_step_native_unavailable(self) -> ConfigFlowResult:
        """Stop rather than retry blocked or unrecognized website flows."""
        return self.async_show_menu(
            step_id="native_unavailable", menu_options=["browser", "token_import"]
        )

    async def _new_candidate(self) -> Account:
        pool = accounts(self.hass)
        device = str(uuid4())
        if self._reauth:
            entry = self._get_reauth_entry()
            device = entry.data.get(CONF_DEVICE_ID, device)
            if key := entry.data.get(CONF_ACCOUNT):
                try:
                    linked = await pool.get(key)
                except AuthError:
                    pass
                else:
                    device = linked.auth.device_id
        return Account(GMAuth(pool.session, device))

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display GM's authorization URL and accept its mobile redirect."""
        self._clear_native()
        error = None
        if self._candidate is None:
            self._candidate = await self._new_candidate()
        metadata = None
        try:
            metadata = await self._candidate.auth.metadata()
            if user_input is not None and self._attempt is not None:
                await self._candidate.auth.authorize(
                    self._attempt, user_input["redirect"]
                )
                return await self._discover()
        except (AuthError, CloudError, OSError) as err:
            error = "invalid_auth" if isinstance(err, AuthError) else "cannot_connect"
            if metadata is None:
                return await self.async_step_browser_unavailable()
        if self._attempt is None or self._attempt.consumed or error:
            self._attempt = BrowserAttempt()
        return self.async_show_form(
            step_id="browser",
            data_schema=vol.Schema({vol.Required("redirect"): SECRET_INPUT}),
            description_placeholders={
                "url": self._attempt.url(metadata["authorization_endpoint"])
            },
            errors={"base": error} if error else {},
        )

    async def async_step_browser_unavailable(self) -> ConfigFlowResult:
        """Offer a retry or the independent token import path."""
        return self.async_show_menu(
            step_id="browser_unavailable", menu_options=["browser", "token_import"]
        )

    async def async_step_token_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import a Microsoft refresh token; confirm it directly with GM."""
        self._clear_native()
        error = None
        if user_input is not None:
            try:
                value = user_input["tokens"].strip()
                if not value or len(value) > 65536:
                    raise AuthError
                tokens = (
                    json.loads(value)
                    if value.startswith("{")
                    else {"refresh_token": value}
                )
                if not isinstance(tokens, dict):
                    raise AuthError
                self._candidate = await self._new_candidate()
                await self._candidate.auth.import_tokens(tokens)
                return await self._discover()
            except AuthError, ValueError:
                error = "invalid_auth"
            except CloudError, OSError:
                error = "cannot_connect"
        return self.async_show_form(
            step_id="token_import",
            data_schema=vol.Schema({vol.Required("tokens"): SECRET_INPUT}),
            errors={"base": error} if error else {},
        )

    async def _discover(self) -> ConfigFlowResult:
        assert self._candidate is not None and self._candidate.auth.subject is not None
        key = account_id(self._candidate.auth.subject)
        if self._reauth:
            entry = self._get_reauth_entry()
            if entry.data.get(CONF_ACCOUNT, key) != key:
                return self.async_abort(reason="wrong_account")
        else:
            await self.async_set_unique_id(key)
            self._abort_if_unique_id_configured()
        try:
            records = await self._candidate.vehicles()
        except CloudError, OSError:
            return await self.async_step_retry_discovery()
        if self._reauth:
            if not set(entry.data[CONF_VEHICLES]).issubset(records):
                return self.async_abort(reason="wrong_account")
            await accounts(self.hass).link(self._candidate)
            for other in self._async_current_entries():
                if (
                    other.entry_id != entry.entry_id
                    and other.data.get(CONF_ACCOUNT) == key
                ):
                    self.hass.config_entries.async_schedule_reload(other.entry_id)
            data = {CONF_ACCOUNT: key, CONF_VEHICLES: entry.data[CONF_VEHICLES]}
            return self.async_update_reload_and_abort(entry, data=data)

        claimed = {
            vin
            for entry in self._async_current_entries()
            for vin in entry.data.get(CONF_VEHICLES, [entry.data.get("vin")])
        }
        self._vehicles = {
            vin: vehicle_label(record, vin)
            for vin, record in records.items()
            if vin not in claimed
        }
        if not self._vehicles:
            return self.async_abort(reason="no_new_vehicles")
        return await self.async_step_vehicles()

    async def async_step_retry_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry discovery without starting a second OAuth grant."""
        if user_input is not None:
            try:
                return await self._discover()
            except AuthError:
                return self.async_abort(reason="authorization_expired")
        return self.async_show_form(
            step_id="retry_discovery", data_schema=vol.Schema({})
        )

    async def async_step_vehicles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the vehicles that belong to this config entry."""
        errors = {}
        if user_input is not None:
            selected = list(dict.fromkeys(user_input[CONF_VEHICLES]))
            if selected and all(vin in self._vehicles for vin in selected):
                claimed = {
                    vin
                    for entry in self._async_current_entries()
                    for vin in entry.data.get(CONF_VEHICLES, [entry.data.get("vin")])
                }
                if claimed.intersection(selected):
                    return self.async_abort(reason="already_configured")
                assert self._candidate is not None
                try:
                    key = await accounts(self.hass).link(self._candidate)
                except CloudError, OSError:
                    errors["base"] = "cannot_connect"
                else:
                    title = (
                        self._vehicles[selected[0]]
                        if len(selected) == 1
                        else "myChevrolet"
                    )
                    return self.async_create_entry(
                        title=title, data={CONF_ACCOUNT: key, CONF_VEHICLES: selected}
                    )
            else:
                errors["base"] = "select_vehicles"
        return self.async_show_form(
            step_id="vehicles",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VEHICLES, default=list(self._vehicles)
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            multiple=True,
                            options=[
                                {"label": title, "value": vin}
                                for vin, title in self._vehicles.items()
                            ],
                        )
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Provide polling and privacy settings."""
        return MyChevroletOptionsFlow()


def vehicle_label(vehicle: dict[str, Any], vin: str) -> str:
    """Label a vehicle without using its full VIN as the entry title."""
    label = vehicle.get("name") or vehicle.get("model") or "GM vehicle"
    return f"{label} ({vin[-4:]})"


class MyChevroletOptionsFlow(config_entries.OptionsFlowWithReload):
    """Configure an entry's polling, privacy and remote actions."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update options and reload the selected platforms."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=1440)
                    ),
                    vol.Required(
                        CONF_LOCATION, default=current.get(CONF_LOCATION, False)
                    ): bool,
                    vol.Required(
                        CONF_CONTROLS, default=current.get(CONF_CONTROLS, False)
                    ): bool,
                }
            ),
        )
