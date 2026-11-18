"""USB cameras unplugged or plugged in while the worker runs, and starting with none."""

from __future__ import annotations

import tempfile
from pathlib import Path
import threading
import time

import pepperwatch.worker as worker_module
from pepperwatch.config import load
from pepperwatch.hal.base import CameraInfo
from pepperwatch.hal.discovery import CameraCandidate


class FakeCamera:
    def __init__(self, device: str, kind: str = "usb") -> None:
        self.info = CameraInfo(kind=kind, description=device, width=640, height=480, fps=25.0,
                               detail={"device": device})
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeDb:
    def __init__(self) -> None:
        self.logged: list[str] = []

    def log_system(self, *, event: str, **_) -> None:
        self.logged.append(event)


def _worker(*devices: str) -> worker_module.Worker:
    """A Worker with only what the camera watcher touches."""
    worker = worker_module.Worker.__new__(worker_module.Worker)
    worker.cameras = [FakeCamera(device) for device in devices]
    worker.camera_on = [True] * len(devices)
    worker.camera_connected = [True] * len(devices)
    worker.latest = [worker_module.LatestFrame() for _ in devices]
    worker._usb_seen = set(devices)
    worker._usb_arriving = {}
    worker._stopping = threading.Event()
    worker.restart_requested = False
    worker.db = FakeDb()
    worker.cfg = load({"sessions": {"root": tempfile.mkdtemp()}}, use_env=False)
    return worker


