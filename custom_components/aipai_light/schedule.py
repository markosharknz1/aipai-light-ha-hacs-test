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


def build_curve_from_points(points: list[dict], channel: int) -> list[int]:
    """Build one channel's 24 hourly points from time points.

    A time point is ``{"hour": 7, "levels": [..one per channel..]}``. The light
    is dark before the first point and after the last, and fades linearly
    between them - so a 0% point at 07:00 next to a 100% point at 09:00 *is* a
    two-hour sunrise. There is no separate ramp concept.

    Points are keyed by whole hours because that is all the device can store.
    """
    pts = sorted(
        (p for p in points if _levels_of(p, channel) is not None),
        key=lambda p: _safe_int(p.get("hour")),
    )
    if not pts:
        return [0] * POINTS
    first, last = _safe_int(pts[0]["hour"]), _safe_int(pts[-1]["hour"])

    curve: list[int] = []
    for h in range(POINTS):
        if h < first or h > last:
            curve.append(0)
            continue
        value = 0.0
        for a, b in zip(pts, pts[1:]):
            ah, bh = _safe_int(a["hour"]), _safe_int(b["hour"])
            if ah <= h <= bh:
                av = _levels_of(a, channel)
                bv = _levels_of(b, channel)
                # Two points on the same hour shouldn't happen (the UI prevents
                # it), but take the brighter rather than dividing by zero.
                value = max(av, bv) if bh == ah else av + (bv - av) * (h - ah) / (bh - ah)
                break
        else:
            # Single point, or h exactly on the last one.
            value = _levels_of(pts[-1], channel)
        curve.append(_clamp_level(round(value)))
    return curve


def build_curves_from_points(points: list[dict], roads: int) -> list[list[int]]:
    """Curves for every channel, in firmware road order."""
    return [build_curve_from_points(points, i) for i in range(roads)]


def _levels_of(point: dict, channel: int) -> float | None:
    """This point's level for `channel`, or None if it doesn't cover it."""
    levels = point.get("levels")
    if not isinstance(levels, (list, tuple)) or channel >= len(levels):
        return None
    return float(_clamp_level(_safe_int(levels[channel])))


def validate_curve(curve: list) -> list[int]:
    """Coerce a raw curve (e.g. from drag-editing) to 24 ints in 0-100."""
    values = [_clamp_level(_safe_int(v)) for v in curve]
    if len(values) < POINTS:
        values += [0] * (POINTS - len(values))
    return values[:POINTS]


def curve_to_csv(curve: list[int]) -> str:
    return ",".join(str(v) for v in curve)


def clock_epoch(now: float, ha_offset_seconds: float, dev_offset_hours: int) -> int:
    """Epoch to send so the device shows Home Assistant's local time.

    The device treats the epoch as UTC and adds its stored, WHOLE-HOUR timezone
    to display local time. Half-hour zones (e.g. Adelaide, UTC+9:30) can't be
    stored in whole hours, so we bake the difference into the epoch: the device's
    displayed time is ``epoch + dev_offset``, and we want that to equal
    ``now + ha_offset`` - hence ``epoch = now + ha_offset - dev_offset``. Works
    for any device tz value and any real offset (including :30 and DST).
    """
    return int(now + ha_offset_seconds - int(dev_offset_hours) * 3600)


def clock_needs_resync(
    current_offset: float | None,
    last_offset: float | None,
    seconds_since_sync: float,
    resync_interval: float,
) -> bool:
    """Whether to re-send the device clock now.

    True if the UTC offset has changed since the last sync (a DST changeover, or
    the half-hour Adelaide flip - correct it within the hour) or a periodic
    re-sync is due (drift). A first-ever sync (last_offset is None) isn't treated
    as a "change" - only the periodic path fires it.
    """
    changed = last_offset is not None and current_offset != last_offset
    return bool(changed or seconds_since_sync >= resync_interval)


def night_hours(start_hour: int, end_hour: int) -> list[int]:
    """Whole hours a night window covers, inclusive of start, exclusive of end.

    Wraps past midnight: night_hours(19, 7) -> 19,20,21,22,23,0,1,2,3,4,5,6.
    start == end means the whole day.
    """
    s, e = int(start_hour) % 24, int(end_hour) % 24
    if s == e:
        return list(range(24))
    if s < e:
        return list(range(s, e))
    return list(range(s, 24)) + list(range(0, e))


def overlay_night(
    curves: list[list[int]],
    start_hour: int,
    end_hour: int,
    channels: list[int],
    level: int,
    enable: bool,
) -> list[list[int]]:
    """Lay a simulated moonlight over existing 24h curves.

    The night window (which may wrap midnight) is **fully replaced**: the chosen
    channels are set to `level`, and every other channel in that window is set to
    0. Hours outside the window are untouched. So each apply is authoritative -
    re-applying with a different level, different channels, or Off cleanly
    replaces the last one instead of stacking on top of it. This is how moonlight
    is done for models with no native moon timer - it's just the ordinary
    schedule, so it works on every light.

    (The window is meant for night hours, where the day schedule is already dark,
    so replacing rather than max-ing doesn't clip a daytime ramp in practice.)
    """
    hours = set(night_hours(start_hour, end_hour))
    lvl = _clamp_level(_safe_int(level))
    lit = set(channels) if enable else set()
    out = [validate_curve(row) for row in curves]
    for c in range(len(out)):
        for h in hours:
            out[c][h] = lvl if c in lit else 0
    return out


def _clamp_level(v: int) -> int:
    return max(0, min(LEVEL_MAX, int(v)))


def _safe_int(v) -> int:  # noqa: ANN001
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0
