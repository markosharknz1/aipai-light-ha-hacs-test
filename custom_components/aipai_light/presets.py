"""Named lighting presets — one-tap looks and day schedules.

Presets store levels **by channel label** (White / Blue / UV / ...), not by
index, so the same preset applies sensibly to a 6-channel A7 and an 8-channel
A8 alike; labels a fixture doesn't have are simply ignored.

Two kinds:

* ``levels``   — an instant look. Sets the channels now and switches the light
                 to manual mode so the stored day schedule doesn't override it.
* ``schedule`` — a full day. Builds a 24-point curve per channel from a peak
                 level, a window and a ramp, and switches to scheduled mode.

There are **no built-in presets**. Every tank is different - livestock, depth,
fixture height, what the owner is growing - so a canned "Bright day (SPS)" is a
guess dressed up as advice. The slots start empty and you save your own from a
look you have actually dialled in and seen in the water. Configs can be shared
between keepers via export/import (see share.py).
"""
from __future__ import annotations

from typing import Any

from .schedule import Period, build_channel_curve

# Relative spectral weighting used when generating schedule presets, so a
# "peak" of 90 means 90 % blue but a gentler 63 % white.
_WEIGHTS = (
    (("blue",), 1.00),
    (("uv", "violet", "purple"), 0.85),
    (("white", "lb", "cool"), 0.70),
    (("warm", "orange"), 0.55),
)
_DEFAULT_WEIGHT = 0.45  # red / green / olive


def channel_weight(label: str) -> float:
    name = (label or "").lower()
    for keys, weight in _WEIGHTS:
        if any(k in name for k in keys):
            return weight
    return _DEFAULT_WEIGHT


# Intentionally empty - see the module docstring. Kept as a name so the merge
# path, the store and the select entity don't need special-casing for "none".
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {}

SLOT_COUNT = 3   # the card offers three save slots; blank until you fill them


def build_levels(preset: dict[str, Any], labels: list[str]) -> list[int]:
    """Per-channel 0-100 levels for this fixture's labels."""
    wanted = {k.lower(): v for k, v in (preset.get("levels") or {}).items()}
    return [_clamp(wanted.get((lab or "").lower(), 0)) for lab in labels]


def build_curves(preset: dict[str, Any], labels: list[str]) -> list[list[int]]:
    """Per-channel 24-point curves for this fixture's labels."""
    peak = float(preset.get("peak", 60))
    start = float(preset.get("start", 9))
    end = float(preset.get("end", 17))
    ramp = float(preset.get("ramp", 2))
    curves = []
    for label in labels:
        level = _clamp(round(peak * channel_weight(label)))
        curves.append(build_channel_curve(
            [Period(start=start, end=end, level=level, ramp_up=ramp, ramp_down=ramp)]
        ))
    return curves


def levels_from_current(levels_pct: list[int], labels: list[str]) -> dict[str, int]:
    """Turn a fixture's current levels into a label-keyed preset body."""
    return {
        label: _clamp(levels_pct[i])
        for i, label in enumerate(labels)
        if i < len(levels_pct)
    }


def merge(builtin: dict[str, dict], custom: dict[str, dict]) -> dict[str, dict]:
    """Custom presets win over built-ins of the same name."""
    merged = dict(builtin)
    merged.update(custom or {})
    return merged


def _clamp(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0
