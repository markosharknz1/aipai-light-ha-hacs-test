"""Preview-vs-saved comparison (pure logic, no Home Assistant imports).

Edits are written to the fixture straight away so you can judge them in the
water, but the last *saved* configuration is kept separately. "Unsaved changes"
is simply: does the light's current config differ from that saved copy?

Comparison is done on a normalised form so that cosmetic differences - a curve
arriving as "0,0,50" instead of [0, 0, 50], an int where a str was stored,
None vs absent - never show up as a phantom change. A false "unsaved" that
never clears is worse than useless: people stop trusting the indicator.
"""
from __future__ import annotations

from typing import Any

# Keys that make up a saved configuration. Anything outside this list (live
# channel values, temperature, RSSI) is device state, not settings, and must
# not count as an unsaved change.
SNAPSHOT_KEYS = ("road_data", "mode", "moon")


def normalise_curve(curve: Any) -> list[int]:
    """A curve as a list of 24 ints, however it arrived."""
    if isinstance(curve, str):
        # Keep empty fields: these are positional (one per hour), so dropping a
        # blank would shift every later hour earlier.
        parts = curve.split(",") if curve else []
    elif isinstance(curve, (list, tuple)):
        parts = list(curve)
    else:
        return [0] * 24
    values = []
    for p in parts:
        try:
            values.append(max(0, min(100, int(round(float(p))))))
        except (TypeError, ValueError):
            values.append(0)
    return (values + [0] * 24)[:24]


def normalise_moon(moon: Any) -> dict[str, Any]:
    """Moon settings in a stable shape, with times as literal HH.MM floats."""
    if not isinstance(moon, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("start", "end", "level"):
        if key in moon and moon[key] is not None:
            try:
                out[key] = round(float(moon[key]), 2)
            except (TypeError, ValueError):
                pass
    if "color" in moon and moon["color"] is not None:
        out["color"] = str(moon["color"]).upper().lstrip("#").replace("0X", "")
    # "run"/"enabled" have both spellings in the wild; settle on one.
    for key in ("run", "enabled", "enable"):
        if key in moon:
            out["run"] = bool(moon[key])
            break
    return out


def normalise_snapshot(snapshot: Any) -> dict[str, Any]:
    """Canonical form of a saved configuration, safe to compare or store."""
    if not isinstance(snapshot, dict):
        return {"road_data": [], "mode": "", "moon": {}}
    rows = snapshot.get("road_data") or []
    if isinstance(rows, str):          # a single row handed in unwrapped
        rows = [rows]
    return {
        "road_data": [normalise_curve(r) for r in rows],
        "mode": str(snapshot.get("mode", "")),
        "moon": normalise_moon(snapshot.get("moon")),
    }


def snapshots_differ(a: Any, b: Any) -> bool:
    """True when these two configurations would light the tank differently."""
    return normalise_snapshot(a) != normalise_snapshot(b)


def snapshot_to_storage(snapshot: Any) -> dict[str, Any]:
    """Normalised form with curves back as CSV, which is what the device wants."""
    norm = normalise_snapshot(snapshot)
    return {
        "road_data": [",".join(str(v) for v in row) for row in norm["road_data"]],
        "mode": norm["mode"],
        "moon": norm["moon"],
    }
