"""MQTT client for the 'gen-2' AIPAI device family (pumps, plug, chiller, ...).

EXPERIMENTAL / UNVERIFIED. Extracted from the decrypted app (public.js
`mqttConnect`/`mqttPost`) but not tested against real hardware. Contributors
with the actual devices: this is the layer most likely to need correcting.

Scheme (differs from the light scheme):
  * connect as clientId `m-<random hex>`
  * subscribe to `mob/<random hex>`   (device replies here)
  * publish commands to `dev/<serial>`, each JSON carrying a `clientId`
    field so the device knows which `mob/...` topic to answer on
  * `{"type": "readCfg"}` asks the device to report its state
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .const import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME, MQTT_WS_PATH

_LOGGER = logging.getLogger(__name__)


class AipaiGen2Client:
    def __init__(
        self,
        serial: str,
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._serial = serial
        self._on_message = on_message
        self._on_connection_change = on_connection_change

        self._app_id = secrets.token_hex(4)  # matches the app's 8-hex-char id
        self._pub_topic = f"dev/{serial}"
        self._sub_topic = f"mob/{self._app_id}"

        self._client = mqtt.Client(client_id=f"m-{self._app_id}", transport="websockets")
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
            client.subscribe(self._sub_topic)
        else:
            _LOGGER.warning("gen2 connect failed for %s: rc=%s", self._serial, rc)
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

    def send(self, command: dict[str, Any]) -> None:
        """Publish a command dict to the device (clientId injected)."""
        payload = dict(command)
        payload["clientId"] = self._app_id
        self._client.publish(self._pub_topic, json.dumps(payload))

    def request_state(self) -> None:
        self.send({"type": "readCfg"})
