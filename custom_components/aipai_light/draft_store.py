"""Persisted 'last saved settings' per light, so a preview can be rolled back.

Edits reach the fixture immediately - that is the point of a preview, you want
to judge it in the water. The safety net is here: the configuration as of the
last explicit Save is kept in .storage, so Discard can write it back, and an HA
restart or a closed browser mid-preview never strands the tank on settings
nobody chose to keep.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .draft import snapshot_to_storage, snapshots_differ

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}_saved_settings"
_DATA_KEY = f"{DOMAIN}_draft_store"


class DraftStore:
    """Last-saved configuration for each light, keyed by serial."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._saved: dict[str, dict[str, Any]] = {}
        self._listeners: list = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._saved = (data or {}).get("saved", {})

    async def _async_write(self) -> None:
        await self._store.async_save({"saved": self._saved})
        for cb in list(self._listeners):
            cb()

    def add_listener(self, callback) -> None:  # noqa: ANN001
        self._listeners.append(callback)

    # -- reading -----------------------------------------------------------

    def saved(self, serial: str) -> dict[str, Any] | None:
        """The last explicitly saved configuration, or None if never saved."""
        entry = self._saved.get(str(serial))
        return entry.get("snapshot") if entry else None

    def saved_at(self, serial: str) -> str | None:
        entry = self._saved.get(str(serial))
        return entry.get("saved_at") if entry else None

    def has_unsaved(self, serial: str, current: Any) -> bool:
        """True when the light is running something other than its saved copy.

        With no baseline yet we report False: on a fresh install the device's
        own configuration is, by definition, what the user is already living
        with - flagging it as 'unsaved' the moment the integration loads would
        be noise.
        """
        baseline = self.saved(serial)
        if baseline is None:
            return False
        return snapshots_differ(baseline, current)

    # -- writing -----------------------------------------------------------

    async def async_commit(self, serial: str, snapshot: Any) -> None:
        """Promote what the light is running now to be the saved copy."""
        self._saved[str(serial)] = {
            "snapshot": snapshot_to_storage(snapshot),
            "saved_at": dt_util.utcnow().isoformat(),
        }
        await self._async_write()

    async def async_forget(self, serial: str) -> None:
        """Drop the baseline (e.g. the light was removed)."""
        if self._saved.pop(str(serial), None) is not None:
            await self._async_write()


async def async_get_draft_store(hass: HomeAssistant) -> DraftStore:
    """One shared store for the whole integration."""
    store: DraftStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = DraftStore(hass)
        await store.async_load()
        hass.data[_DATA_KEY] = store
    return store
