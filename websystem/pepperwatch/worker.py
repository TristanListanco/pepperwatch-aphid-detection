"""The inference worker: ``pepperwatch-worker``.

Owns the camera and runs manuscript Figure 3.3's pipeline — acquire, letterbox,
infer, threshold, NMS, scale back, publish, persist — then hands results to the
web process over shared memory (frames) and MQTT (telemetry and detections).

The structural decision worth understanding is the **split between preview rate
and inference rate**. §3.2 asks for at least 17 FPS, but an INT8 YOLOv8n on a Pi
5 CPU at 640x640 realistically delivers somewhere near 5-10. Coupling the two
would mean the live view stuttered at the inference rate, making the dashboard
feel broken for a reason that has nothing to do with the dashboard.

So a capture thread runs the camera flat out into a **single-slot** buffer,
dropping any frame the detector did not get to, while the inference thread always
takes the newest frame available. The preview streams smoothly, inference runs as
fast as the silicon allows, and the two numbers are reported separately —
``preview_fps`` and ``inference_fps`` — so Chapter IV can state what was actually
achieved instead of collapsing both into one figure that flatters neither.

Single-slot, not a queue, is the important part: a queue would let the detector
fall behind in *memory*, and the boxes would drift further and further from what
the camera is currently seeing. Falling behind in time and dropping frames is the
correct failure mode for a live monitor.

Each run of the worker is one **session**. At start it checks for an AI HAT+
(``accel.py``), applies the matching ``[profiles.*]`` table, opens whichever
camera is attached (``hal/discovery.py``), and records every inference and a
once-a-second system sample to CSV (``session.py``). The dashboard's "New
session" button closes the camera and the HAT and runs that detection again
from scratch, within the same process.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .accel import AcceleratorChoice, choose_accelerator
from .config import Config, detect_board_info, load, total_memory_mb
from .db import Database, utcnow
from .detect import Detector, get_detector, severity_from_config
from .detect.postprocess import drop_non_plant
from .frames import make_writer
from .hal import CameraError, CameraSource, Frame, discover_cameras, get_telemetry, open_cameras
from .hal.discovery import SYSFS_VIDEO, usb_cameras
from .models import Detection
from .mqtt_bus import (
    STATUS_ERROR,
    STATUS_NO_CAMERA,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_RESTARTING,
    MqttBus,
    Topics,
    make_bus,
)
from .render import draw_detections, encode_jpeg, make_thumbnail
from .session import SessionRecorder, recover_interrupted, request_next_label, write_durably
from .thermal import ThermalGovernor

log = logging.getLogger(__name__)

# run() found no camera sending pictures: main() waits for the cameras to
# change instead of exiting.
EXIT_NO_FRAMES = 3
# A camera with no frame for this long is reported, and reopened this often.
STALL_S = 10.0
REOPEN_S = 30.0
# A camera plugged in must stay for this many checks (about a second each)
# before it starts a new session, and camera-driven restarts are this far apart:
# a camera browning out on a weak supply would otherwise restart the session,
# and reload the model, every few seconds.
ARRIVAL_CHECKS = 10
CAMERA_RESTART_GAP_S = 60.0


class _NoRoom(Exception):
    """The SD card is low on space: the photo is skipped, the event still saved."""


class _Throttle:
    """At most one report per key per interval, counting the ones skipped."""

    def __init__(self, interval_s: float = 60.0) -> None:
        self._interval = interval_s
        self._last: dict[str, float] = {}
        self._skipped: dict[str, int] = {}
        self._lock = threading.Lock()

    def due(self, key: str) -> int | None:
        """How many were skipped since the last report, if one is due now; else None."""
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                self._skipped[key] = self._skipped.get(key, 0) + 1
                return None
            self._last[key] = now
            return self._skipped.pop(key, 0)


class LatestFrame:
    """A one-deep slot holding the newest frame, with a dropped-frame count."""

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self.dropped = 0

    def put(self, frame: Frame) -> None:
        with self._lock:
            if self._frame is not None:
                # The detector never saw the previous frame. Count it: the drop
                # rate is the honest measure of how far inference trails capture.
                self.dropped += 1
            self._frame = frame
        self._arrived.set()

    def take(self, timeout: float = 1.0) -> Frame | None:
        """Remove and return the newest frame, waiting briefly if none is ready."""
        if not self._arrived.wait(timeout=timeout):
            return None
        with self._lock:
            frame, self._frame = self._frame, None
            self._arrived.clear()
        return frame


class FpsMeter:
    """Frame rate over a trailing wall-clock window."""

    def __init__(self, window_s: float = 3.0) -> None:
        self._window_s = window_s
        self._marks: deque[float] = deque()

    def mark(self) -> None:
        now = time.monotonic()
        self._marks.append(now)
        cutoff = now - self._window_s
        while self._marks and self._marks[0] < cutoff:
            self._marks.popleft()

    @property
    def fps(self) -> float:
        if len(self._marks) < 2:
            return 0.0
        span = self._marks[-1] - self._marks[0]
        return (len(self._marks) - 1) / span if span > 0 else 0.0


@dataclass
class PersistDecision:
    write: bool
    trigger: str
    reason: str


@dataclass
class PersistPolicy:
    """Decides which frames become database rows.

    At 17 fps over an eight-hour cycle the camera produces roughly 490,000
    frames a day. Writing a row for each would destroy the SD card and make the
    Historical Detection Log unusable, and the manuscript does not address it.
    So a frame is persisted when it is *informative*: the clean/detected state
    flipped, the severity band moved, a heartbeat interval elapsed, or the user
    asked for a snapshot.

    Two guards make "informative" hold up in practice, both added after
    measuring 5.6 events per second on real frames without them:

    **Debounce.** A detector whose box count hovers on a band boundary flips
    severity every frame, and a naive state-change rule writes a row for each
    flip. A new band must therefore hold for ``state_change_debounce_s`` before
    it is believed. This is the same reasoning as the trend tile's stability
    band: an agricultural officer wants to know the infestation got worse, not
    that the count crossed five and came back.

    **Rate cap.** ``min_write_interval_s`` bounds automatic writes no matter
    which rule fires, so no combination of thresholds can produce a write storm.
    Manual snapshots bypass it, because a user pressing a button should always
    be obeyed.
    """

    heartbeat_detected_s: float
    heartbeat_clean_s: float
    on_state_change: bool = True
    state_change_debounce_s: float = 4.0
    min_write_interval_s: float = 8.0

    _last_write: float = field(default=0.0, init=False)
    _confirmed: str = field(default="none", init=False)
    _candidate: str = field(default="none", init=False)
    _candidate_since: float = field(default=0.0, init=False)
    _started: bool = field(default=False, init=False)
    suppressed: int = field(default=0, init=False)

    def decide(self, severity: str, *, now: float | None = None) -> PersistDecision:
        now = now if now is not None else time.monotonic()

        # Track how long the observed band has held.
        if severity != self._candidate:
            self._candidate = severity
            self._candidate_since = now

        if not self._started:
            self._started = True
            self._confirmed = severity
            self._last_write = now
            return PersistDecision(True, "auto", "first observation")

        held_for = now - self._candidate_since
        since_write = now - self._last_write

        # Promote the candidate only once it has held long enough.
        confirmed_change = (
            self.on_state_change
            and self._candidate != self._confirmed
            and held_for >= self.state_change_debounce_s
        )

        detected = self._confirmed != "none"
        interval = self.heartbeat_detected_s if detected else self.heartbeat_clean_s
        heartbeat_due = since_write >= interval

        if not (confirmed_change or heartbeat_due):
            return PersistDecision(False, "", "")

        # The rate cap applies to both rules. A change that arrives too soon is
        # not discarded -- the candidate keeps holding, so it writes as soon as
        # the cap allows.
        if since_write < self.min_write_interval_s:
            self.suppressed += 1
            return PersistDecision(False, "", "rate capped")

        previous = self._confirmed
        self._confirmed = self._candidate
        self._last_write = now

        if confirmed_change:
            was = "clean" if previous == "none" else previous
            now_band = "clean" if self._confirmed == "none" else self._confirmed
            return PersistDecision(
                True, "state_change", f"severity {was} -> {now_band} held {held_for:.1f}s"
            )
        return PersistDecision(True, "heartbeat", f"{interval:.0f}s heartbeat")

    @property
    def confirmed_severity(self) -> str:
        return self._confirmed


def start_session_hardware(base: Config) -> tuple[Config, AcceleratorChoice, Detector]:
    """Pick the accelerator, apply its profile, and build the detector.

    If the AI HAT+ was chosen but its detector would not start, the session
    drops to the CPU profile before the CPU detector is built, so the CPU path
    gets its own thread count rather than the HAT profile's.
    """
    choice = choose_accelerator(base)
    cfg = base.with_profile(choice.profile)
    detector, final = get_detector(cfg, choice)
    if final.profile != choice.profile:
        detector.close()
        cfg = base.with_profile(final.profile)
        detector, final = get_detector(cfg, final)
    log.info("accelerator: %s (%s)", final.name, final.reason)
    return cfg, final, detector


class Worker:
    """Runs the detection pipeline until told to stop."""

    # When a camera last restarted a session, across sessions (see CAMERA_RESTART_GAP_S).
    _camera_restart_at = float("-inf")

    def __init__(self, base_cfg: Config) -> None:
        self.base_cfg = base_cfg
        self.cfg, self.accelerator, self.detector = start_session_hardware(base_cfg)
        cfg = self.cfg
        self.site = cfg.site.name

        try:
            self.cameras: list[CameraSource]
            self.cameras, self.cameras_found = open_cameras(cfg)
        except Exception:
            self.detector.close()
            raise
        # The primary camera: warm-up, logs, and the single-camera fields of the
        # session record. Everything per camera below is indexed like cameras.
        self.camera = self.cameras[0]
        count = len(self.cameras)
        self.telemetry = get_telemetry(cfg)
        self.db = Database(cfg)
        self.frames = [make_writer(cfg, index) for index in range(count)]
        self.bus: MqttBus = make_bus(cfg, "worker", with_will=True)

        self.governor = ThermalGovernor()
        self.latest = [LatestFrame() for _ in range(count)]
        # Turned off from the dashboard: no reads, no encodes, no inference, so
        # the other camera gets the whole detector. Until the next session.
        self.camera_on = [True] * count
        # Unplugged while running (USB only): stopped and shown as such, while
        # the others carry on. Plugging one in restarts the session instead.
        self.camera_connected = [True] * count
        # USB cameras seen this session. One that appears and is not in here (a
        # new camera, or one plugged back in) is picked up by a new session.
        self._usb_seen = {c.device for c in self.cameras_found if c.kind == "usb"}
        self._usb_arriving: dict[str, int] = {}  # device -> checks in a row it has been seen
        # Plugged in but sending nothing (a hung webcam, a loose ribbon).
        self.camera_stalled = [False] * count
        # ok | low (no photos saved) | full (nothing saved): see _check_storage.
        self.storage = "ok"
        self._disk_free_mb: int | None = None
        self._storage_checked_at = float("-inf")
        self._throttle = _Throttle(60.0)
        self.preview_fps = [FpsMeter() for _ in range(count)]
        self.inference_fps = FpsMeter()

        # Per camera, so one camera's state changes and heartbeats do not reset
        # the other's.
        self.policy = [
            PersistPolicy(
                heartbeat_detected_s=cfg.persist.heartbeat_detected_s,
                heartbeat_clean_s=cfg.persist.heartbeat_clean_s,
                on_state_change=cfg.persist.on_state_change,
                state_change_debounce_s=cfg.persist.state_change_debounce_s,
                min_write_interval_s=cfg.persist.min_write_interval_s,
            )
            for _ in range(count)
        ]

        self._stopping = threading.Event()
        self._paused = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._pending_snapshots: list[dict[str, Any]] = []

        self._last_telemetry: dict[str, Any] = {}
        self._last_thermal_level = 0
        self._last_severity = ["none"] * count
        self._inference_ms = 0.0
        self._events_written = 0
        self._frames_captured = 0
        self.recorder: SessionRecorder | None = None
        self.restart_requested = False

        self.media_root = cfg.media_root
        # Photos are written here, synced, then moved into place, so a power
        # cut leaves its half-written photo here rather than in the gallery.
        self.incoming = self.media_root / ".incoming"
        shutil.rmtree(self.incoming, ignore_errors=True)
        self.incoming.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        """Start everything and block until stopped. Returns an exit code."""
        info = self.detector.info
        log.info(
            "worker starting: site=%s accelerator=%s detector=%s (%s) cameras=%s",
            self.site, self.accelerator.name, info.kind, info.model_version,
            "; ".join(c.info.description for c in self.cameras),
        )
        if info.synthetic:
            log.warning(
                "detector is synthetic (%s) — events will be flagged and must not "
                "be reported as measurements", info.model_version,
            )

        try:
            return self._run()
        finally:
            self.shutdown()

    def _run(self) -> int:
        info = self.detector.info
        self.db.initialise(writer="worker")
        self._restore_thresholds()
        for session_id in recover_interrupted(self.cfg.sessions_root):
            log.warning("session %s was cut off (power loss or crash); closed as interrupted", session_id)
            self.db.log_system(level="warn", event="session_recovered", source="worker", detail=session_id)
        self._check_storage()
        # Announced on every connect, not once: after a reconnect the broker
        # holds this worker's Last Will ("offline") until it is overwritten.
        self.bus.on_connected(self._announce)
        self.bus.start(timeout=3.0)
        self.bus.subscribe(Topics.COMMANDS, self._on_command, qos=1)

        self.db.log_system(
            event="startup",
            source="worker",
            detail=(
                f"detector={info.kind}:{info.model_version} accelerator={self.accelerator.name} "
                f"cameras={'; '.join(c.info.description for c in self.cameras)}"
            ),
        )

        # Warm up before measuring anything: the first ONNX call allocates
        # arenas and picks kernels, and that cost would otherwise land in the
        # first reported frame and skew every benchmark.
        primer = self._await_first_frame()
        if primer is None:
            log.error("no camera sent a picture in 10 s; waiting for the cameras to change")
            return EXIT_NO_FRAMES
        self.detector.warmup(primer.bgr, rounds=2)

        # Recording starts after warm-up, so first-call allocation does not
        # land in the session's latency figures.
        self.recorder = SessionRecorder(
            self.cfg.sessions_root,
            self._session_meta(),
            flush_interval_s=self.cfg.sessions.flush_interval_s,
            summary_interval_s=self.cfg.sessions.summary_interval_s,
        )
        self.bus.publish(Topics.STATUS_SESSION, self._session_status(), qos=1, retain=True)

        threads = [
            threading.Thread(target=self._capture_loop, args=(index,), name=f"capture-{index}", daemon=True)
            for index in range(len(self.cameras))
        ]
        threads.append(threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True))
        for thread in threads:
            thread.start()

        self._inference_loop()
        return 0

    def _announce(self) -> None:
        """The retained status: online, the detector, and the session if one runs."""
        info = self.detector.info
        self.bus.publish_raw(Topics.STATUS_WORKER, STATUS_ONLINE, qos=1, retain=True)
        self.bus.publish(
            Topics.STATUS_DETECTOR,
            {
                "kind": info.kind,
                "model_version": info.model_version,
                "description": info.description,
                "input_size": info.input_size,
                "class_names": info.class_names,
                "synthetic": info.synthetic,
                "camera": self.camera.info.kind,
                "accelerator": self.accelerator.name,
                "detail": info.detail,
            },
            qos=1,
            retain=True,
        )
        if self.recorder is not None:
            self.bus.publish(Topics.STATUS_SESSION, self._session_status(), qos=1, retain=True)

    def _session_status(self) -> dict[str, Any]:
        assert self.recorder is not None
        return {**self.recorder.meta, "directory": str(self.recorder.directory)}

    def _await_first_frame(self, timeout: float = 10.0) -> Frame | None:
        """The first frame from any camera: one that stays silent must not stop the others."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for camera in self.cameras:
                try:
                    frame = camera.read()
                except Exception:  # noqa: BLE001 — a camera failing here is reported by the capture loop
                    frame = None
                if frame is not None:
                    return frame
            time.sleep(0.01)
        return None

    def _report(self, key: str, event: str, detail: str = "") -> None:
        """A recurring error: to the log and system_log at most once a minute per key.

        Call from an except block. A loose camera ribbon fails twice a second and
        a broken model on every frame; unthrottled, either fills the SD card.
        """
        skipped = self._throttle.due(key)
        if skipped is None:
            return
        more = f" (+{skipped} more since the last report)" if skipped else ""
        log.exception("%s%s%s", event, f": {detail}" if detail else "", more)
        try:
            self.db.log_system(level="error", event=event, source="worker", detail=f"{detail}{more}".strip() or None)
        except Exception:  # noqa: BLE001 — a full disk refuses the log row too
            pass

    def request_stop(self, *_: object) -> None:
        log.info("stop requested")
        self._stopping.set()

    def shutdown(self) -> None:
        log.info("worker stopping after %d events", self._events_written)
        self._stopping.set()
        if self.recorder is not None:
            try:
                self.recorder.update_counters(**self._counters())
                summary = self.recorder.close()
                self.bus.publish(Topics.SESSION_SUMMARY, summary, qos=1)
            except Exception:  # noqa: BLE001
                log.exception("could not close the session record")
        try:
            self.db.log_system(event="shutdown", source="worker", detail=f"events={self._events_written}")
        except Exception:  # noqa: BLE001
            pass
        # A clean disconnect suppresses the Last Will, so publish the offline
        # status deliberately — an intentional stop should not look like a crash.
        # A new session follows a restart at once, so that is "restarting".
        final = STATUS_RESTARTING if self.restart_requested else STATUS_OFFLINE
        self.bus.stop(final_status=(Topics.STATUS_WORKER, final))
        for camera in self.cameras:
            camera.stop()
        self.telemetry.close()
        self.detector.close()
        for writer in self.frames:
            writer.close()
        self.db.close()

    # -- threads -----------------------------------------------------------

    def _capture_loop(self, index: int) -> None:
        """Run one camera flat out, publishing its clean preview as we go."""
        quality = self.cfg.camera.jpeg_quality
        camera, meter, writer = self.cameras[index], self.preview_fps[index], self.frames[index]
        last_frame_at = time.monotonic()
        reopened_at = time.monotonic()
        # The web sends each camera at most [web] stream_fps frames a second;
        # encoding the other 15 of 25 a second only heats the CPU.
        publish_every = 1.0 / max(1.0, float(self.cfg.web.stream_fps))
        published_at = float("-inf")
        while not self._stopping.is_set():
            if not (self.camera_on[index] and self.camera_connected[index]):
                self._stopping.wait(timeout=0.2)
                last_frame_at = time.monotonic()  # off is not stalled
                continue
            try:
                frame = camera.read()
            except Exception:  # noqa: BLE001
                if not self.camera_connected[index]:
                    continue  # unplugged during the read
                self._report(f"camera_error:{index}", "camera_error", f"camera {index}")
                time.sleep(0.5)
                frame = None

            if frame is None:
                # Plugged in but silent: say so, and reopen it now and then,
                # from this thread so nothing else touches the camera meanwhile.
                now = time.monotonic()
                if now - last_frame_at >= STALL_S:
                    if not self.camera_stalled[index]:
                        self.camera_stalled[index] = True
                        log.warning("camera %d sent no picture for %.0f s", index, now - last_frame_at)
                        self._log_once("camera_stalled", f"camera {index}")
                    if now - reopened_at >= REOPEN_S:
                        reopened_at = now
                        self._reopen(index)
                time.sleep(0.002)
                continue

            last_frame_at = time.monotonic()
            if self.camera_stalled[index]:
                self.camera_stalled[index] = False
                log.info("camera %d is sending pictures again", index)
                self._log_once("camera_resumed", f"camera {index}")

            meter.mark()
            self._frames_captured += 1

            # Published CLEAN. The browser draws boxes on a canvas over this,
            # which keeps encode cost off the inference budget and lets the
            # overlay be toggled and scaled per device.
            if time.monotonic() - published_at >= publish_every:
                published_at = time.monotonic()
                try:
                    writer.publish(
                        encode_jpeg(frame.bgr, quality),
                        seq=frame.seq,
                        preview_fps=meter.fps,
                        inference_fps=self.inference_fps.fps,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("frame publish failed")

            self.latest[index].put(frame)

    def _telemetry_loop(self) -> None:
        """Sample sensors, drive the thermal governor, and log on an interval."""
        interval = float(self.cfg.persist.telemetry_interval_s)
        period = float(self.cfg.sessions.system_interval_s)
        last_logged = 0.0
        chip_temperature = getattr(self.detector, "chip_temperature", None)

        while not self._stopping.is_set():
            try:
                sample = self.telemetry.sample()
            except Exception:  # noqa: BLE001
                log.exception("telemetry sample failed")
                time.sleep(interval)
                continue
            try:
                last_logged = self._telemetry_tick(sample, chip_temperature, interval, last_logged)
            except Exception:  # noqa: BLE001 — this thread is the dashboard's heartbeat: never let it die
                self._report("telemetry_loop", "telemetry_error")
            self._stopping.wait(timeout=min(period, interval))

    def _telemetry_tick(self, sample, chip_temperature, interval: float, last_logged: float) -> float:
        """One telemetry round. Returns when telemetry was last logged."""
        payload = sample.as_dict()
        payload["hailo_temp_c"] = chip_temperature() if chip_temperature else None
        payload["accelerator"] = self.accelerator.name
        self._last_telemetry = payload

        state = self.governor.update(sample.cpu_temp_c)
        payload["thermal_level"] = state.level
        payload["thermal_multiplier"] = state.multiplier
        payload["backing_off"] = state.backing_off
        self._record_system(sample, payload, state.level)
        self._check_cameras()
        if time.monotonic() - self._storage_checked_at >= 10.0:
            self._check_storage()

        self.bus.publish(Topics.TELEMETRY_SYSTEM, payload, qos=0)
        active = self._active()
        self.bus.publish(
            Topics.TELEMETRY_PIPELINE,
            {
                "preview_fps": round(self.preview_fps[0].fps, 2),
                # An off camera's meter keeps its last marks; report it as stopped.
                "camera_fps": [
                    round(meter.fps, 2) if index in active else 0.0
                    for index, meter in enumerate(self.preview_fps)
                ],
                "camera_on": list(self.camera_on),
                "camera_connected": list(self.camera_connected),
                "camera_stalled": list(self.camera_stalled),
                "storage": self.storage,
                "disk_free_mb": self._disk_free_mb,
                # What the detector is using, so Settings can confirm a change.
                "conf_threshold": self.cfg.inference.conf_threshold,
                "iou_threshold": self.cfg.inference.iou_threshold,
                "inference_fps": round(self.inference_fps.fps, 2),
                "inference_ms": round(self._inference_ms, 2),
                "dropped": self._dropped(),
                "target_fps": self.cfg.inference.target_fps,
                "paused": self._paused.is_set(),
                "thermal_level": state.level,
            },
            qos=0,
        )

        # A thermal transition is a diagnostic event in its own right, and
        # it explains an FPS dip that would otherwise look inexplicable in
        # the Chapter IV series.
        if state.level != self._last_thermal_level:
            self.db.log_system(
                level="warn",
                event="thermal_backoff",
                source="worker",
                telemetry=payload,
                detail=f"level {self._last_thermal_level} -> {state.level}: {state.reason}",
            )
            self._last_thermal_level = state.level

        now = time.monotonic()
        if now - last_logged >= interval:
            self.db.log_system(event="telemetry", source="worker", telemetry=payload)
            last_logged = now

        if self.recorder is not None and self.recorder.summary_due():
            self.recorder.update_counters(**self._counters())
            self.bus.publish(Topics.SESSION_SUMMARY, self.recorder.write_summary(), qos=0)
        return last_logged

    def _record_system(self, sample, payload: dict[str, Any], thermal_level: int) -> None:
        if self.recorder is None or self.storage == "full":
            return
        self.recorder.record_system(
            {
                "cpu_temp_c": sample.cpu_temp_c,
                "hailo_temp_c": payload.get("hailo_temp_c"),
                "cpu_percent": sample.cpu_percent,
                "cpu_freq_mhz": sample.cpu_freq_mhz,
                "mem_used_mb": sample.mem_used_mb,
                "fan_rpm": sample.fan_rpm,
                "throttled_now": sample.throttled,
                "throttled_flags": hex(sample.throttled_flags) if sample.throttled_flags is not None else None,
                "ina219_voltage_v": sample.voltage_v,
                "ina219_current_ma": sample.current_ma,
                "ina219_power_w": sample.power_w,
                "pmic_power_w": sample.pmic_power_w,
                "input_voltage_v": sample.input_voltage_v,
                "power_w": sample.best_power_w,
                "power_source": sample.power_source,
                "preview_fps": round(self.preview_fps[0].fps, 2),
                "inference_fps": round(self.inference_fps.fps, 2),
                "inference_ms": round(self._inference_ms, 2),
                "dropped_frames": self._dropped(),
                "thermal_level": thermal_level,
                "paused": self._paused.is_set(),
            }
        )

    def _dropped(self) -> int:
        return sum(slot.dropped for slot in self.latest)

    def _counters(self) -> dict[str, int]:
        return {
            "frames_captured": self._frames_captured,
            "dropped_frames": self._dropped(),
            "events_persisted": self._events_written,
        }

    def _inference_loop(self) -> None:
        """Detect on the newest frame available, forever."""
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        info = self.detector.info
        base_interval = 1.0 / max(self.cfg.inference.target_fps, 1)
        turn = 0

        while not self._stopping.is_set():
            self._drain_snapshots()

            if self._paused.is_set():
                self._stopping.wait(timeout=0.2)
                continue

            # One detector, several cameras: take their newest frames in turn,
            # so each camera that is on gets an equal share of the inference rate.
            active = self._active()
            if not active:  # every camera off or unplugged
                self._stopping.wait(timeout=0.2)
                continue
            index = active[turn % len(active)]
            turn += 1
            frame = self.latest[index].take(timeout=0.5 / len(active))
            if frame is None:
                continue

            began = time.perf_counter()
            try:
                detections, non_plant = self._detect(frame.bgr)
            except Exception:  # noqa: BLE001
                self._report("inference_error", "inference_error")
                continue
            self._inference_ms = (time.perf_counter() - began) * 1000.0
            self.inference_fps.mark()

            severity, damaged_area = severity_from_config(detections, self.cfg)
            # Recording and saving can fail (a full SD card, a locked database);
            # the live detections must carry on regardless.
            try:
                self._record_inference(index, frame, detections, severity, damaged_area, non_plant)
            except Exception:  # noqa: BLE001
                self._report("session_record", "session_record_failed")
            self._publish_detections(index, frame, detections, severity, damaged_area)
            try:
                self._maybe_persist(index, frame, detections, severity, info)
            except Exception:  # noqa: BLE001
                self._report("persist", "persist_failed")

            # The governor stretches this interval when hot. It does not touch
            # the capture loop, so the preview stays smooth while backing off.
            state = self.governor.update(self._last_telemetry.get("cpu_temp_c"))
            if state.multiplier > 1.0:
                self._stopping.wait(timeout=base_interval * (state.multiplier - 1.0))

    def _detect(self, bgr: np.ndarray) -> tuple[list[Detection], int]:
        """Detect, then drop boxes with almost no plant colour. Returns the
        boxes kept and how many were dropped, for the session record."""
        found = self.detector.detect(bgr)
        if self.detector.info.synthetic:  # the stub's boxes are not on plants anyway
            return found, 0
        kept = drop_non_plant(bgr, found, float(self.cfg.inference.min_plant_fraction))
        return kept, len(found) - len(kept)

    # -- publishing and persistence ---------------------------------------

    def _record_inference(
        self,
        index: int,
        frame: Frame,
        detections: list[Detection],
        severity: str,
        damaged_area: float,
        non_plant: int,
    ) -> None:
        if self.recorder is None or self.storage == "full":
            return
        timings = self.detector.timings
        damaged = [d for d in detections if d.class_name != "healthy"]
        self.recorder.record_inference(
            {
                "camera": index,
                "frame_seq": frame.seq,
                "preprocess_ms": timings.preprocess_ms,
                "infer_ms": timings.infer_ms,
                "postprocess_ms": timings.postprocess_ms,
                "total_ms": timings.total_ms,
                # Capture to result: queueing in the one-slot buffer plus the
                # whole detect call. What an observer of the live view waits.
                "end_to_end_ms": (time.time() - frame.ts) * 1000.0,
                "inference_fps": self.inference_fps.fps,
                "preview_fps": self.preview_fps[index].fps,
                "dropped_frames": self._dropped(),
                "detections": len(detections),
                "non_plant_dropped": non_plant,
                "damage_boxes": len(damaged),
                "max_confidence": max((d.confidence for d in damaged), default=None),
                "damaged_area": damaged_area,
                "severity": severity,
                "thermal_level": self._last_thermal_level,
            }
        )

    def _session_meta(self) -> dict[str, Any]:
        """Everything a reader of the CSV needs to know about this run."""
        info = self.detector.info
        camera = self.camera.info
        board = detect_board_info()
        model_path = Path(info.detail.get("hef_path") or info.detail.get("model_path") or "")
        return {
            "accelerator": self.accelerator.as_dict(),
            "camera": {
                "kind": camera.kind,
                "model": camera.detail.get("model"),
                "device": camera.detail.get("device"),
                "description": camera.description,
                "width": camera.width,
                "height": camera.height,
                "fps": camera.fps,
                "found": [c.as_dict() for c in self.cameras_found],
            },
            # Every camera in use, in dashboard order; "camera" above is the first.
            "cameras": [
                {
                    "index": index,
                    "kind": c.info.kind,
                    "model": c.info.detail.get("model"),
                    "device": c.info.detail.get("device"),
                    "description": c.info.description,
                    "width": c.info.width,
                    "height": c.info.height,
                }
                for index, c in enumerate(self.cameras)
            ],
            "detector": {
                "kind": info.kind,
                "description": info.description,
                "model_version": info.model_version,
                "model_file": model_path.name or None,
                "model_sha256": _sha256(model_path) if model_path.is_file() else None,
                "input_size": info.input_size,
                "class_names": info.class_names,
                "synthetic": info.synthetic,
            },
            "settings": {
                "profile": self.cfg.get("session_profile"),
                "preview_fps": self.cfg.camera.preview_fps,
                "conf_threshold": self.cfg.inference.conf_threshold,
                "iou_threshold": self.cfg.inference.iou_threshold,
                "min_plant_fraction": self.cfg.inference.min_plant_fraction,
                "intra_op_threads": self.cfg.inference.intra_op_threads if info.kind == "onnx" else None,
                "target_fps": self.cfg.inference.target_fps,
            },
            "host": {
                "board": board.model,
                "board_family": board.family,
                "board_key": board.key,
                "board_tier": board.tier,
                "board_profiles": self.cfg.board_profile,
                "cpu": board.cpu,
                "ram_mb": round(total_memory_mb() or 0),
                "os": _os_name(),
                "kernel": platform.release(),
                "python": platform.python_version(),
            },
            "site": self.site,
        }

    def _publish_detections(
        self, index: int, frame: Frame, detections: list[Detection], severity: str, damaged_area: float
    ) -> None:
        self.bus.publish(
            Topics.DETECT_FRAME,
            {
                "camera": index,
                "frame_seq": frame.seq,
                "ts": frame.ts,
                "severity": severity,
                "damaged_area": round(damaged_area, 5),
                "inference_ms": round(self._inference_ms, 2),
                "boxes": [d.as_dict() for d in detections],
            },
            qos=0,
        )

        # Only on change, at QoS 1: this is the signal an alert would key off,
        # so it must arrive, and it must not arrive ten times a second.
        if severity != self._last_severity[index]:
            damaged = [d for d in detections if d.class_name != "healthy"]
            self.bus.publish(
                Topics.DETECT_FLAG,
                {
                    "camera": index,
                    "severity": severity,
                    "previous": self._last_severity[index],
                    "count": len(damaged),
                    "max_confidence": round(max((d.confidence for d in damaged), default=0.0), 4),
                    "damaged_area": round(damaged_area, 5),
                    "captured_at": utcnow(),
                },
                qos=1,
            )
            self._last_severity[index] = severity

    def _maybe_persist(
        self, index: int, frame: Frame, detections: list[Detection], severity: str, info
    ) -> None:
        decision = self.policy[index].decide(severity)
        if not decision.write or self.storage == "full":
            return
        self._write_event(
            index, frame, detections, severity, info, trigger=decision.trigger, note=decision.reason
        )

    def _write_event(
        self,
        index: int,
        frame: Frame,
        detections: list[Detection],
        severity: str,
        info,
        *,
        trigger: str,
        note: str | None = None,
        still: np.ndarray | None = None,
    ) -> int:
        image = still if still is not None else frame.bgr
        # Annotated at save time so an archived scan stands on its own — see
        # the module docstring in render.py.
        annotated = draw_detections(image, detections)

        captured_at = datetime.now(timezone.utc)
        relative_dir = Path(captured_at.strftime("%Y/%m/%d"))
        (self.media_root / relative_dir).mkdir(parents=True, exist_ok=True)
        stem = f"{captured_at.strftime('%H%M%S%f')[:-3]}_c{index}_{frame.seq}"

        image_rel = relative_dir / f"{stem}.jpg"
        thumb_rel = relative_dir / f"{stem}_thumb.jpg"
        quality = self.cfg.camera.jpeg_quality

        try:
            if self.storage != "ok":
                raise _NoRoom()
            # On the disk before the row that points at them is committed.
            write_durably(self.media_root / image_rel, encode_jpeg(annotated, quality), scratch=self.incoming)
            write_durably(
                self.media_root / thumb_rel,
                encode_jpeg(make_thumbnail(annotated, self.cfg.persist.thumb_width), quality),
                scratch=self.incoming,
            )
        except _NoRoom:
            image_rel = thumb_rel = None  # type: ignore[assignment]
            note = f"{note}; storage low, no photo" if note else "storage low, no photo"
        except Exception:  # noqa: BLE001
            self._report("media_write", "media_write_failed", f"frame {frame.seq}")
            image_rel = thumb_rel = None  # type: ignore[assignment]

        event_id = self.db.insert_event(
            site=self.site,
            camera=index,
            frame_seq=frame.seq,
            detections=detections,
            model_version=info.model_version,
            conf_threshold=self.cfg.inference.conf_threshold,
            iou_threshold=self.cfg.inference.iou_threshold,
            severity=severity,
            trigger=trigger,
            captured_at=captured_at.isoformat(timespec="milliseconds"),
            image_path=str(image_rel) if image_rel else None,
            thumb_path=str(thumb_rel) if thumb_rel else None,
            inference_ms=round(self._inference_ms, 2),
            preview_fps=round(self.preview_fps[index].fps, 2),
            inference_fps=round(self.inference_fps.fps, 2),
            synthetic=info.synthetic,
            note=note,
        )
        self._events_written += 1

        self.bus.publish(
            Topics.EVENT_PERSISTED,
            {
                "event_id": event_id,
                "camera": index,
                "captured_at": captured_at.isoformat(timespec="milliseconds"),
                "severity": severity,
                "detection_count": len(detections),
                "trigger": trigger,
                "image_path": str(image_rel) if image_rel else None,
                "thumb_path": str(thumb_rel) if thumb_rel else None,
            },
            qos=1,
        )
        return event_id

    # -- commands ----------------------------------------------------------

    def _on_command(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle a command from the web process."""
        if topic == Topics.CMD_INFERENCE:
            action = str(payload.get("action", "")).lower()
            if action == "pause":
                self._paused.set()
                log.info("inference paused")
            elif action == "resume":
                self._paused.clear()
                log.info("inference resumed")
            self.db.log_system(event=f"inference_{action}", source="web")

        elif topic == Topics.CMD_CAPTURE:
            # Queued rather than executed here: this runs on paho's network
            # thread, and a high-resolution still can take hundreds of
            # milliseconds. Blocking the network thread would stall the
            # keepalive and make the broker think the worker had died.
            with self._snapshot_lock:
                self._pending_snapshots.append(payload)
            log.info("snapshot queued (%s)", payload.get("request_id", "-"))

        elif topic == Topics.CMD_CONFIG:
            self._apply_config(payload)

        elif topic == Topics.CMD_CAMERA:
            self._switch_camera(payload)

        elif topic == Topics.CMD_SESSION:
            # Stop this session cleanly; main() then builds a new Worker, which
            # detects the camera and the AI HAT+ afresh. In-process rather than
            # via a systemd restart, whose start limit a few quick clicks would
            # exhaust.
            label = str(payload.get("label") or "").strip()
            request_next_label(self.cfg.sessions_root, label)
            log.info("new session requested%s", f" ({label})" if label else "")
            self.db.log_system(event="session_restart", source="web", detail=label or None)
            self.restart_requested = True
            self.request_stop()

    def _restore_thresholds(self) -> None:
        """Thresholds set from the dashboard outlast the session they were set in.

        A camera plugged in or out starts a new session; without this, an
        expert's tuning would silently fall back to the config file each time.
        """
        saved = {}
        for key in ("conf_threshold", "iou_threshold"):
            value = self.db.meta(key)
            if value is not None:
                saved[key] = float(value)
        if saved:
            self._apply_config(saved, persist=False)

    def _apply_config(self, payload: dict[str, Any], *, persist: bool = True) -> None:
        conf = payload.get("conf_threshold")
        iou = payload.get("iou_threshold")
        if hasattr(self.detector, "set_thresholds") and (conf is not None or iou is not None):
            self.detector.set_thresholds(  # type: ignore[attr-defined]
                conf=float(conf) if conf is not None else None,
                iou=float(iou) if iou is not None else None,
            )
        if conf is not None:
            self.cfg.inference._data["conf_threshold"] = float(conf)  # noqa: SLF001
        if iou is not None:
            self.cfg.inference._data["iou_threshold"] = float(iou)  # noqa: SLF001
        log.info("thresholds %s: conf=%s iou=%s", "updated" if persist else "restored", conf, iou)
        if not persist:
            return
        for key, value in (("conf_threshold", conf), ("iou_threshold", iou)):
            if value is not None:
                self.db.set_meta(key, str(float(value)))
        self.db.log_system(
            event="config_change", source="web", detail=f"conf={conf} iou={iou}"
        )

    def _switch_camera(self, payload: dict[str, Any]) -> None:
        """Turn one camera off or back on; the last one on stays on."""
        index, on = payload.get("camera"), payload.get("enabled")
        if not isinstance(index, int) or not isinstance(on, bool) or not 0 <= index < len(self.cameras):
            log.warning("ignoring camera command %r", payload)
            return
        if self.camera_on[index] == on:
            return
        if on and not self.camera_connected[index]:
            log.warning("camera %d is unplugged; it cannot be turned on", index)
            return
        if not on and self._active() == [index]:
            log.warning("camera %d is the only one on; leaving it on", index)
            return
        self.camera_on[index] = on
        self.latest[index].take(timeout=0)  # a frame from before the switch is stale
        state = "on" if on else "off"
        log.info("camera %d turned %s", index, state)
        self.db.log_system(event=f"camera_{state}", source="web", detail=f"camera {index}")

    def _active(self) -> list[int]:
        """The cameras the detector takes frames from: turned on and plugged in."""
        return [i for i, on in enumerate(self.camera_on) if on and self.camera_connected[i]]

    def _check_cameras(self) -> None:
        """Notice USB cameras unplugged or plugged in. Called about once a second.

        CSI cameras are not watched: a ribbon cable must not be plugged in or
        out with the power on.
        """
        present = _usb_present()
        if present is None:
            return
        for index, camera in enumerate(self.cameras):
            device = camera.info.detail.get("device")
            if camera.info.kind != "usb" or not self.camera_connected[index] or device in present:
                continue
            self.camera_connected[index] = False
            camera.stop()
            self.latest[index].take(timeout=0)
            log.warning("camera %d (%s) was unplugged", index, device)
            self.db.log_system(level="warn", event="camera_unplugged", source="worker", detail=f"camera {index} {device}")

        self._usb_seen &= present  # one that went away is new again when it is back
        arrived = present - self._usb_seen
        self._usb_arriving = {device: self._usb_arriving.get(device, 0) + 1 for device in arrived}
        ready = {device for device, seen in self._usb_arriving.items() if seen >= ARRIVAL_CHECKS}
        if not ready or self._stopping.is_set():
            return
        if time.monotonic() - Worker._camera_restart_at < CAMERA_RESTART_GAP_S:
            return  # a camera restarted the session less than a minute ago; wait
        Worker._camera_restart_at = time.monotonic()
        request_next_label(self.cfg.sessions_root, "camera-plugged-in")
        log.info("camera plugged in (%s); starting a new session to use it", ", ".join(sorted(ready)))
        self.db.log_system(event="camera_connected", source="worker", detail=", ".join(sorted(ready)))
        self.restart_requested = True
        self.request_stop()

    def _check_storage(self) -> None:
        """Below [persist] min_free_mb stop saving photos; below critical_free_mb
        stop saving anything. Live detection carries on, and nothing is deleted."""
        self._storage_checked_at = time.monotonic()
        try:
            free_mb = shutil.disk_usage(self.media_root).free / 1e6
        except OSError:
            return
        persist = self.cfg.persist
        if free_mb < float(persist.critical_free_mb):
            level = "full"
        elif free_mb < float(persist.min_free_mb):
            level = "low"
        else:
            level = "ok"
        self._disk_free_mb = int(free_mb)
        if level == self.storage:
            return
        self.storage = level
        log.warning("storage %s: %d MB free", level, free_mb)
        self._log_once(f"storage_{level}", f"{free_mb:.0f} MB free", level="info" if level == "ok" else "warn")

    def _reopen(self, index: int) -> None:
        """Stop and start a camera that went silent. Called from its capture thread."""
        camera = self.cameras[index]
        log.info("reopening camera %d", index)
        try:
            camera.stop()
            camera.start()
        except Exception:  # noqa: BLE001
            self._report(f"reopen:{index}", "camera_reopen_failed", f"camera {index}")

    def _log_once(self, event: str, detail: str, *, level: str = "warn") -> None:
        """A state change for system_log; never raises (the disk may be full)."""
        try:
            self.db.log_system(level=level, event=event, source="worker", detail=detail)
        except Exception:  # noqa: BLE001
            log.warning("could not log %s (%s)", event, detail)

    def _drain_snapshots(self) -> None:
        with self._snapshot_lock:
            pending, self._pending_snapshots = self._pending_snapshots, []
        for request in pending:
            try:
                self._take_snapshot(request)
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")

    def _take_snapshot(self, request: dict[str, Any]) -> None:
        """Capture a high-resolution still from every camera; one manual event each."""
        hi_res = bool(request.get("hi_res", True))
        for index, camera in enumerate(self.cameras):
            if index not in self._active():
                continue
            try:  # one camera failing must not lose the other's snapshot
                still = camera.capture_still() if hi_res else None
                frame = self.latest[index].take(timeout=1.0)
                if frame is None and index == 0:
                    frame = self._await_first_frame(timeout=2.0)
                if frame is None:
                    log.warning("snapshot requested but camera %d has no frame", index)
                    continue
                self._snapshot_one(index, frame, still, request)
            except Exception:  # noqa: BLE001
                log.exception("snapshot from camera %d failed", index)
                self.db.log_system(level="error", event="snapshot_failed", source="worker", detail=f"camera {index}")

    def _snapshot_one(
        self, index: int, frame: Frame, still: np.ndarray | None, request: dict[str, Any]
    ) -> None:
        if self.storage == "full":
            log.warning("snapshot from camera %d not saved: the SD card is full", index)
            return
        subject = still if still is not None else frame.bgr
        detections, _ = self._detect(subject)
        severity, _ = severity_from_config(detections, self.cfg)
        event_id = self._write_event(
            index,
            frame,
            detections,
            severity,
            self.detector.info,
            trigger="manual",
            note=f"manual snapshot {request.get('request_id', '')}".strip(),
            still=subject,
        )
        log.info("snapshot stored as event %d (%d detections)", event_id, len(detections))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _usb_present() -> set[str] | None:
    """Device paths of the USB cameras attached now, or None where sysfs cannot tell."""
    if not SYSFS_VIDEO.is_dir():
        return None
    return {camera.device for camera in usb_cameras()}


def wait_for_camera(cfg: Config, *, poll_s: float = 2.0, retry_s: float = 30.0) -> None:
    """Tell the dashboard no camera is connected, until the cameras attached change.

    Returns then, or, when a camera is attached but would not start, after
    ``retry_s`` to try it again. Replaces exiting and being restarted by
    systemd, which left the dashboard saying only "offline" and, through the
    target's dependencies, restarted the Wi-Fi access point each time.
    """
    bus = make_bus(cfg, "worker", with_will=True)
    bus.start(timeout=3.0)
    bus.publish_raw(Topics.STATUS_WORKER, STATUS_NO_CAMERA, qos=1, retain=True)
    log.info("waiting for a camera to be plugged in")
    before = {camera.describe() for camera in discover_cameras()}
    # A camera attached that would not start is tried again now and then. With
    # none attached only a change is worth it: each try reloads the model.
    deadline = time.monotonic() + retry_s if before else None
    while deadline is None or time.monotonic() < deadline:
        time.sleep(poll_s)
        if {camera.describe() for camera in discover_cameras()} != before:
            break
    bus.stop()  # no closing status: the next worker announces itself


def wait_after_error(cfg: Config, *, seconds: float = 30.0) -> None:
    """Tell the dashboard the worker hit a problem, then wait before trying again."""
    bus = make_bus(cfg, "worker", with_will=True)
    bus.start(timeout=3.0)
    bus.publish_raw(Topics.STATUS_WORKER, STATUS_ERROR, qos=1, retain=True)
    time.sleep(seconds)
    bus.stop()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    board = detect_board_info()
    if not board.supported:
        log.error("Pepper Watch cannot run on this machine: %s", board.reason)
        return 2
    log.info(
        "board: %s (%s, %s, %s MB) -> %s tier",
        board.model, board.family, board.cpu, board.mem_mb, board.tier,
    )
    # Never exits on a problem it can recover from: systemd would restart it
    # anyway, and the dashboard would only say "offline" without saying why.
    while True:
        cfg = load()
        try:
            worker = Worker(cfg)
        except CameraError as exc:
            log.error("%s", exc)
            wait_for_camera(cfg)
            continue
        except Exception:  # noqa: BLE001 — a corrupt model, a full disk, a driver error
            log.exception("the worker could not start; trying again in 30 s")
            wait_after_error(cfg)
            continue
        try:
            code = worker.run()
        except Exception:  # noqa: BLE001
            log.exception("the worker stopped on an error; starting again in 30 s")
            code = None
        # The session's handlers would otherwise swallow a stop request while waiting.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        if code is None:
            wait_after_error(cfg)
        elif code == EXIT_NO_FRAMES:
            wait_for_camera(cfg)
        elif not worker.restart_requested:
            return code
        else:
            log.info("starting a new session")


if __name__ == "__main__":
    raise SystemExit(main())
