"""The MQTT bus: low-bandwidth telemetry and commands.

Manuscript §3.3.2.1 and §3.4.5 specify a dual-protocol split — MQTT for
telemetry and detection flags, local HTTP REST for image payloads. This module
is the MQTT half. Images never travel over it: a 50 KB JPEG at inference rate
would be an abuse of a protocol designed for small messages, and the shared
memory transport in ``frames.py`` already does that job better.

A note for Chapter III, because a reviewer will ask. The benefits the manuscript
cites for MQTT — resilience to intermittent connectivity, minimal packet
overhead — are properties of constrained *network* links, and over loopback they
are close to nil. The choice is still right, but on different grounds: it
decouples the worker from the web process so either can restart without the
other noticing, it fans out one publication to any number of subscribers, its
Last Will and Testament gives honest liveness reporting for free, and it leaves
room for additional sensor nodes to join later without redesign. Those are the
reasons worth writing down; "low packet overhead on localhost" is not.

Every operation degrades rather than raises. A missing broker must never stop
the worker from detecting aphids or the dashboard from serving history.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


class Topics:
    """Topic names, in one place so publisher and subscriber cannot drift."""

    ROOT = "pepperwatch"

    # High rate, lossy, QoS 0: a dropped sample is harmless and an ack per
    # message would be pure overhead at one per second.
    TELEMETRY_SYSTEM = f"{ROOT}/telemetry/system"
    TELEMETRY_PIPELINE = f"{ROOT}/telemetry/pipeline"
    DETECT_FRAME = f"{ROOT}/detect/frame"

    # State and commands, QoS 1: these must arrive.
    DETECT_FLAG = f"{ROOT}/detect/flag"
    EVENT_PERSISTED = f"{ROOT}/event/persisted"
    CMD_CAPTURE = f"{ROOT}/cmd/capture"
    CMD_INFERENCE = f"{ROOT}/cmd/inference"
    CMD_CONFIG = f"{ROOT}/cmd/config"

    # Retained, plus the Last Will: a dashboard that connects late immediately
    # learns whether the detector is alive, and learns within the keepalive
    # window if it dies.
    STATUS_WORKER = f"{ROOT}/status/worker"
    # Retained so a dashboard that connects at any time learns immediately which
    # model is running and whether its output is synthetic. Without this the
    # health endpoint cannot report provenance, and "which model produced this
    # result" is exactly the question Chapter IV has to answer.
    STATUS_DETECTOR = f"{ROOT}/status/detector"

    ALL = f"{ROOT}/#"
    COMMANDS = f"{ROOT}/cmd/#"

    # Everything the dashboard needs, and nothing it does not.
    DASHBOARD = (
        (TELEMETRY_SYSTEM, 0),
        (TELEMETRY_PIPELINE, 0),
        (DETECT_FRAME, 0),
        (DETECT_FLAG, 1),
        (EVENT_PERSISTED, 1),
        (STATUS_WORKER, 1),
        (STATUS_DETECTOR, 1),
    )


STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"


class MqttBus:
    """A thin, fail-soft wrapper over paho's client."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 1883,
        keepalive: int = 30,
        client_id: str = "pepperwatch",
        will_topic: str | None = None,
        will_payload: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._client_id = client_id
        self._connected = threading.Event()
        self._handlers: dict[str, list[Callable[[str, dict[str, Any]], None]]] = {}
        self._subscriptions: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self._started = False

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Registered before connecting, so the broker records it during CONNECT.
        if will_topic:
            self._client.will_set(will_topic, will_payload or STATUS_OFFLINE, qos=1, retain=True)

        # Cap the backoff: a solar device may sit with the broker down for a
        # while, and an unbounded delay would leave it silent long after
        # recovery.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, timeout: float = 3.0) -> bool:
        """Connect and run the network loop on its own thread.

        Returns whether the connection came up within ``timeout``. ``False`` is
        not fatal: paho keeps retrying in the background and publishes made in
        the meantime are dropped with a warning, not an exception.
        """
        with self._lock:
            if self._started:
                return self._connected.is_set()
            self._started = True

        try:
            self._client.connect_async(self._host, self._port, self._keepalive)
            self._client.loop_start()
        except Exception as exc:  # noqa: BLE001 — never let the bus kill the caller
            log.warning("mqtt connect to %s:%s failed: %s", self._host, self._port, exc)
            return False

        if not self._connected.wait(timeout=timeout):
            log.warning(
                "mqtt broker %s:%s not reachable yet — continuing and retrying in the background",
                self._host, self._port,
            )
            return False
        return True

    def stop(self, *, final_status: tuple[str, str] | None = None) -> None:
        """Publish a closing status, then disconnect cleanly.

        A clean disconnect suppresses the Last Will, so an intentional shutdown
        is reported as ``offline`` deliberately rather than looking like a crash.
        """
        if final_status and self._connected.is_set():
            topic, payload = final_status
            self.publish_raw(topic, payload, qos=1, retain=True)
            self._client.loop_write()

        try:
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        self._connected.clear()
        self._started = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- publishing --------------------------------------------------------

    def publish(self, topic: str, payload: dict[str, Any], *, qos: int = 0, retain: bool = False) -> bool:
        """Publish a JSON payload. Returns whether it was handed to the client."""
        return self.publish_raw(topic, json.dumps(payload, separators=(",", ":")), qos=qos, retain=retain)

    def publish_raw(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> bool:
        if not self._connected.is_set():
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as exc:  # noqa: BLE001
            log.debug("publish to %s failed: %s", topic, exc)
            return False

    # -- subscribing -------------------------------------------------------

    def subscribe(
        self, topic: str, handler: Callable[[str, dict[str, Any]], None], *, qos: int = 0
    ) -> None:
        """Register a handler for a topic filter.

        Subscriptions are remembered and replayed on every reconnect, so a
        broker restart does not silently leave the dashboard deaf.
        """
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)
            if (topic, qos) not in self._subscriptions:
                self._subscriptions.append((topic, qos))
        if self._connected.is_set():
            self._client.subscribe(topic, qos=qos)

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.warning("mqtt connect refused: %s", reason_code)
            return
        self._connected.set()
        log.info("mqtt connected to %s:%s as %s", self._host, self._port, self._client_id)
        with self._lock:
            subscriptions = list(self._subscriptions)
        for topic, qos in subscriptions:
            client.subscribe(topic, qos=qos)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected.clear()
        if reason_code:
            log.warning("mqtt disconnected (%s) — paho will retry", reason_code)

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        text = message.payload.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except json.JSONDecodeError:
            # Status topics carry bare strings, not JSON.
            payload = {"value": text}

        with self._lock:
            handlers = [
                handler
                for filter_, registered in self._handlers.items()
                if mqtt.topic_matches_sub(filter_, message.topic)
                for handler in registered
            ]

        for handler in handlers:
            try:
                handler(message.topic, payload)
            except Exception:  # noqa: BLE001 — one bad handler must not kill the loop
                log.exception("mqtt handler for %s raised", message.topic)


def make_bus(cfg, role: str, *, with_will: bool = False) -> MqttBus:
    """Build a bus for ``role`` (``worker`` or ``web``).

    Client ids include the pid so a restarting process cannot be kicked off the
    broker by its own stale session.
    """
    import os

    return MqttBus(
        host=cfg.mqtt.host,
        port=cfg.mqtt.port,
        keepalive=cfg.mqtt.keepalive,
        client_id=f"{cfg.mqtt.client_id_prefix}-{role}-{os.getpid()}",
        will_topic=Topics.STATUS_WORKER if with_will else None,
        will_payload=STATUS_OFFLINE if with_will else None,
    )
