"""Persistence for the simulated night light (moonlight), per light.

The night light is baked into the ordinary schedule curves, which means the
device can't tell us "these hours are the night light" versus the day schedule.
So we remember what we applied - window, level, channels - here. That lets each
apply CLEAR the previous window before writing the new one (so changing the time
doesn't leave the old hours lit), and lets the card show the real current
settings instead of guessing.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_night_lights"
_DATA_KEY = f"{DOMAIN}_night_store"


class NightStore:
    """Applied night-light config per light, keyed by serial."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._data = (data or {}).get("night", {})

    async def _async_write(self) -> None:
        await self._store.async_save({"night": self._data})

    def get(self, serial: str) -> dict[str, Any] | None:
        return self._data.get(str(serial))

    async def async_set(self, serial: str, config: dict[str, Any]) -> None:
        self._data[str(serial)] = dict(config)
        await self._async_write()

    async def async_clear(self, serial: str) -> None:
        if self._data.pop(str(serial), None) is not None:
            await self._async_write()


async def async_get_night_store(hass: HomeAssistant) -> NightStore:
    store: NightStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = NightStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
