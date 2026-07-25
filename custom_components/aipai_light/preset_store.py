"""Persistent storage for user-saved presets (shared across all lights).

Three slots, empty until the user fills them. There are no built-in presets -
see presets.py for why. Slots are also addressable by name so the Preset select
entity and service calls keep working.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .presets import BUILTIN_PRESETS, SLOT_COUNT, merge

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_presets"
_DATA_KEY = f"{DOMAIN}_preset_store"


class PresetStore:
    """User presets in a fixed number of slots, persisted in .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._custom: dict[str, dict[str, Any]] = {}
        self._slots: list[dict[str, Any] | None] = [None] * SLOT_COUNT
        self._listeners: list = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._custom = data.get("presets", {})
        slots = data.get("slots")
        if isinstance(slots, list):
            self._slots = (list(slots) + [None] * SLOT_COUNT)[:SLOT_COUNT]
        elif self._custom:
            # Upgrade from the pre-slot layout: keep the user's own presets by
            # dropping the first few into slots rather than losing them.
            for i, (name, body) in enumerate(list(self._custom.items())[:SLOT_COUNT]):
                self._slots[i] = {"name": name, "body": body}

    async def _async_save(self) -> None:
        await self._store.async_save({"presets": self._custom, "slots": self._slots})
        for cb in list(self._listeners):
            cb()

    # -- slots -------------------------------------------------------------

    @property
    def slots(self) -> list[dict[str, Any] | None]:
        """The three slots; each is {'name', 'body'} or None if empty."""
        return list(self._slots)

    def slot_names(self) -> list[str | None]:
        return [s["name"] if s else None for s in self._slots]

    async def async_save_slot(self, index: int, name: str, body: dict[str, Any]) -> bool:
        if not 0 <= index < SLOT_COUNT:
            return False
        self._slots[index] = {"name": (name or "").strip() or f"Preset {index + 1}",
                              "body": body}
        await self._async_save()
        return True

    async def async_clear_slot(self, index: int) -> bool:
        if not 0 <= index < SLOT_COUNT or self._slots[index] is None:
            return False
        self._slots[index] = None
        await self._async_save()
        return True

    def add_listener(self, callback) -> None:  # noqa: ANN001
        self._listeners.append(callback)

    @property
    def all(self) -> dict[str, dict[str, Any]]:
        """Every addressable preset: the filled slots, plus any legacy names."""
        by_name = merge(BUILTIN_PRESETS, self._custom)
        for slot in self._slots:
            if slot:
                by_name[slot["name"]] = slot["body"]
        return by_name

    @property
    def names(self) -> list[str]:
        # Slot order first (that's how they appear on the card), then anything
        # else alphabetically.
        ordered = [s["name"] for s in self._slots if s]
        rest = sorted(n for n in self.all if n not in ordered)
        return ordered + rest

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
        removed = False
        for key in list(self._custom):
            if key.lower() == target:
                del self._custom[key]
                removed = True
        for i, slot in enumerate(self._slots):
            if slot and slot["name"].lower() == target:
                self._slots[i] = None
                removed = True
        if removed:
            await self._async_save()
        return removed


async def async_get_store(hass: HomeAssistant) -> PresetStore:
    """One shared store for the whole integration."""
    store: PresetStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = PresetStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
