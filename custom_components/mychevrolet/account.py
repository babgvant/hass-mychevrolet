"""Account-level session ownership and private token storage."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util.file import WriteError, write_utf8_file_atomic

from .const import DOMAIN
from .onyonstar.auth import GMAuth
from .onyonstar.client import Account
from .onyonstar.http import AuthError


def account_id(subject: str) -> str:
    """Derive a stable storage key from a verified identity claim."""
    return sha256(subject.encode()).hexdigest()


class TokenStore:
    """Private, atomic persistence that propagates failed token rotations."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid account storage key")
        self.hass = hass
        self.path = Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}.auth.{key}"))

    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError, ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def async_load(self) -> dict[str, Any] | None:
        """Read the account record outside the event loop."""
        return await self.hass.async_add_executor_job(self._load)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_utf8_file_atomic(str(self.path), json.dumps(data), private=True)
        except WriteError:
            raise OSError("Unable to persist GM authorization") from None

    async def async_save(self, data: dict[str, Any]) -> None:
        """Do not release the caller's rotation lock during an in-flight write."""
        job = self.hass.async_add_executor_job(self._write, data)
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise

    async def async_remove(self) -> None:
        """Delete the stored refresh token."""
        await self.hass.async_add_executor_job(self.path.unlink, True)


class Accounts:
    """Own one in-memory session and one private store per account."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.session = async_get_clientsession(hass)
        self.lock = asyncio.Lock()
        self._accounts: dict[str, Account] = {}

    def store(self, key: str) -> TokenStore:
        """Use Home Assistant's atomic, private storage writer."""
        return TokenStore(self.hass, key)

    async def _load(self, key: str) -> Account | None:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            return None
        if key in self._accounts:
            return self._accounts[key]
        store = self.store(key)
        data = await store.async_load()
        if not isinstance(data, dict):
            return None
        subject = data.get("subject")
        refresh = data.get("refresh_token")
        device = data.get("device_id")
        if not all(
            isinstance(value, str) and value for value in (subject, refresh, device)
        ):
            return None
        if account_id(subject) != key:
            return None
        self._accounts[key] = Account(
            GMAuth(
                self.session,
                device,
                refresh,
                subject=subject,
                save=self._save(key, subject, device),
            )
        )
        return self._accounts[key]

    def _save(
        self, key: str, subject: str, device: str
    ) -> Callable[[str], Awaitable[None]]:
        store = self.store(key)

        async def save(refresh: str) -> None:
            await store.async_save(
                {
                    "refresh_token": refresh,
                    "subject": subject,
                    "device_id": device,
                }
            )

        return save

    async def get(self, key: str) -> Account:
        """Load an account session once, including after a restart."""
        async with self.lock:
            account = await self._load(key)
        if account is None:
            raise AuthError
        return account

    async def link(self, candidate: Account) -> str:
        """Commit an authorized candidate to the account-scoped store."""
        subject = candidate.auth.subject
        if not subject:
            raise AuthError
        key = account_id(subject)
        async with self.lock:
            current = await self._load(key)
            if current is None:
                candidate.auth.save = self._save(key, subject, candidate.auth.device_id)
                await candidate.auth.save(candidate.auth.refresh_token)
                self._accounts[key] = candidate
            else:
                await current.auth.replace(candidate.auth)
                current.reuse_discovery(candidate)
        return key

    async def remove(self, key: str) -> None:
        """Remove token storage after the last config entry is removed."""
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            return
        async with self.lock:
            account = self._accounts.pop(key, None)
            delete = self.store(key).async_remove
            if account is None:
                await delete()
            else:
                await account.auth.remove(delete)


def accounts(hass: HomeAssistant) -> Accounts:
    """Get or create the integration's account pool."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = Accounts(hass)
    return hass.data[DOMAIN]
