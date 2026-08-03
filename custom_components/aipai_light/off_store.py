"""Persistence for the timed 'lights off' so it survives a Home Assistant restart.

A timed off is dangerous to lose: if HA restarts while the tank is dark and the
timer is only in memory, the tank stays dark indefinitely. So the deadline and
the state to restore are written to .storage; on startup the hub either reverts
immediately (deadline already passed) or re-arms the timer for the remainder.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_off_timers"
_DATA_KEY = f"{DOMAIN}_off_store"


class OffStore:
    """Pending timed-off per light, keyed by serial."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._data = (data or {}).get("off", {})

    async def _async_write(self) -> None:
        await self._store.async_save({"off": self._data})

    def get(self, serial: str) -> dict[str, Any] | None:
        return self._data.get(str(serial))

    async def async_set(
        self, serial: str, revert_at_iso: str, prev_mode: str, prev_pct: list[int]
    ) -> None:
        self._data[str(serial)] = {
            "revert_at": revert_at_iso,
            "prev_mode": prev_mode,
            "prev_pct": list(prev_pct),
        }
        await self._async_write()

    async def async_clear(self, serial: str) -> None:
        if self._data.pop(str(serial), None) is not None:
            await self._async_write()


async def async_get_off_store(hass: HomeAssistant) -> OffStore:
    store: OffStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = OffStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
