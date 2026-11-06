"""The worker end to end, with two cameras: real MQTT broker, database, frame
segments and session CSVs; only the cameras are fakes.

Needs a running broker, so it is skipped unless PW_TEST_MQTT_PORT is set. CI's
integration job installs mosquitto; locally, `mosquitto -p 18830` and then
`PW_TEST_MQTT_PORT=18830 pytest tests/test_worker_integration.py`.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import threading
import time
import uuid

import numpy as np
import pytest

from pepperwatch.config import load
from pepperwatch.frames import make_reader
from pepperwatch.hal.base import CameraInfo, CameraSource, Frame
from pepperwatch.hal.discovery import CameraCandidate

PORT = os.environ.get("PW_TEST_MQTT_PORT")
pytestmark = [
    pytest.mark.skipif(not PORT, reason="needs an MQTT broker: set PW_TEST_MQTT_PORT"),
    pytest.mark.timeout(120),
]


class FakeCamera(CameraSource):
    """Ten frames a second of one solid colour."""

    def __init__(self, colour: tuple[int, int, int], device: str) -> None:
        self.image = np.full((480, 640, 3), colour, np.uint8)
        self.seq, self.last = 0, 0.0
        self._info = CameraInfo(
            kind="usb", description=f"fake {device}", width=640, height=480, fps=10.0,
            detail={"model": f"fake {device}", "device": device},
        )

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def capture_still(self) -> np.ndarray:
        return self.image.copy()

    def read(self) -> Frame | None:
        if time.time() - self.last < 0.1:
            return None
        self.last = time.time()
        self.seq += 1
        return Frame(seq=self.seq, ts=self.last, bgr=self.image.copy())

    @property
    def info(self) -> CameraInfo:
        return self._info


def test_two_cameras_are_detected_saved_and_streamed_separately(tmp_path, monkeypatch) -> None:
    import paho.mqtt.client as mqtt

    import pepperwatch.worker as worker_module

    cfg = load(
        {
            "mqtt": {"port": int(PORT)},
            "db": {"path": str(tmp_path / "db.sqlite")},
            "sessions": {"root": str(tmp_path / "sessions")},
            "media": {"root": str(tmp_path / "media")},
            "frames": {"shm_name": f"pw_it_{uuid.uuid4().hex[:8]}"},
            "inference": {"model_path": str(tmp_path / "absent.onnx")},  # the stub detector
            "camera": {"max_cameras": 2},
        },
        use_env=False,
    )
    cameras = [FakeCamera((40, 160, 60), "/dev/video0"), FakeCamera((60, 120, 200), "/dev/video2")]
    found = [CameraCandidate("usb", c.info.detail["device"], c.info.detail["model"]) for c in cameras]
    monkeypatch.setattr(worker_module, "open_cameras", lambda _cfg: (cameras, found))

    # Watch what the worker publishes, as the dashboard would.
    seen: dict[str, list[dict]] = {"frame": [], "session": [], "pipeline": []}

    def on_message(_c, _u, msg) -> None:
        kind = msg.topic.rsplit("/", 1)[-1]
        seen[kind].append({**json.loads(msg.payload), "received": time.time()})

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect("127.0.0.1", int(PORT))
    client.subscribe([
        ("pepperwatch/detect/frame", 0),
        ("pepperwatch/status/session", 0),
        ("pepperwatch/telemetry/pipeline", 0),
    ])
    client.loop_start()

    worker = worker_module.Worker(cfg)
    streamed: list[bool] = []
    switched_off_at: list[float] = []

    def during_the_run() -> None:
        time.sleep(3.0)
        readers = [make_reader(cfg, index) for index in (0, 1)]
        streamed.extend(reader.read() is not None for reader in readers)
        for reader in readers:
            reader.close()
        client.publish("pepperwatch/cmd/capture", json.dumps({"hi_res": True}), qos=1)
        time.sleep(3.0)
        client.publish("pepperwatch/cmd/camera", json.dumps({"camera": 1, "enabled": False}), qos=1)
        switched_off_at.append(time.time())
        time.sleep(3.0)
        worker.request_stop()

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    threading.Thread(target=during_the_run, daemon=True).start()
    try:
        assert worker.run() == 0
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        client.loop_stop()
        client.disconnect()

    # Each camera's frames reached its own segment, and its boxes the bus.
    assert streamed == [True, True]
    assert {message["camera"] for message in seen["frame"]} == {0, 1}
    assert [c["device"] for c in seen["session"][-1]["cameras"]] == ["/dev/video0", "/dev/video2"]

    # The first frame of each camera is saved, and the snapshot saved one each.
    import sqlite3

    rows = sqlite3.connect(cfg.db_path).execute(
        "SELECT camera, trigger FROM detection_event ORDER BY camera, trigger"
    ).fetchall()
    assert (0, "auto") in rows and (1, "auto") in rows
    assert (0, "manual") in rows and (1, "manual") in rows

    # Camera 2 turned off: its boxes stop (bar one already in flight), camera 1
    # carries on, and the pipeline telemetry says so.
    after = [m["camera"] for m in seen["frame"] if m["received"] > switched_off_at[0] + 1.0]
    assert after and set(after) == {0}
    assert seen["pipeline"][-1]["camera_on"] == [True, False]
    assert seen["pipeline"][-1]["camera_fps"][1] == 0.0

    # The session CSV says which camera every inference came from.
    (inference_csv,) = (tmp_path / "sessions").glob("*/inference.csv")
    with inference_csv.open() as handle:
        assert {row["camera"] for row in csv.DictReader(handle)} == {"0", "1"}
