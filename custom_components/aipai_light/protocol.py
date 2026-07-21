"""Parse and build the AIPAI/doseen light wire protocol.

Field map (verified against the app's decrypted ctrl-light.html parser and
live captured traffic). With `n` = firmware road count (6 or 8),
`d1 = n - 6`, `d2 = 2 * (n - 6)`:

    [0]              power ("on"/"off")
    [1]              mode ("0" manual / "1" auto sunrise-sunset)
    [2] [3] [4]      TempOn / TempOff / TempOut (fan thresholds)
    [5 .. 5+n-1]     roadVal: current manual level per channel (0..100)
    [11+d1 .. +n-1]  roadData: 24-hour schedule curve per channel (CSV, 0..100)
    [17+d2]          temperature (float, deg C)
    [18+d2]          device clock "HH,MM"
    [19+d2]          auto-on hour
    [20+d2]          auto-off hour
    [21+d2]          serial number
    [22+d2]          knob-intensity flag
    [23+d2]          timezone (UTC offset)
    [24+d2]          model string (e.g. "A8SE8")

The firmware always reports 6 or 8 roads. Some models (A46-P, A7-P4) are
physically 4-channel but still report 6 firmware roads with the extras
zeroed; the model table decides how many to actually surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .const import CHANNEL_CMD_MAX, CHANNEL_PCT_MAX, resolve_model

# saveconfig hardcodes these fan thresholds (from public.js DevicesSave).
_SAVE_TEMP_ON = 35
_SAVE_TEMP_OFF = 30
_SAVE_TEMP_OUT = 80

_ZERO_ROW = ",".join(["0"] * 24)


@dataclass
class LightState:
    power: str = "off"
    mode: str = "1"
    roads_real: int = 8          # firmware road count (6 or 8), drives indexing
    roads: int = 8               # channels actually surfaced (4/6/8)
    labels: list[str] = field(default_factory=list)
    road_val_pct: list[int] = field(default_factory=list)   # len roads_real, 0..100
    road_data: list[str] = field(default_factory=list)       # len roads_real, CSV rows
    temperature: float | None = None
    clock: str | None = None
    open_hour: int = 0
    close_hour: int = 0
    timezone: str = "0"
    serial: str | None = None
    model: str | None = None

    def channel_cmd_values(self) -> list[int]:
        """Surfaced channels as 0..1023 command-scale values."""
        out = []
        for m in range(self.roads):
            pct = self.road_val_pct[m] if m < len(self.road_val_pct) else 0
            out.append(pct_to_cmd(pct))
        return out


def parse_readconfig(msg: str) -> LightState:
    parts = [p.strip() for p in msg.split("|")]
    n = 8 if len(parts) > 28 else 6
    d1 = n - 6
    d2 = 2 * (n - 6)

    def get(idx: int, default: str = "") -> str:
        return parts[idx] if 0 <= idx < len(parts) else default

    road_val = [_safe_int(get(5 + m)) for m in range(n)]
    road_data = [get(11 + d1 + m, _ZERO_ROW) for m in range(n)]
    model = get(24 + d2)

    roads, labels = resolve_model(model, n)

    return LightState(
        power=get(0, "off"),
        mode=get(1, "1"),
        roads_real=n,
        roads=roads,
        labels=labels,
        road_val_pct=road_val,
        road_data=road_data,
        temperature=_safe_float(get(17 + d2)),
        clock=get(18 + d2) or None,
        open_hour=_safe_int(get(19 + d2)),
        close_hour=_safe_int(get(20 + d2)),
        timezone=get(23 + d2, "0"),
        serial=get(21 + d2) or None,
        model=model or None,
    )


def build_saveconfig(state: LightState) -> str:
    """Rebuild a saveconfig `msg` string from a LightState.

    Mirrors public.js DevicesSave exactly, including its forced "on" power
    field (a saveconfig always powers the light on - it is not an off path)
    and hardcoded fan thresholds. Preserves the existing schedule
    (`road_data`) verbatim so a persist never disturbs sunrise/sunset.
    """
    n = state.roads_real
    road_val = list(state.road_val_pct) + [0] * (n - len(state.road_val_pct))
    road_data = list(state.road_data) + [_ZERO_ROW] * (n - len(state.road_data))

    tokens: list[str] = ["on", state.mode, str(_SAVE_TEMP_ON), str(_SAVE_TEMP_OFF), str(_SAVE_TEMP_OUT)]
    tokens += [str(_clamp_pct(v)) for v in road_val[:n]]
    tokens += [row for row in road_data[:n]]
    tokens += [str(int(state.open_hour)), str(int(state.close_hour)), str(state.timezone)]

    # Top-level delimiter is 'x'; commas inside schedule rows become 'y'.
    return "x".join(tokens).replace(",", "y")


def pct_to_cmd(pct: int) -> int:
    pct = _clamp_pct(pct)
    return round(pct / CHANNEL_PCT_MAX * CHANNEL_CMD_MAX)


def cmd_to_pct(cmd: int) -> int:
    cmd = max(0, min(CHANNEL_CMD_MAX, int(cmd)))
    return round(cmd / CHANNEL_CMD_MAX * CHANNEL_PCT_MAX)


def _clamp_pct(v: int) -> int:
    return max(0, min(CHANNEL_PCT_MAX, int(v)))


def _safe_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
