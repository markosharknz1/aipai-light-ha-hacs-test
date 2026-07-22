"""Persistent storage for user-defined presets (shared across all lights)."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .presets import BUILTIN_PRESETS, merge

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_presets"
_DATA_KEY = f"{DOMAIN}_preset_store"


class PresetStore:
    """Custom presets, persisted in .storage and merged over the built-ins."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._custom: dict[str, dict[str, Any]] = {}
        self._listeners: list = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._custom = (data or {}).get("presets", {})

    async def _async_save(self) -> None:
        await self._store.async_save({"presets": self._custom})
        for cb in list(self._listeners):
            cb()

    def add_listener(self, callback) -> None:  # noqa: ANN001
        self._listeners.append(callback)

    @property
    def all(self) -> dict[str, dict[str, Any]]:
        return merge(BUILTIN_PRESETS, self._custom)

    @property
    def names(self) -> list[str]:
        return sorted(self.all)

    def get(self, name: str) -> dict[str, Any] | None:
        # Case-insensitive lookup so service calls are forgiving.
        target = (name or "").strip().lower()
        for key, preset in self.all.items():
            if key.lower() == target:
                return preset
        return None

    async def async_save_preset(self, name: str, preset: dict[str, Any]) -> None:
        self._custom[name.strip()] = preset
        await self._async_save()

    async def async_delete_preset(self, name: str) -> bool:
        target = (name or "").strip().lower()
        for key in list(self._custom):
            if key.lower() == target:
                del self._custom[key]
                await self._async_save()
                return True
        return False


async def async_get_store(hass: HomeAssistant) -> PresetStore:
    """One shared store for the whole integration."""
    store: PresetStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = PresetStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
