"""Schedule curve engine for AIPAI lights.

The device stores, per channel, a 24-point hourly curve (values 0-100, one
per hour 0..23) and ramps smoothly between the points. This module turns
human-friendly "periods" (a channel is at PEAK between start and end, with a
ramp-up before and ramp-down after - i.e. sunrise/sunset) into those 24
hourly points, and validates raw curves coming from the visual designer.

Times are decimal hours (13.5 == 13:30). Periods may wrap past midnight
(end < start), which is split across the day boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

POINTS = 24
LEVEL_MAX = 100


@dataclass
class Period:
    """A lit period for one channel, with sunrise/sunset ramps."""
    start: float          # decimal hour the channel reaches `level`
    end: float            # decimal hour the channel starts ramping down
    level: float          # peak level 0-100
    ramp_up: float = 1.0  # hours before `start` spent ramping 0 -> level
    ramp_down: float = 1.0  # hours after `end` spent ramping level -> 0


def parse_hhmm(value: str | float) -> float:
    """'13:30' -> 13.5 ; passes through numbers unchanged."""
    if isinstance(value, (int, float)):
        return float(value)
    value = value.strip()
    if ":" in value:
        h, m = value.split(":", 1)
        return int(h) + int(m) / 60.0
    return float(value)


def hhmm_dotted(value: str | float) -> float:
    """'6:30' -> 6.30 (literal HH.MM, the device's moon time format).

    This is NOT decimal hours - the firmware reads 6.30 as 6h30m. Using
    parse_hhmm (6.5) here would mis-set any non-zero minutes.
    """
    s = str(value).strip()
    if ":" in s:
        h, m = s.split(":", 1)
        return round(int(h) + int(m) / 100, 2)
    return round(float(s), 2)


def _period_value_at(p: Period, hour: float) -> float:
    """Piecewise-linear value of one period at `hour` on a 0..24 line (no wrap)."""
    up_start = p.start - p.ramp_up
    down_end = p.end + p.ramp_down
    # Hold region first so zero-length ramps at the day boundary resolve to peak.
    if p.start <= hour <= p.end:
        return p.level
    if up_start <= hour < p.start and p.ramp_up > 0:  # ramping up
        return p.level * (hour - up_start) / p.ramp_up
    if p.end < hour <= down_end and p.ramp_down > 0:  # ramping down
        return p.level * (down_end - hour) / p.ramp_down
    return 0.0


def _expand_wrapping(p: Period) -> list[Period]:
    """Split a period that wraps past midnight into same-day pieces."""
    if p.end >= p.start:
        return [p]
    # e.g. start 22:00 end 02:00 -> [22..24] and [0..02] (ramps kept on outer edges)
    first = Period(p.start, POINTS, p.level, p.ramp_up, 0.0)
    second = Period(0.0, p.end, p.level, 0.0, p.ramp_down)
    return [first, second]


def build_channel_curve(periods: list[Period]) -> list[int]:
    """Build the 24 hourly integer points (0-100) for one channel."""
    expanded: list[Period] = []
    for p in periods:
        expanded.extend(_expand_wrapping(p))
    curve = []
    for h in range(POINTS):
        value = max((_period_value_at(p, float(h)) for p in expanded), default=0.0)
        curve.append(_clamp_level(round(value)))
    return curve


def build_schedule(channel_periods: list[list[Period]], roads: int) -> list[list[int]]:
    """Build 24-point curves for `roads` channels from their period lists."""
    out: list[list[int]] = []
    for i in range(roads):
        periods = channel_periods[i] if i < len(channel_periods) else []
        out.append(build_channel_curve(periods))
    return out


def validate_curve(curve: list) -> list[int]:
    """Coerce a raw curve (e.g. from drag-editing) to 24 ints in 0-100."""
    values = [_clamp_level(_safe_int(v)) for v in curve]
    if len(values) < POINTS:
        values += [0] * (POINTS - len(values))
    return values[:POINTS]


def curve_to_csv(curve: list[int]) -> str:
    return ",".join(str(v) for v in curve)


def _clamp_level(v: int) -> int:
    return max(0, min(LEVEL_MAX, int(v)))


def _safe_int(v) -> int:  # noqa: ANN001
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0
