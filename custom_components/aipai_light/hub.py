"""Per-light state hub: owns the MQTT connection and shared entity state."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import DEFAULT_LABELS, DOMAIN
from .mqtt_client import AipaiMqttClient
from .protocol import LightState, build_saveconfig, cmd_to_pct, parse_readconfig, pct_to_cmd

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 45              # default seconds between readconfig polls
CLOCK_CHECK_INTERVAL = 3600    # check the UTC offset this often (catches DST fast)
CLOCK_RESYNC_INTERVAL = 6 * 3600  # force a clock re-sync at least this often (drift)


class AipaiLightHub:
    """Holds live state for one AIPAI light and fans out updates to entities."""

    def __init__(
        self, hass: HomeAssistant, serial: str, model_hint: str | None = None,
        poll_interval: int = POLL_INTERVAL, name: str | None = None,
    ) -> None:
        self.hass = hass
        self.serial = serial
        # Friendly device name (from the config flow). The user can still rename
        # the device in the HA UI afterwards - that override wins in the registry.
        self.name = (name or "").strip() or f"AIPAI Light {serial}"
        self._poll_interval = max(10, int(poll_interval))
        self._connected = False
        self._last_reply = 0.0  # monotonic time of the last device reply
        self._poll_unsub = None
        self._clock_unsub = None
        self._last_offset: float | None = None   # UTC offset at the last clock sync
        self._last_clock_sync = 0.0              # monotonic time of the last sync
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
        # Schedule layers: the daytime curves (from the editor) and the night
        # light config, kept apart so every save rebuilds device = day + night.
        self.day_curves: list[list[int]] | None = None
        self.night_config: dict[str, Any] | None = None
        self._night_store = None
        # Timed lights-off state.
        self._off_store = None
        self._off_unsub = None
        self._off_prev: tuple[str, list[int]] | None = None
        self.off_until = None
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
            self.hass, self._async_clock_tick, timedelta(seconds=CLOCK_CHECK_INTERVAL)
        )

    async def async_disconnect(self) -> None:
        for unsub in (self._poll_unsub, self._clock_unsub, self._off_unsub):
            if unsub:
                unsub()
        self._poll_unsub = self._clock_unsub = self._off_unsub = None
        await self.hass.async_add_executor_job(self.client.disconnect)

    async def _async_clock_tick(self, _now) -> None:  # noqa: ANN001
        """Hourly: re-sync if the UTC offset changed (DST) or a full re-sync is due.

        Catching an offset change means a DST changeover (or the half-hour Adelaide
        transition) is corrected within the hour instead of waiting up to 6h - and
        we only send an extra command on the day it actually changes.
        """
        if not self._connected:
            return
        from .schedule import clock_needs_resync

        offset = self._current_offset()
        elapsed = time.monotonic() - self._last_clock_sync
        if clock_needs_resync(offset, self._last_offset, elapsed, CLOCK_RESYNC_INTERVAL):
            _LOGGER.debug(
                "clock sync for %s (offset now=%s, was=%s, elapsed=%ds)",
                self.serial, offset, self._last_offset, int(elapsed),
            )
            self.auto_sync_clock()

    @staticmethod
    def _current_offset() -> float | None:
        off = dt_util.now().utcoffset()
        return off.total_seconds() if off is not None else None

    def _compensated_epoch(self) -> int:
        """Epoch that makes the device show HA's local time.

        Compensates for the device storing only a WHOLE-HOUR timezone, so
        half-hour zones (e.g. Adelaide UTC+9:30) and DST come out right. Falls
        back to raw UTC only before the first state read (device tz unknown).
        """
        from .schedule import clock_epoch

        now = time.time()
        if self.has_state:
            ha_offset = dt_util.now().utcoffset()
            if ha_offset is not None:
                return clock_epoch(
                    now, ha_offset.total_seconds(), _safe_int(self.state.timezone)
                )
        return int(now)

    def auto_sync_clock(self) -> None:
        """Keep the device clock right automatically (no vendor-app popup needed)."""
        self.client.sync_clock(self._compensated_epoch())
        # Remember the offset/time we just synced for, so the hourly tick can tell
        # when DST (or the half-hour Adelaide flip) has changed it.
        self._last_offset = self._current_offset()
        self._last_clock_sync = time.monotonic()

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
    def moon_capable(self) -> bool:
        """Whether this model has a moonlight timer (A7-S line does not)."""
        from .const import model_has_moon

        return model_has_moon(self.state.model or "")

    @property
    def labels(self) -> list[str]:
        return self.state.labels

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=self.name,
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
        _LOGGER.debug("WRITE %s set_channel ch%d = %d", self.serial, index, value_cmd)
        self.channels[index] = value_cmd
        if value_cmd > 0:
            self._restore[index] = value_cmd
        self.client.set_channel(_road_code(index), value_cmd)
        self._notify()

    def turn_all_off(self) -> None:
        _LOGGER.debug("WRITE %s turn_all_off", self.serial)
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

    # -- timed lights-off --------------------------------------------------

    def attach_off_store(self, store) -> None:  # noqa: ANN001
        self._off_store = store

    async def async_lights_off(self, revert_at, *, persist: bool = True) -> None:
        """Turn the tank off now and schedule its return at ``revert_at``.

        Captures what to restore first: if the light was on its schedule
        (mode 1) it resumes the schedule on revert; if it was manual, its levels
        come back. Persisted so a restart re-arms or reverts (see off_store).
        """
        from datetime import datetime

        prev_mode = self.state.mode if self.has_state else "1"
        prev_pct = [cmd_to_pct(c) for c in self.channels[: self.roads]]
        self._off_prev = (prev_mode, prev_pct)

        self._cancel_off_timer()
        self.turn_all_off()
        if self.has_state and self.state.mode != "0":
            self.set_mode("0")  # hold off; don't let the schedule re-light it

        self.off_until = revert_at
        self._off_unsub = async_track_point_in_time(
            self.hass, self._on_off_expired, revert_at
        )
        if persist and self._off_store is not None:
            iso = revert_at.isoformat() if isinstance(revert_at, datetime) else str(revert_at)
            await self._off_store.async_set(self.serial, iso, prev_mode, prev_pct)
        _LOGGER.info("Lights off for %s until %s", self.serial, self.off_until)
        self._notify()

    async def async_cancel_off(self, *, revert: bool = True) -> None:
        """Cancel a pending timed-off; by default restore the tank now."""
        self._cancel_off_timer()
        prev = self._off_prev
        self.off_until = None
        self._off_prev = None
        if self._off_store is not None:
            await self._off_store.async_clear(self.serial)
        if revert and prev is not None:
            self._apply_revert(*prev)
        self._notify()

    async def async_restore_off(self, entry: dict) -> None:
        """On startup, re-arm or immediately revert a persisted timed-off."""
        try:
            revert_at = dt_util.parse_datetime(entry["revert_at"])
        except (KeyError, TypeError):
            revert_at = None
        prev_mode = entry.get("prev_mode", "1")
        prev_pct = entry.get("prev_pct", [])
        self._off_prev = (prev_mode, prev_pct)
        if revert_at is None or revert_at <= dt_util.utcnow():
            # Deadline already passed while HA was down - bring the tank back.
            _LOGGER.info("Timed-off for %s already expired; restoring", self.serial)
            self._apply_revert(prev_mode, prev_pct)
            if self._off_store is not None:
                await self._off_store.async_clear(self.serial)
            return
        # Still within the window - hold off and re-arm for the remainder.
        self.off_until = revert_at
        self.turn_all_off()
        self._off_unsub = async_track_point_in_time(
            self.hass, self._on_off_expired, revert_at
        )
        _LOGGER.info("Re-armed timed-off for %s until %s", self.serial, revert_at)
        self._notify()

    def _cancel_off_timer(self) -> None:
        if self._off_unsub:
            self._off_unsub()
            self._off_unsub = None

    @callback
    def _on_off_expired(self, _now) -> None:  # noqa: ANN001
        # @callback => runs in the event loop, so async_create_task is safe.
        self.hass.async_create_task(self._async_off_expired())

    async def _async_off_expired(self) -> None:
        prev = self._off_prev or ("1", [])
        self._off_unsub = None
        self.off_until = None
        self._off_prev = None
        if self._off_store is not None:
            await self._off_store.async_clear(self.serial)
        self._apply_revert(*prev)
        self._notify()

    def _apply_revert(self, prev_mode: str, prev_pct: list[int]) -> None:
        if self.has_state and str(prev_mode) == "1":
            self.set_mode("1")  # resume the stored schedule
        else:
            for i, pct in enumerate(prev_pct[: self.roads]):
                self.set_channel(i, pct_to_cmd(pct))

    # -- time / schedule / moon -------------------------------------------

    def sync_clock(self, epoch: int | None = None) -> None:
        """Set the device clock. With no epoch, compute the compensated one so the
        manual SYNC CLOCK matches auto-sync (correct on half-hour zones like
        Adelaide) instead of sending raw UTC."""
        self.client.sync_clock(epoch if epoch is not None else self._compensated_epoch())

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
        _LOGGER.debug(
            "WRITE %s apply_preset (levels) -> MANUAL mode, levels=%s",
            self.serial, levels,
        )
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
        _LOGGER.debug("WRITE %s set_mode -> %s", self.serial, self.state.mode)
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
        _LOGGER.debug(
            "WRITE %s apply_schedule mode=%s curves=%s (saveconfig forces power on)",
            self.serial, self.state.mode, "yes" if road_data is not None else "unchanged",
        )
        self.client.save_config(build_saveconfig(self.state))
        return True

    def apply_night_light(
        self,
        start_hour: int,
        end_hour: int,
        level_pct: int,
        channels: list[int],
        enable: bool,
    ) -> bool:
        """Simulated moonlight: low level on chosen channels across a night window.

        Written into the ordinary schedule curves and persisted via saveconfig -
        so it works on every model, including those with no native moon timer
        (e.g. the A7-S line). Requires a prior readconfig.
        """
        if not self.has_state:
            _LOGGER.warning("apply_night_light skipped for %s: no state read yet", self.serial)
            return False
        from .schedule import curve_to_csv

        # Rebuild from the clean daytime layer, so this write is exactly the day
        # schedule plus the current night light - any earlier night residue (from
        # other windows/channels/levels) is wiped, not stacked on.
        day = self._day_base()
        self.day_curves = day                 # lock in the base we rebuilt from
        self.night_config = (
            {
                "enable": True,
                "start_hour": int(start_hour) % 24,
                "end_hour": int(end_hour) % 24,
                "level": int(level_pct),
                "channels": list(channels),
            }
            if enable
            else None
        )
        merged = self._compose(day)
        _LOGGER.debug(
            "WRITE %s night_light %02d:00-%02d:00 level=%d ch=%s enable=%s",
            self.serial, int(start_hour) % 24, int(end_hour) % 24, level_pct, channels, enable,
        )
        ok = self.apply_schedule(road_data=[curve_to_csv(r) for r in merged])
        self._persist_night()
        self._persist_day()
        return ok

    def attach_night_store(self, store) -> None:  # noqa: ANN001
        """Wire up schedule-layer persistence and restore the saved layers."""
        self._night_store = store
        saved = store.get_night(self.serial)
        if saved:
            self.night_config = saved
        day = store.get_day(self.serial)
        if day:
            self.day_curves = [list(c) for c in day]

    def _persist_night(self) -> None:
        if self._night_store is None:
            return
        cfg = self.night_config
        coro = (
            self._night_store.async_set_night(self.serial, cfg)
            if cfg
            else self._night_store.async_clear_night(self.serial)
        )
        self.hass.async_create_task(coro)

    def _persist_day(self) -> None:
        if self._night_store is not None and self.day_curves is not None:
            self.hass.async_create_task(
                self._night_store.async_set_day(self.serial, self.day_curves)
            )

    def _device_rows(self) -> list[list[int]]:
        """Current on-device curves as int rows (per firmware road)."""
        rows: list[list[int]] = []
        for row in self.state.road_data:
            vals = [_safe_int(x) for x in str(row).split(",") if x != ""]
            rows.append((vals + [0] * 24)[:24])
        return rows

    def _day_base(self) -> list[list[int]]:
        """The daytime layer to build on.

        Prefer the stored clean day layer (captured from the schedule editor). If
        we don't have one yet, reconstruct it from the device by clearing the
        tracked night window - correct once a clean baseline exists.
        """
        from .schedule import overlay_night

        if self.day_curves is not None:
            return [list(c) for c in self.day_curves]
        rows = self._device_rows()
        prev = self.night_config
        if prev and prev.get("enable"):
            rows = overlay_night(rows, prev["start_hour"], prev["end_hour"], [], 0, False)
        return rows

    def _compose(self, day: list[list[int]]) -> list[list[int]]:
        """Full device curves = the day layer with the night light laid over it."""
        from .schedule import overlay_night

        cfg = self.night_config
        if cfg and cfg.get("enable"):
            return overlay_night(
                day, cfg["start_hour"], cfg["end_hour"],
                cfg["channels"], cfg["level"], True,
            )
        return [list(c) for c in day]

    def blue_channels(self) -> list[int]:
        """Channel indices whose label looks like blue - the moonlight default."""
        blue = [i for i, lab in enumerate(self.labels) if "blue" in str(lab).lower()]
        if blue:
            return blue
        # No blue label (unusual): fall back to channel 2 (index 1), the usual blue.
        return [1] if self.roads > 1 else [0]

    # -- preview / save / discard -----------------------------------------

    def capture_snapshot(self) -> dict[str, Any] | None:
        """The light's current *settings* - the thing Save commits.

        Deliberately excludes live state (channel values, temperature): those
        change constantly and must never register as an unsaved change.
        """
        if not self.has_state:
            return None
        return {
            "road_data": list(self.state.road_data),
            "mode": self.state.mode,
            "moon": dict(self.moon),
        }

    def apply_points(self, points: list[dict]) -> bool:
        """Preview a time-point schedule on the fixture straight away.

        `points` is [{"hour": 7, "levels": [..per channel..]}, ...]. Writing is
        immediate by design - the whole reason to preview is to judge it in the
        water. Roll-back safety comes from the saved baseline in draft_store.
        """
        from .schedule import build_curves_from_points, curve_to_csv

        if not self.has_state:
            _LOGGER.warning("apply_points skipped for %s: no state read yet", self.serial)
            return False
        _LOGGER.debug("WRITE %s apply_points (preview) %d points -> AUTO mode",
                      self.serial, len(points))
        # These points ARE the daytime layer (the editor edits the day layer, not
        # the composed curves). Capture it clean, then write day + night light so
        # editing the day schedule keeps the night light instead of wiping it.
        day = build_curves_from_points(points, self.state.roads_real)
        self.day_curves = [list(c) for c in day]
        self._persist_day()
        rows = [curve_to_csv(c) for c in self._compose(day)]
        return self.apply_schedule(road_data=rows, mode="1")

    def restore_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Write a previously saved configuration back to the light (Discard)."""
        from .draft import normalise_snapshot
        from .schedule import hhmm_dotted

        if not self.has_state:
            _LOGGER.warning("restore skipped for %s: no state read yet", self.serial)
            return False
        norm = normalise_snapshot(snapshot)
        _LOGGER.debug("WRITE %s restore_snapshot (discard) -> mode=%s",
                      self.serial, norm["mode"] or "unchanged")
        rows = [",".join(str(v) for v in row) for row in norm["road_data"]]
        ok = self.apply_schedule(
            road_data=rows or None, mode=norm["mode"] or None
        )
        moon = norm.get("moon") or {}
        if moon and "start" in moon and "end" in moon:
            # Already literal HH.MM in the snapshot; hhmm_dotted is a no-op on
            # floats but keeps the one true conversion point in schedule.py.
            self.set_moon(
                color_hex=moon.get("color", "00A0E9"),
                level_pct=int(moon.get("level", 0)),
                start_hhmm=hhmm_dotted(moon["start"]),
                end_hhmm=hhmm_dotted(moon["end"]),
                enable=bool(moon.get("run", False)),
            )
        return ok

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

        color_hex like '#00A0E9'; level_pct 0-100; start/end as literal HH.MM
        (6.30 == 06:30), i.e. the output of schedule.hhmm_dotted - NOT decimal
        hours. Passing 6.5 here would set 06:50 on the device.
        """
        color = "0x" + color_hex.lstrip("#").upper()
        level_255 = round(max(0, min(100, level_pct)) * 255 / 100)
        _LOGGER.debug("WRITE %s set_moon %s-%s level=%d enable=%s",
                      self.serial, start_hhmm, end_hhmm, level_pct, enable)
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
