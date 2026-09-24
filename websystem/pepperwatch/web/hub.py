"""MQTT-to-browser fan-out.

Browsers cannot speak MQTT, so something has to bridge the bus to the page.
This is that bridge, and its shape is dictated by the hardware: a Pi 5 serving
several phones in a field cannot afford a thread per subscriber, and it
absolutely cannot let one phone on the edge of Wi-Fi range slow the worker down.

So there is exactly **one** MQTT subscription for the whole process, held by
this hub, and each SSE client gets a small bounded queue. Publishing is a
non-blocking put into every queue; a queue that is full has its oldest item
discarded. A slow client therefore loses frames — the correct outcome for live
telemetry — instead of applying back-pressure up the chain to the detector.

Because there is one subscription per process, the web server **must** run a
single worker process. Two gunicorn workers would mean two hubs, two
subscriptions, and each browser seeing half the traffic depending on which
process it landed on. See the ``--workers 1`` note in the deploy units.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..mqtt_bus import STATUS_OFFLINE, STATUS_ONLINE, MqttBus, Topics, make_bus

log = logging.getLogger(__name__)


# eq=False keeps identity-based hashing, which is required because
# subscribers live in a set: two connections are distinct even if their
# queues happen to hold the same bytes. A plain @dataclass sets
# __hash__ = None and every SSE connection would fail to register.
@dataclass(eq=False)
class Subscriber:
    """One SSE connection's bounded mailbox."""

    queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=10))
    dropped: int = 0

    def offer(self, payload: str) -> None:
        """Enqueue, discarding the oldest if full. Never blocks."""
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass
            self.dropped += 1


class EventHub:
    """Subscribes once to MQTT and fans messages out to SSE clients."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._bus: MqttBus | None = None
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._queue_size = int(cfg.web.sse_queue_size)
        self._max_clients = int(cfg.web.max_sse_clients)
        self._started = False

        # Latest-value cache, so a page that loads between publications renders
        # immediately instead of showing empty tiles until the next tick.
        self._latest: dict[str, dict[str, Any]] = {}
        self._worker_status = "unknown"
        self._worker_seen_at = 0.0
        self._detector: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

        self._bus = make_bus(self.cfg, "web")
        self._bus.start(timeout=2.0)
        for topic, qos in Topics.DASHBOARD:
            self._bus.subscribe(topic, self._on_message, qos=qos)
        log.info("event hub subscribed to %d topics", len(Topics.DASHBOARD))

    def stop(self) -> None:
        if self._bus is not None:
            self._bus.stop()
            self._bus = None
        self._started = False

    @property
    def bus(self) -> MqttBus | None:
        return self._bus

    @property
    def connected(self) -> bool:
        return self._bus is not None and self._bus.connected

    # -- inbound -----------------------------------------------------------

    def _on_message(self, topic: str, payload: dict[str, Any]) -> None:
        short = topic.removeprefix(f"{Topics.ROOT}/")

        if topic == Topics.STATUS_WORKER:
            self._worker_status = str(payload.get("value", "unknown"))
            self._worker_seen_at = time.time()
        elif topic == Topics.STATUS_DETECTOR:
            self._detector = dict(payload)
        else:
            self._latest[short] = payload
            if "telemetry" in short or short == "detect/frame":
                self._worker_seen_at = time.time()

        self.publish(short, payload)

    # -- outbound ----------------------------------------------------------

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """Fan one event out to every subscriber."""
        frame = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(frame)

    def stream(self) -> Iterator[str]:
        """Generate an SSE response body for one client.

        Opens with the cached latest values so the page paints at once, then
        drains this client's queue, emitting a comment as a keepalive whenever
        the bus is quiet. The comment matters: without it an idle connection is
        indistinguishable from a dead one to a phone's power-saving NAT.
        """
        subscriber = Subscriber(queue=queue.Queue(maxsize=self._queue_size))

        with self._lock:
            if len(self._subscribers) >= self._max_clients:
                # Refusing is better than accepting and starving everyone.
                yield (
                    "event: rejected\n"
                    f'data: {{"reason":"too many clients","max":{self._max_clients}}}\n\n'
                )
                return
            self._subscribers.add(subscriber)

        keepalive = float(self.cfg.web.sse_keepalive_s)
        try:
            yield f"event: hello\ndata: {json.dumps(self.snapshot(), separators=(',', ':'))}\n\n"
            while True:
                try:
                    yield subscriber.queue.get(timeout=keepalive)
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            raise
        finally:
            with self._lock:
                self._subscribers.discard(subscriber)
            if subscriber.dropped:
                log.debug("sse client dropped %d messages", subscriber.dropped)

    # -- state -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything a newly connected page needs to render immediately."""
        return {
            "worker": self.worker_state(),
            "detector": dict(self._detector),
            "latest": dict(self._latest),
            "mqtt_connected": self.connected,
        }

    def worker_state(self) -> dict[str, Any]:
        """Honest liveness, from the retained status plus a staleness check.

        The retained MQTT status says what the worker last announced, but a
        worker killed with SIGKILL announces nothing and the broker's Last Will
        can take a keepalive interval to fire. Treating silence as offline means
        the dashboard tells the truth within seconds either way.
        """
        age = time.time() - self._worker_seen_at if self._worker_seen_at else None
        stale_after = max(self.cfg.persist.telemetry_interval_s * 2.0, 10.0)
        alive = self._worker_status == STATUS_ONLINE and age is not None and age < stale_after

        return {
            "status": STATUS_ONLINE if alive else (self._worker_status or STATUS_OFFLINE),
            "alive": alive,
            "announced": self._worker_status,
            "last_seen_s": round(age, 1) if age is not None else None,
            "stale_after_s": stale_after,
        }

    @property
    def detector(self) -> dict[str, Any]:
        """What the worker last announced about its model. Empty if none has."""
        return dict(self._detector)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