def test_an_unplugged_camera_stops_and_the_other_carries_on(monkeypatch) -> None:
    present = {"/dev/video0"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0", "/dev/video2")

    worker._check_cameras()

    assert worker.camera_connected == [True, False]
    assert worker.cameras[1].stopped and not worker.cameras[0].stopped
    assert worker._active() == [0]
    assert worker.db.logged == ["camera_unplugged"]
    assert not worker.restart_requested

    # Every camera gone: nothing to take frames from, and no restart either.
    present.clear()
    worker._check_cameras()
    assert worker._active() == []
    assert not worker.restart_requested


def _settle(worker) -> None:
    for _ in range(worker_module.ARRIVAL_CHECKS):
        worker._check_cameras()


def test_a_camera_plugged_in_starts_a_new_session_once_it_has_settled(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.Worker, "_camera_restart_at", float("-inf"))
    present = {"/dev/video0"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0")

    present.add("/dev/video4")
    for _ in range(worker_module.ARRIVAL_CHECKS - 1):
        worker._check_cameras()
    assert not worker.restart_requested  # not settled yet: it may be browning out
    worker._check_cameras()
    assert worker.restart_requested and worker._stopping.is_set()
    assert worker.db.logged == ["camera_connected"]
    # The new session says why it started.
    assert (Path(str(worker.cfg.sessions_root)) / ".next_label").read_text() == "camera-plugged-in"


def test_a_flapping_camera_restarts_the_session_at_most_once_a_minute(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.Worker, "_camera_restart_at", float("-inf"))
    present = {"/dev/video0", "/dev/video4"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    first = _worker("/dev/video0")
    first._usb_seen = {"/dev/video0"}
    _settle(first)
    assert first.restart_requested

    # The next session sees it vanish and come back at once: no second restart yet.
    second = _worker("/dev/video0", "/dev/video4")
    present.discard("/dev/video4")
    second._check_cameras()
    present.add("/dev/video4")
    _settle(second)
    assert not second.restart_requested
    monkeypatch.setattr(worker_module.Worker, "_camera_restart_at", float("-inf"))  # a minute later
    second._check_cameras()
    assert second.restart_requested


def test_a_camera_that_goes_quiet_is_not_restarted_while_it_comes_and_goes(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.Worker, "_camera_restart_at", float("-inf"))
    present = {"/dev/video0"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0")
    for _ in range(worker_module.ARRIVAL_CHECKS):  # there one second, gone the next
        present.add("/dev/video4")
        worker._check_cameras()
        present.discard("/dev/video4")
        worker._check_cameras()
    assert not worker.restart_requested


def test_the_same_camera_plugged_back_in_is_picked_up(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.Worker, "_camera_restart_at", float("-inf"))
    present = {"/dev/video0", "/dev/video2"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0", "/dev/video2")

    present.discard("/dev/video2")
    worker._check_cameras()
    present.add("/dev/video2")  # back on the same device path
    _settle(worker)
    assert worker.restart_requested


def test_without_sysfs_nothing_is_watched(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "_usb_present", lambda: None)
    worker = _worker("/dev/video0")
    worker._check_cameras()
    assert worker.camera_connected == [True] and not worker.restart_requested


def test_csi_cameras_are_not_watched(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "_usb_present", lambda: set())
    worker = _worker("0")
    worker.cameras = [FakeCamera("0", kind="csi")]
    worker._usb_seen = set()
    worker._check_cameras()
    assert worker.camera_connected == [True]


def test_with_no_camera_the_dashboard_is_told_until_one_is_plugged_in(monkeypatch) -> None:
    published = []

    class FakeBus:
        def start(self, timeout: float) -> bool:
            return False

        def publish_raw(self, topic, payload, **_) -> None:
            published.append((topic, payload))

        def stop(self) -> None:
            published.append("stopped")

    scans = iter([[], [], [CameraCandidate("usb", "/dev/video0", "Webcam")]])
    monkeypatch.setattr(worker_module, "make_bus", lambda *a, **k: FakeBus())
    monkeypatch.setattr(worker_module, "discover_cameras", lambda: next(scans))

    worker_module.wait_for_camera(load(use_env=False), poll_s=0.0, retry_s=5.0)

    assert published == [("pepperwatch/status/worker", "no_camera"), "stopped"]


def test_a_camera_that_will_not_start_is_tried_again_after_a_while(monkeypatch) -> None:
    class FakeBus:
        def start(self, timeout: float) -> bool:
            return True

        def publish_raw(self, *_, **__) -> None:
            pass

        def stop(self) -> None:
            pass

    broken = [CameraCandidate("usb", "/dev/video0", "Broken webcam")]
    monkeypatch.setattr(worker_module, "make_bus", lambda *a, **k: FakeBus())
    monkeypatch.setattr(worker_module, "discover_cameras", lambda: broken)
    began = time.monotonic()
    worker_module.wait_for_camera(load(use_env=False), poll_s=0.01, retry_s=0.1)
    assert time.monotonic() - began < 2.0  # returned on its own, nothing changed


def test_repeated_errors_are_reported_once_a_minute_with_a_count() -> None:
    throttle = worker_module._Throttle(60.0)
    assert throttle.due("camera_error:1") == 0
    assert throttle.due("camera_error:1") is None
    assert throttle.due("camera_error:1") is None
    assert throttle.due("inference_error") == 0  # each key on its own
    throttle._last["camera_error:1"] -= 61  # a minute on
    assert throttle.due("camera_error:1") == 2


def test_storage_levels_follow_the_free_space(monkeypatch, tmp_path) -> None:
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    free = {"mb": 5000}
    monkeypatch.setattr(worker_module.shutil, "disk_usage", lambda _: usage(0, 0, free["mb"] * 1_000_000))
    worker = _worker("/dev/video0")
    worker.cfg = load({"persist": {"min_free_mb": 2048, "critical_free_mb": 512}}, use_env=False)
    worker.media_root = tmp_path
    worker.storage = "ok"
    worker._storage_checked_at = 0.0

    worker._check_storage()
    assert worker.storage == "ok" and worker.db.logged == []
    free["mb"] = 1000
    worker._check_storage()
    assert worker.storage == "low" and worker._disk_free_mb == 1000
    free["mb"] = 100
    worker._check_storage()
    assert worker.storage == "full"
    free["mb"] = 9000  # photos downloaded and cleared
    worker._check_storage()
    assert worker.storage == "ok"
    assert worker.db.logged == ["storage_low", "storage_full", "storage_ok"]  # transitions only


def test_every_reconnect_announces_the_worker_again() -> None:
    """After a dropped connection the broker holds the Last Will ("offline");
    only announcing again on reconnect puts the dashboard right."""
    from pepperwatch.mqtt_bus import MqttBus

    bus = MqttBus(client_id="test")
    announced = []
    bus.on_connected(lambda: announced.append("online"))
    bus.on_connected(lambda: 1 / 0)  # a failing callback must not stop the others

    class Client:
        def subscribe(self, *_, **__) -> None:
            pass

    bus._on_connect(Client(), None, None, 0)
    bus._on_connect(Client(), None, None, 0)  # the reconnect
    assert announced == ["online", "online"]


def test_a_clean_disconnect_is_not_logged_as_a_problem(caplog) -> None:
    """paho 2's ReasonCode is truthy even for "Normal disconnection"."""
    import logging

    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    from pepperwatch.mqtt_bus import MqttBus

    bus = MqttBus(client_id="test")
    with caplog.at_level(logging.WARNING, logger="pepperwatch.mqtt_bus"):
        bus._on_disconnect(None, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0))
        assert not caplog.records
        bus._on_disconnect(None, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=128))
    assert "Unspecified error" in caplog.text


def test_a_silent_camera_is_reported_and_reopened_then_recovers(monkeypatch) -> None:
    import numpy as np

    from pepperwatch.hal.base import Frame

    monkeypatch.setattr(worker_module, "STALL_S", 0.2)
    monkeypatch.setattr(worker_module, "REOPEN_S", 0.3)

    class Silent(FakeCamera):
        def __init__(self) -> None:
            super().__init__("/dev/video0")
            self.starts, self.sending = 0, False

        def start(self) -> None:
            self.starts += 1

        def read(self):
            if not self.sending:
                return None
            return Frame(seq=1, ts=time.time(), bgr=np.zeros((8, 8, 3), np.uint8))

    class Writer:
        def publish(self, *_, **__) -> None:
            pass

    worker = _worker("/dev/video0")
    camera = Silent()
    worker.cameras = [camera]
    worker.cfg = load(use_env=False)
    worker.frames = [Writer()]
    worker.preview_fps = [worker_module.FpsMeter()]
    worker.inference_fps = worker_module.FpsMeter()
    worker.camera_stalled = [False]
    worker._frames_captured = 0
    worker._throttle = worker_module._Throttle(60.0)
    thread = threading.Thread(target=worker._capture_loop, args=(0,), daemon=True)
    thread.start()
    try:
        time.sleep(0.8)
        assert worker.camera_stalled == [True] and camera.starts >= 1
        assert "camera_stalled" in worker.db.logged
        camera.sending = True
        time.sleep(0.2)
        assert worker.camera_stalled == [False] and "camera_resumed" in worker.db.logged
    finally:
        worker._stopping.set()
        thread.join(timeout=2)


def test_low_storage_keeps_the_event_but_not_the_photo(tmp_path) -> None:
    import numpy as np

    from pepperwatch.db import Database
    from pepperwatch.hal.base import Frame

    class Bus:
        def publish(self, *_, **__) -> None:
            pass

    class Info:
        model_version, synthetic = "m", False

    cfg = load({"db": {"path": str(tmp_path / "db.sqlite")}, "media": {"root": str(tmp_path / "media")}},
               use_env=False)
    worker = _worker("/dev/video0")
    worker.cfg, worker.site, worker.bus = cfg, "Naawan", Bus()
    worker.db = Database(cfg)
    worker.db.initialise()
    worker.media_root = tmp_path / "media"
    worker.incoming = worker.media_root / ".incoming"
    worker.incoming.mkdir(parents=True)
    worker._inference_ms, worker._events_written = 1.0, 0
    worker.preview_fps, worker.inference_fps = [worker_module.FpsMeter()], worker_module.FpsMeter()
    frame = Frame(seq=7, ts=time.time(), bgr=np.zeros((48, 64, 3), np.uint8))

    worker.storage = "low"
    worker._write_event(0, frame, [], "none", Info(), trigger="auto")
    rows = worker.db.list_events(limit=5)["items"]
    assert len(rows) == 1 and rows[0]["image_path"] is None and "storage low" in rows[0]["note"]
    assert not list(worker.media_root.rglob("*.jpg"))

    worker.storage = "ok"
    worker._write_event(0, frame, [], "none", Info(), trigger="auto")
    assert len(list(worker.media_root.rglob("*.jpg"))) == 2  # the photo and its thumbnail


def test_thresholds_set_from_the_dashboard_outlast_the_session(tmp_path) -> None:
    from pepperwatch.db import Database

    cfg = load({"db": {"path": str(tmp_path / "db.sqlite")}}, use_env=False)
    database = Database(cfg)
    database.initialise()

    class Detector:
        def __init__(self) -> None:
            self.applied = None

        def set_thresholds(self, conf=None, iou=None) -> None:
            self.applied = (conf, iou)

    first = _worker("/dev/video0")
    first.cfg, first.db, first.detector = load(use_env=False), database, Detector()
    first._apply_config({"conf_threshold": 0.4})

    # A camera plugged in: a new session, which starts from the config files.
    second = _worker("/dev/video0")
    second.cfg, second.db, second.detector = load(use_env=False), database, Detector()
    assert second.cfg.inference.conf_threshold == 0.25
    second._restore_settings()
    assert second.cfg.inference.conf_threshold == 0.4 and second.detector.applied == (0.4, None)


def test_a_renamed_site_is_stamped_on_new_photos_and_outlasts_the_session(tmp_path) -> None:
    from pepperwatch.db import Database

    cfg = load({"db": {"path": str(tmp_path / "db.sqlite")}}, use_env=False)
    database = Database(cfg)
    database.initialise()

    running = _worker("/dev/video0")
    running.db, running.site = database, "Naawan"
    running._apply_config({"site_name": "Buru-un"})  # the dashboard's rename
    assert running.site == "Buru-un" and running.cfg.site.name == "Buru-un"

    database.set_meta("site_name", "Buru-un")  # what the dashboard saved
    later = _worker("/dev/video0")
    later.db, later.site = database, "Naawan"
    later._restore_settings()
    assert later.site == "Buru-un"


def test_photo_interval_set_from_the_dashboard_reaches_every_camera_and_outlasts_the_session(tmp_path) -> None:
    from pepperwatch.db import Database

    cfg = load({"db": {"path": str(tmp_path / "db.sqlite")}}, use_env=False)
    database = Database(cfg)
    database.initialise()

    def worker() -> worker_module.Worker:
        made = _worker("/dev/video0", "/dev/video2")
        made.cfg, made.db, made.detector = load(use_env=False), database, object()
        made.policy = [worker_module.PersistPolicy(heartbeat_detected_s=30, heartbeat_clean_s=300) for _ in range(2)]
        return made

    first = worker()
    first._apply_config({"heartbeat_detected_s": 60})
    assert [policy.heartbeat_detected_s for policy in first.policy] == [60, 60]
    assert first.cfg.persist.heartbeat_detected_s == 60

    second = worker()  # a new session, which starts from the config files
    second._restore_settings()
    assert [policy.heartbeat_detected_s for policy in second.policy] == [60, 60]
    assert [policy.heartbeat_clean_s for policy in second.policy] == [300, 300]


def test_previews_are_encoded_only_as_often_as_they_can_be_sent(monkeypatch) -> None:
    import numpy as np

    from pepperwatch.hal.base import Frame

    class Fast(FakeCamera):
        seq = 0

        def read(self):
            self.seq += 1
            time.sleep(0.005)  # 200 fps
            return Frame(seq=self.seq, ts=time.time(), bgr=np.zeros((8, 8, 3), np.uint8))

    published = []

    class Writer:
        def publish(self, *_, **__) -> None:
            published.append(time.monotonic())

    worker = _worker("/dev/video0")
    worker.cameras = [Fast("/dev/video0")]
    worker.cfg = load({"web": {"stream_fps": 10}}, use_env=False)
    worker.frames = [Writer()]
    worker.preview_fps = [worker_module.FpsMeter()]
    worker.inference_fps = worker_module.FpsMeter()
    worker.camera_stalled = [False]
    worker._frames_captured = 0
    worker._throttle = worker_module._Throttle(60.0)
    thread = threading.Thread(target=worker._capture_loop, args=(0,), daemon=True)
    thread.start()
    time.sleep(0.5)
    worker._stopping.set()
    thread.join(timeout=2)
    assert worker._frames_captured > 40  # every frame still reaches the detector
    assert len(published) <= 7  # ~10 a second, not 200
