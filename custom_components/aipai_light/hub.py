"""Per-light state hub: owns the MQTT connection and shared entity state."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DEFAULT_LABELS, DOMAIN
from .mqtt_client import AipaiMqttClient
from .protocol import LightState, build_saveconfig, cmd_to_pct, parse_readconfig, pct_to_cmd

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 45          # default seconds between readconfig polls
CLOCK_INTERVAL = 6 * 3600   # re-sync the device clock this often (drift + DST)


class AipaiLightHub:
    """Holds live state for one AIPAI light and fans out updates to entities."""

    def __init__(
        self, hass: HomeAssistant, serial: str, model_hint: str | None = None,
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self.hass = hass
        self.serial = serial
        self._poll_interval = max(10, int(poll_interval))
        self._connected = False
        self._last_reply = 0.0  # monotonic time of the last device reply
        self._poll_unsub = None
        self._clock_unsub = None
        self.last_ack: dict[str, Any] | None = None  # last saveconfig/clock/moon ack

        # Best-effort defaults until the first readconfig arrives.
        self.state = LightState(
            roads=8, roads_real=8, labels=list(DEFAULT_LABELS),
            road_val_pct=[0] * 8, road_data=[",".join(["0"] * 24)] * 8,
            model=model_hint,
        )
        self.has_state = False
        self.moon: dict[str, Any] = {}
        # Live channel values in command scale (0..1023), indexed by channel.
        self.channels: list[int] = [0] * self.state.roads
        # Remembered non-zero levels so the master switch can restore them.
        self._restore: list[int] = [pct_to_cmd(50)] * self.state.roads

        self._entities: list[Any] = []
        self.client = AipaiMqttClient(
            serial,
            on_message=self._handle_message,
            on_connection_change=self._handle_connection_change,
        )

    # -- lifecycle ---------------------------------------------------------

    def register_entity(self, entity: Any) -> None:
        self._entities.append(entity)

    async def async_connect(self) -> None:
        await self.hass.async_add_executor_job(self.client.connect)
        self._poll_unsub = async_track_time_interval(
            self.hass, self._async_poll, timedelta(seconds=self._poll_interval)
        )
        self._clock_unsub = async_track_time_interval(
            self.hass, self._async_clock_tick, timedelta(seconds=CLOCK_INTERVAL)
        )

    async def async_disconnect(self) -> None:
        for unsub in (self._poll_unsub, self._clock_unsub):
            if unsub:
                unsub()
        self._poll_unsub = self._clock_unsub = None
        await self.hass.async_add_executor_job(self.client.disconnect)

    async def _async_clock_tick(self, _now) -> None:  # noqa: ANN001
        if self._connected:
            self.auto_sync_clock()

    def auto_sync_clock(self) -> None:
        """Keep the device clock right automatically (no vendor-app popup needed).

        Sends an epoch chosen so the device's *local* time matches Home
        Assistant's current local time, including DST. The device applies its
        stored (non-DST) UTC offset, so we compensate here rather than rewriting
        the timezone (which would need a saveconfig and force the light on).
        """
        now = time.time()
        epoch = int(now)
        if self.has_state:
            ha_offset = dt_util.now().utcoffset()
            if ha_offset is not None:
                dev_offset = _safe_int(self.state.timezone) * 3600
                epoch = int(now + ha_offset.total_seconds() - dev_offset)
        self.client.sync_clock(epoch)

    def request_refresh(self) -> None:
        self.client.request_state()

    async def _async_poll(self, _now) -> None:  # noqa: ANN001
        if self._connected:
            self.request_refresh()
        self._notify()  # re-evaluate availability even if the reply never comes

    # -- derived views for entities ---------------------------------------

    @property
    def available(self) -> bool:
        """True only while the broker is connected AND the device is replying."""
        grace = self._poll_interval * 3 + 15
        return self._connected and (time.monotonic() - self._last_reply) < grace

    @property
    def roads(self) -> int:
        return self.state.roads

    @property
    def labels(self) -> list[str]:
        return self.state.labels

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=f"AIPAI Light {self.serial}",
            manufacturer="AIPAI (Doseen)",
            model=self.state.model or "AIPAI Light",
            serial_number=self.serial,
        )

    @property
    def is_on(self) -> bool:
        return any(v > 0 for v in self.channels[: self.roads])

    # -- commands ----------------------------------------------------------

    def set_channel(self, index: int, value_cmd: int) -> None:
        value_cmd = max(0, min(1023, int(value_cmd)))
        self.channels[index] = value_cmd
        if value_cmd > 0:
            self._restore[index] = value_cmd
        self.client.set_channel(_road_code(index), value_cmd)
        self._notify()

    def turn_all_off(self) -> None:
        for i in range(self.roads):
            if self.channels[i] > 0:
                self._restore[i] = self.channels[i]
            self.channels[i] = 0
            self.client.set_channel(_road_code(i), 0)
        self._notify()

    def turn_all_on(self) -> None:
        for i in range(self.roads):
            value = self._restore[i] or pct_to_cmd(50)
            self.channels[i] = value
            self.client.set_channel(_road_code(i), value)
        self._notify()

    def persist_levels(self) -> bool:
        """Save current live levels to the device so they survive a reboot.

        Preserves the existing schedule verbatim. Requires a prior readconfig
        (so we never overwrite the schedule with defaults). Note: a saveconfig
        always powers the light on - that is how the firmware behaves.
        """
        if not self.has_state:
            _LOGGER.warning("persist_levels skipped for %s: no state read yet", self.serial)
            return False
        road_val = list(self.state.road_val_pct)
        for i in range(min(self.roads, len(road_val))):
            road_val[i] = cmd_to_pct(self.channels[i])
        self.state.road_val_pct = road_val
        self.client.save_config(build_saveconfig(self.state))
        return True

    def restart(self) -> None:
        self.client.restart()

    # -- time / schedule / moon -------------------------------------------

    def sync_clock(self, epoch: int | None = None) -> None:
        """Set device clock to a UTC epoch (defaults to real current time)."""
        self.client.sync_clock(epoch)

    def apply_preset(self, preset: dict[str, Any]) -> bool:
        """Apply a named preset (see presets.py) to this fixture.

        A `levels` preset sets the channels now and drops the light into manual
        mode so its stored day schedule doesn't override the look. A `schedule`
        preset writes curves and switches to scheduled mode.
        """
        from .presets import build_curves, build_levels

        labels = self.labels[: self.roads]
        kind = preset.get("kind", "levels")

        if kind == "schedule":
            if not self.has_state:
                _LOGGER.warning("preset skipped for %s: no state read yet", self.serial)
                return False
            curves = build_curves(preset, labels)
            rows = [",".join(str(v) for v in c) for c in curves]
            return self.apply_schedule(road_data=rows, mode="1")

        levels = build_levels(preset, labels)
        # Manual mode first, so the schedule doesn't fight the look. Needs a
        # saveconfig, which the firmware always powers on - harmless here since
        # we immediately set the levels (including all-zero for "All off").
        if self.has_state and self.state.mode != "0":
            self.apply_schedule(mode="0")
        for i, pct in enumerate(levels):
            self.set_channel(i, pct_to_cmd(pct))
        return True

    def set_mode(self, mode: str) -> bool:
        """Set control mode: '0' manual, '1' scheduled (sunrise/sunset)."""
        if not self.has_state:
            _LOGGER.warning("set_mode skipped for %s: no state read yet", self.serial)
            return False
        self.state.mode = "1" if str(mode) == "1" else "0"
        self.client.save_config(build_saveconfig(self.state))
        self._notify()
        return True

    @property
    def mode(self) -> str:
        return self.state.mode

    def set_timezone(self, tz: int) -> bool:
        """Set the device timezone (UTC offset). Persisted via saveconfig."""
        if not self.has_state:
            _LOGGER.warning("set_timezone skipped for %s: no state read yet", self.serial)
            return False
        self.state.timezone = str(int(tz))
        self.client.save_config(build_saveconfig(self.state))
        return True

    def apply_schedule(
        self,
        road_data: list[str] | None = None,
        open_hour: int | None = None,
        close_hour: int | None = None,
        mode: str | None = None,
    ) -> bool:
        """Write new per-channel schedule curves (and optional auto on/off hours).

        `road_data` is a list of 24-value CSV strings, one per firmware road.
        Requires a prior readconfig so untouched fields are preserved.
        """
        if not self.has_state:
            _LOGGER.warning("apply_schedule skipped for %s: no state read yet", self.serial)
            return False
        if road_data is not None:
            n = self.state.roads_real
            rows = list(road_data)[:n] + [",".join(["0"] * 24)] * max(0, n - len(road_data))
            self.state.road_data = rows
        if open_hour is not None:
            self.state.open_hour = int(open_hour)
        if close_hour is not None:
            self.state.close_hour = int(close_hour)
        if mode is not None:
            self.state.mode = "1" if str(mode) == "1" else "0"
        self.client.save_config(build_saveconfig(self.state))
        return True

    def set_moon(
        self,
        color_hex: str,
        level_pct: int,
        start_hhmm: float,
        end_hhmm: float,
        enable: bool,
        save: bool = True,
    ) -> None:
        """Configure the native moonlight timer (spawning trigger).

        color_hex like '#00A0E9'; level_pct 0-100; start/end as decimal hours.
        """
        color = "0x" + color_hex.lstrip("#").upper()
        level_255 = round(max(0, min(100, level_pct)) * 255 / 100)
        self.client.set_moon(color, level_255, start_hhmm, end_hhmm, enable, save)
        self.moon = {
            "color": color_hex, "level": level_pct,
            "start": start_hhmm, "end": end_hhmm, "run": enable,
        }
        self._notify()

    # -- MQTT thread callbacks --------------------------------------------

    def _handle_connection_change(self, connected: bool) -> None:
        self._connected = connected
        self.hass.loop.call_soon_threadsafe(self._notify)
        if connected:
            self.hass.loop.call_soon_threadsafe(self.request_refresh)

    def _handle_message(self, payload: dict[str, Any]) -> None:
        self.hass.loop.call_soon_threadsafe(self._process_message, payload)

    def _process_message(self, payload: dict[str, Any]) -> None:
        msg_type = payload.get("type", "")
        msg = payload.get("msg", "")

        self._last_reply = time.monotonic()  # any reply proves the device is online

        if msg_type == "readconfig":
            first = not self.has_state
            state = parse_readconfig(msg)
            self.state = state
            self.has_state = True
            # Resize live arrays to match the resolved channel count.
            if len(self.channels) != state.roads:
                self.channels = [0] * state.roads
                self._restore = [pct_to_cmd(50)] * state.roads
            for i, cmd in enumerate(state.channel_cmd_values()):
                self.channels[i] = cmd
                if cmd > 0:
                    self._restore[i] = cmd
            self._notify()
            if first:
                # We now know the device's timezone: sync its clock immediately.
                self.auto_sync_clock()
        elif msg_type == "online":
            self._notify()
            self.request_refresh()
        elif msg_type in ("saveconfig", "clock", "moonSet"):
            # Confirmation that a write landed on the device.
            self.last_ack = {"type": msg_type, "msg": msg, "at": dt_util.utcnow().isoformat()}
            _LOGGER.debug("%s ack for %s: %s", msg_type, self.serial, msg)
            self._notify()

        # Moon state may arrive in its own reply; capture it if present.
        if "moonColor" in payload:
            self.moon = {
                "color": "#" + str(payload.get("moonColor", "0x")).replace("0x", ""),
                "level": round(int(payload.get("moonLevel", 0)) * 100 / 255),
                "start": payload.get("moonStart"),
                "end": payload.get("moonEnd"),
                "run": payload.get("moonRun") == 1,
            }
            self._notify()

    def _notify(self) -> None:
        for entity in self._entities:
            entity.async_write_ha_state()


def _road_code(index: int) -> str:
    from .const import ROAD_CODES

    return ROAD_CODES[index]


def _safe_int(value) -> int:  # noqa: ANN001
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
