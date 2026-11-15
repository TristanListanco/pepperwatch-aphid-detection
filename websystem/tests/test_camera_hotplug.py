"""USB cameras unplugged or plugged in while the worker runs, and starting with none."""

from __future__ import annotations

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
    worker._usb_arriving = set()
    worker._stopping = threading.Event()
    worker.restart_requested = False
    worker.db = FakeDb()
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


def test_a_camera_plugged_in_starts_a_new_session_once_it_has_settled(monkeypatch) -> None:
    present = {"/dev/video0"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0")

    present.add("/dev/video4")
    worker._check_cameras()
    assert not worker.restart_requested  # seen once: udev may not have finished
    worker._check_cameras()
    assert worker.restart_requested and worker._stopping.is_set()
    assert worker.db.logged == ["camera_connected"]


def test_the_same_camera_plugged_back_in_is_picked_up(monkeypatch) -> None:
    present = {"/dev/video0", "/dev/video2"}
    monkeypatch.setattr(worker_module, "_usb_present", lambda: present)
    worker = _worker("/dev/video0", "/dev/video2")

    present.discard("/dev/video2")
    worker._check_cameras()
    present.add("/dev/video2")  # back on the same device path
    worker._check_cameras()
    worker._check_cameras()
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
