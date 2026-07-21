"""Thin wrapper around paho-mqtt for talking to the AIPAI/doseen cloud broker.

Connects over MQTT-over-WebSocket (plain, not TLS - matching the vendor app)
with credentials hardcoded identically into every install of the app, not
per-account.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .const import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME, MQTT_WS_PATH

_LOGGER = logging.getLogger(__name__)


class AipaiMqttClient:
    """One MQTT connection dedicated to a single light (mirrors the app)."""

    def __init__(
        self,
        serial: str,
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._serial = serial
        self._on_message = on_message
        self._on_connection_change = on_connection_change

        client_id = f"A8SE8-{serial}-MOB"
        self._client = mqtt.Client(client_id=client_id, transport="websockets")
        self._client.ws_set_options(path=MQTT_WS_PATH)
        self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    def connect(self) -> None:
        self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _handle_connect(self, client, userdata, flags, rc) -> None:  # noqa: ANN001
        connected = rc == 0
        if connected:
            client.subscribe(f"light/{self._serial}/dev")
        else:
            _LOGGER.warning("AIPAI broker connect failed for %s: rc=%s", self._serial, rc)
        if self._on_connection_change:
            self._on_connection_change(connected)

    def _handle_disconnect(self, client, userdata, rc) -> None:  # noqa: ANN001
        if self._on_connection_change:
            self._on_connection_change(False)

    def _handle_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        try:
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
        except ValueError:
            return
        self._on_message(payload)

    def _publish(self, msg_type: str, msg: str) -> None:
        topic = f"light/{self._serial}/mob"
        self._client.publish(topic, json.dumps({"type": msg_type, "msg": msg}))

    def request_state(self) -> None:
        self._publish("readconfig", "read=config")

    def set_channel(self, letter: str, value_cmd: int) -> None:
        """Live per-channel set, 0..1023. Instant, non-persistent."""
        value_cmd = max(0, min(1023, int(value_cmd)))
        self._publish(f"{letter}{value_cmd}", f"{letter}={value_cmd}")

    def save_config(self, save_msg: str) -> None:
        """Persist full state (levels + schedule). Always powers the light on."""
        self._publish("saveconfig", save_msg)

    def sync_clock(self, epoch: int | None = None) -> None:
        """Set the device clock. `epoch` is UTC seconds; defaults to now."""
        ts = int(time.time()) if epoch is None else int(epoch)
        self._publish("clock", f"clock={ts}")

    def set_moon(
        self,
        color_hex: str,
        level_255: int,
        start_hhmm: float,
        end_hhmm: float,
        run: bool,
        save: bool = True,
    ) -> None:
        """Publish a moonSet command (moonSave 1=persist, 0=live preview)."""
        payload = {
            "type": "moonSet",
            "moonSave": 1 if save else 0,
            "moonColor": color_hex,
            "moonLevel": max(0, min(255, int(level_255))),
            "moonStart": round(float(start_hhmm), 2),
            "moonEnd": round(float(end_hhmm), 2),
            "moonRun": 1 if run else 0,
        }
        topic = f"light/{self._serial}/mob"
        self._client.publish(topic, json.dumps(payload))

    def restart(self) -> None:
        self._publish("noderestart", "node=restart")
