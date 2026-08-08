"""Local (LAN) transport for a light — a drop-in for AipaiMqttClient.

Talks straight to the light's own HTTP server instead of the vendor cloud
broker, using the endpoints confirmed on real hardware:

  * ``GET /?read=config``      -> the pipe-delimited state (same as the MQTT dev topic)
  * ``GET /?w=512`` (b=, r=…)  -> live per-channel set (0..1023)
  * ``GET /?save=<cfg>``       -> persist levels/schedule (the build_saveconfig string)
  * ``GET /?clock=<epoch>``    -> set the device clock

It presents the SAME method surface the hub already uses for the MQTT client and
feeds the hub's ``on_message`` / ``on_connection_change`` callbacks identically,
so switching transport is invisible to the rest of the integration. The device
still keeps its own outbound cloud connection (firmware) — this just means *our*
control never touches the cloud.

Requests are async (aiohttp). The hub calls these methods from the event loop
(and ``connect`` from an executor thread), so every call is marshalled onto the
loop with ``call_soon_threadsafe`` to stay thread-safe either way. If a request
fails (e.g. the light's DHCP lease changed its IP), it re-scans for the serial
once and retries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .protocol import extract_config_body

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=4)


class AipaiLocalClient:
    """One LAN HTTP connection to a single light (drop-in for AipaiMqttClient)."""

    def __init__(
        self,
        hass: HomeAssistant,
        serial: str,
        ip: str,
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._hass = hass
        self._serial = serial
        self._ip = ip
        self._on_message = on_message
        self._on_conn = on_connection_change

    @property
    def ip(self) -> str:
        return self._ip

    # -- lifecycle (connect is called from an executor thread by the hub) ---
    def connect(self) -> None:
        # No persistent connection to open; just prove reachability with a read,
        # which also seeds the first state and flips availability on.
        self._submit(self._async_read())

    def disconnect(self) -> None:
        pass  # nothing to tear down

    # -- command surface (mirrors AipaiMqttClient) -------------------------
    def request_state(self) -> None:
        self._submit(self._async_read())

    def set_channel(self, letter: str, value_cmd: int) -> None:
        v = max(0, min(1023, int(value_cmd)))
        self._submit(self._async_cmd(f"{letter}={v}"))

    def save_config(self, save_msg: str) -> None:
        self._submit(self._async_cmd(f"save={save_msg}"))

    def sync_clock(self, epoch: int | None = None) -> None:
        ts = int(time.time()) if epoch is None else int(epoch)
        self._submit(self._async_cmd(f"clock={ts}"))

    def set_moon(self, *args: Any, **kwargs: Any) -> None:
        # The native moon timer isn't used in local mode; moonlight is done via
        # the schedule (save_config). No-op so the hub's call path stays valid.
        _LOGGER.debug("set_moon ignored in local mode for %s", self._serial)

    def restart(self) -> None:
        self._submit(self._async_cmd("node=restart"))

    # -- internals ---------------------------------------------------------
    def _submit(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule a coroutine on the event loop from any thread.

        Uses a BACKGROUND task - a plain async_create_task is awaited by HA's
        bootstrap, so a slow/pending HTTP read blocks setup ("Setup timed out for
        stage 2 waiting on AipaiLocalClient._async_read"). Background tasks aren't
        awaited at setup boundaries, so reads/commands never hold up startup.
        """
        self._hass.loop.call_soon_threadsafe(self._spawn, coro)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self._hass.async_create_background_task(coro, name=f"aipai_local_{self._serial}")

    async def _async_read(self) -> None:
        raw = await self._fetch("read=config")
        if raw is None:
            self._set_conn(False)
            return
        body = extract_config_body(raw)
        if body:
            self._on_message({"type": "readconfig", "msg": body})
            self._set_conn(True)
        else:
            _LOGGER.debug("No config body in reply from %s: %r", self._ip, raw[:80])
            self._set_conn(False)

    async def _async_cmd(self, query: str) -> None:
        raw = await self._fetch(query)
        self._set_conn(raw is not None)

    async def _fetch(self, query: str) -> str | None:
        """GET /?<query>; on failure re-resolve the IP by serial and retry once."""
        session = async_get_clientsession(self._hass)
        for attempt in (1, 2):
            try:
                async with session.get(f"http://{self._ip}/?{query}", timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    _LOGGER.debug("%s -> HTTP %s for %s", self._ip, resp.status, query)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("local request to %s failed (%s): %s", self._ip, query, err)
            if attempt == 1 and not await self._reresolve_ip():
                break  # couldn't find it on the network; don't bother retrying
        return None

    async def _reresolve_ip(self) -> bool:
        """The light may have moved (DHCP). Re-scan for its serial."""
        from .discovery import async_find_ip

        ip = await async_find_ip(self._hass, self._serial)
        if ip and ip != self._ip:
            _LOGGER.info("Light %s moved to %s (was %s)", self._serial, ip, self._ip)
            self._ip = ip
            return True
        return ip is not None

    def _set_conn(self, ok: bool) -> None:
        if self._on_conn:
            self._on_conn(bool(ok))
