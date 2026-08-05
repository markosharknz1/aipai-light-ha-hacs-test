"""Persistence for the schedule's two layers, per light.

The device stores ONE set of 24-hour curves, but we treat it as two layers we
own separately:

* ``day``   - the daytime schedule (from the visual editor), night hours zeroed.
* ``night`` - the simulated night light (window, level, channels).

The device curves are always rebuilt as ``day`` with ``night`` laid over it, so
every save writes exactly the current settings and wipes any earlier residue -
and editing the day schedule no longer clobbers the night light, or vice versa.
We keep the two layers here because the device can't tell us which hours are
which.
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
    """The day and night layers per light, keyed by serial."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._night: dict[str, dict[str, Any]] = {}
        self._day: dict[str, list[list[int]]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._night = data.get("night", {})
        self._day = data.get("day", {})

    async def _async_write(self) -> None:
        await self._store.async_save({"night": self._night, "day": self._day})

    def get_night(self, serial: str) -> dict[str, Any] | None:
        return self._night.get(str(serial))

    def get_day(self, serial: str) -> list[list[int]] | None:
        return self._day.get(str(serial))

    async def async_set_night(self, serial: str, config: dict[str, Any]) -> None:
        self._night[str(serial)] = dict(config)
        await self._async_write()

    async def async_clear_night(self, serial: str) -> None:
        if self._night.pop(str(serial), None) is not None:
            await self._async_write()

    async def async_set_day(self, serial: str, curves: list[list[int]]) -> None:
        self._day[str(serial)] = [list(c) for c in curves]
        await self._async_write()


async def async_get_night_store(hass: HomeAssistant) -> NightStore:
    store: NightStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = NightStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
