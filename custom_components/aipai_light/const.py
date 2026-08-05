"""Constants and per-model channel tables for the AIPAI Aquarium Light integration.

Reverse-engineered from the AIPAI Android app (Doseen OEM). Model/channel
tables and the wire format below were extracted from the app's decrypted
`ctrl-light.html` / `public.js`, and verified against live captured traffic
for the A8-SE8.

Key facts about the protocol:

* Every light in the range uses ONE positional channel-code array,
  `ROAD_CODES`. A light with N channels uses the first N codes. The physical
  color a code drives varies by model (that's what MODEL_TABLE labels are
  for), but the wire code is always positional.
* The live per-channel command uses a 0..1023 scale
  (`{"type":"w512","msg":"w=512"}`); stored/preset values use a 0..100 scale.
"""

DOMAIN = "aipai_light"

CONF_SERIAL = "serial"
CONF_MODEL = "model"
CONF_DEVICE_TYPE = "device_type"
CONF_POLL_INTERVAL = "poll_interval"
CONF_NAME = "name"
CONF_SUBNET = "subnet"

DEFAULT_POLL_INTERVAL = 45  # seconds

# Verified device type (has real hardware behind it).
DEVICE_TYPE_LIGHT = "light"

MQTT_HOST = "mqtt.doseen.com"
MQTT_PORT = 8083
MQTT_WS_PATH = "/mqtt"
MQTT_USERNAME = "aplus"
MQTT_PASSWORD = "19491001"

# Positional per-channel command codes (channel 1..8). A model with N roads
# uses ROAD_CODES[:N].
ROAD_CODES = ["w", "b", "r", "g", "b2", "p", "uv", "wm"]

# Live per-channel command value range (10-bit PWM: w0..w1023).
CHANNEL_CMD_MAX = 1023
# Stored/preset per-channel range (readconfig roadVal, saveconfig).
CHANNEL_PCT_MAX = 100

# Default saltwater labels (channel 1..8). Used by A8-SE8, A8-PRO, A7-S,
# A8-S*, A8-SE* — any model that doesn't override in MODEL_TABLE.
DEFAULT_LABELS = ["White", "Blue", "Red", "Green", "Blue2", "Purple", "UV", "Warm"]

# Normalized model string (uppercase, no dashes/spaces) -> (roads, labels).
# Mirrors the branching in the app's ctrl-light.html.
MODEL_TABLE: dict[str, tuple[int, list[str]]] = {
    # A7-S saltwater (6ch)
    "A7S2": (6, DEFAULT_LABELS[:6]),
    "A7S6": (6, DEFAULT_LABELS[:6]),
    # A8-S / A8-SE saltwater (6ch)
    "A8S2": (6, DEFAULT_LABELS[:6]),
    "A8S3": (6, DEFAULT_LABELS[:6]),
    "A8S5": (6, DEFAULT_LABELS[:6]),
    "A8S6": (6, DEFAULT_LABELS[:6]),
    "A8SE": (6, DEFAULT_LABELS[:6]),
    "A8SE3": (6, DEFAULT_LABELS[:6]),
    "A8SE5": (6, DEFAULT_LABELS[:6]),
    "A8SE6": (6, DEFAULT_LABELS[:6]),
    # 8-channel saltwater (A8-SE8, A8-PRO5/6)
    "A8PRO5": (8, DEFAULT_LABELS),
    "A8PRO6": (8, DEFAULT_LABELS),
    "A8SE8": (8, DEFAULT_LABELS),
    # A8 "Blue"/"Max" variants (8ch, different labels/order)
    "A8SEB": (8, ["Blue1", "Blue2", "Warm", "Olive", "Blue3", "Purple", "UV", "White"]),
    "A8PROB": (8, ["Blue1", "Blue2", "Warm", "Olive", "Blue3", "Purple", "UV", "White"]),
    # A8-HP (6ch)
    "A8HP": (6, ["White", "Blue", "Red", "UV", "Blue3", "Purple"]),
    "A8HP6": (6, ["White", "Blue", "Red", "UV", "Blue3", "Purple"]),
    # A46 freshwater (4ch)
    "A46P": (4, ["White", "Blue", "Red", "Green"]),
    # A8-X (6ch)
    "A8X": (6, ["White", "Royal Blue", "Broadband Blue", "Violet Blue", "Purple", "Crossover UV"]),
    "A8X5": (6, ["White", "Royal Blue", "Broadband Blue", "Violet Blue", "Purple", "Crossover UV"]),
    "A8X6": (6, ["White", "Royal Blue", "Broadband Blue", "Violet Blue", "Purple", "Crossover UV"]),
    # A7-P / A180 freshwater
    "A7P": (6, ["Warm", "Blue", "Red", "Green", "Orange Light", "Cool White"]),
    "A7P6": (6, ["Warm", "Blue", "Red", "Green", "Orange Light", "Cool White"]),
    "A180": (6, ["Warm", "Blue", "Red", "Green", "Orange Light", "Cool White"]),
    "A7P4": (4, ["Cool White", "Red", "Orange Light", "Warm"]),
}


# Models WITHOUT a moonlight feature. Moonlight is an A8-class feature; the
# vendor app itself refuses it ("not open") on the A7-S line. Verified: A8-SE8
# HAS moon, A7S6 does NOT. Anything not listed here is assumed moon-capable, so
# this only suppresses the control where we KNOW it does nothing - extend as
# other models are confirmed.
MODELS_WITHOUT_MOON = {"A7S2", "A7S6"}


def normalize_model(model: str) -> str:
    """Normalize a model string for MODEL_TABLE lookup (strip dashes/spaces, upper)."""
    return (model or "").upper().replace("-", "").replace(" ", "").strip()


def model_has_moon(model: str) -> bool:
    """Whether a model supports the native moonlight timer (best-effort)."""
    return normalize_model(model) not in MODELS_WITHOUT_MOON


def resolve_model(model: str, roads_from_length: int) -> tuple[int, list[str]]:
    """Resolve (roads, labels) for a model string.

    Falls back to the channel count derived from the readconfig length (the
    same signal the app trusts first) with default labels when the model is
    unknown, so a new firmware/model still yields a usable device.
    """
    key = normalize_model(model)
    if key in MODEL_TABLE:
        return MODEL_TABLE[key]
    roads = roads_from_length if roads_from_length in (4, 6, 8) else 8
    return roads, DEFAULT_LABELS[:roads] if roads <= len(DEFAULT_LABELS) else DEFAULT_LABELS
