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
from .frames import make_writer
from .hal import CameraSource, Frame, get_telemetry, open_camera
from .models import Detection, severity_rank
from .mqtt_bus import STATUS_OFFLINE, STATUS_ONLINE, MqttBus, Topics, make_bus
from .render import draw_detections, encode_jpeg, make_thumbnail
from .session import SessionRecorder, request_next_label
from .thermal import ThermalGovernor

log = logging.getLogger(__name__)


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

    def __init__(self, base_cfg: Config) -> None:
        self.base_cfg = base_cfg
        self.cfg, self.accelerator, self.detector = start_session_hardware(base_cfg)
        cfg = self.cfg
        self.site = cfg.site.name

        try:
            self.camera: CameraSource
            self.camera, self.cameras_found = open_camera(cfg)
        except Exception:
            self.detector.close()
            raise
        self.telemetry = get_telemetry(cfg)
        self.db = Database(cfg)
        self.frames = make_writer(cfg)
        self.bus: MqttBus = make_bus(cfg, "worker", with_will=True)

        self.governor = ThermalGovernor()
        self.latest = LatestFrame()
        self.preview_fps = FpsMeter()
        self.inference_fps = FpsMeter()

        self.policy = PersistPolicy(
            heartbeat_detected_s=cfg.persist.heartbeat_detected_s,
            heartbeat_clean_s=cfg.persist.heartbeat_clean_s,
            on_state_change=cfg.persist.on_state_change,
            state_change_debounce_s=cfg.persist.state_change_debounce_s,
            min_write_interval_s=cfg.persist.min_write_interval_s,
        )

        self._stopping = threading.Event()
        self._paused = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._pending_snapshots: list[dict[str, Any]] = []

        self._last_telemetry: dict[str, Any] = {}
        self._last_thermal_level = 0
        self._last_severity = "none"
        self._inference_ms = 0.0
        self._events_written = 0
        self._frames_captured = 0
        self.recorder: SessionRecorder | None = None
        self.restart_requested = False

        self.media_root = cfg.media_root
        self.media_root.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        """Start everything and block until stopped. Returns an exit code."""
        info = self.detector.info
        log.info(
            "worker starting: site=%s accelerator=%s detector=%s (%s) camera=%s",
            self.site, self.accelerator.name, info.kind, info.model_version,
            self.camera.info.description,
        )
        if info.synthetic:
            log.warning(
                "detector is synthetic (%s) — events will be flagged and must not "
                "be reported as measurements", info.model_version,
            )

        self.db.initialise(writer="worker")
        self.bus.start(timeout=3.0)
        self.bus.subscribe(Topics.COMMANDS, self._on_command, qos=1)
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

        self.db.log_system(
            event="startup",
            source="worker",
            detail=(
                f"detector={info.kind}:{info.model_version} accelerator={self.accelerator.name} "
                f"camera={self.camera.info.description}"
            ),
        )

        # Warm up before measuring anything: the first ONNX call allocates
        # arenas and picks kernels, and that cost would otherwise land in the
        # first reported frame and skew every benchmark.
        primer = self._await_first_frame()
        if primer is None:
            log.error("no frame arrived from the camera; aborting")
            return 1
        self.detector.warmup(primer.bgr, rounds=2)

        # Recording starts after warm-up, so first-call allocation does not
        # land in the session's latency figures.
        self.recorder = SessionRecorder(
            self.cfg.sessions_root,
            self._session_meta(),
            flush_interval_s=self.cfg.sessions.flush_interval_s,
            summary_interval_s=self.cfg.sessions.summary_interval_s,
        )
        self.bus.publish(
            Topics.STATUS_SESSION,
            {**self.recorder.meta, "directory": str(self.recorder.directory)},
            qos=1,
            retain=True,
        )

        threads = [
            threading.Thread(target=self._capture_loop, name="capture", daemon=True),
            threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            self._inference_loop()
        finally:
            self.shutdown()
        return 0

    def _await_first_frame(self, timeout: float = 10.0) -> Frame | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.camera.read()
            if frame is not None:
                return frame
            time.sleep(0.01)
        return None

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
        self.bus.stop(final_status=(Topics.STATUS_WORKER, STATUS_OFFLINE))
        self.camera.stop()
        self.telemetry.close()
        self.detector.close()
        self.frames.close()
        self.db.close()

    # -- threads -----------------------------------------------------------

    def _capture_loop(self) -> None:
        """Run the camera flat out, publishing the clean preview as we go."""
        quality = self.cfg.camera.jpeg_quality
        while not self._stopping.is_set():
            try:
                frame = self.camera.read()
            except Exception:  # noqa: BLE001
                log.exception("camera read failed")
                self.db.log_system(level="error", event="camera_error", source="worker")
                time.sleep(0.5)
                continue

            if frame is None:
                time.sleep(0.002)
                continue

            self.preview_fps.mark()
            self._frames_captured += 1

            # Published CLEAN. The browser draws boxes on a canvas over this,
            # which keeps encode cost off the inference budget and lets the
            # overlay be toggled and scaled per device.
            try:
                self.frames.publish(
                    encode_jpeg(frame.bgr, quality),
                    seq=frame.seq,
                    preview_fps=self.preview_fps.fps,
                    inference_fps=self.inference_fps.fps,
                )
            except Exception:  # noqa: BLE001
                log.exception("frame publish failed")

            self.latest.put(frame)

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

            payload = sample.as_dict()
            payload["hailo_temp_c"] = chip_temperature() if chip_temperature else None
            payload["accelerator"] = self.accelerator.name
            self._last_telemetry = payload

            state = self.governor.update(sample.cpu_temp_c)
            payload["thermal_level"] = state.level
            payload["thermal_multiplier"] = state.multiplier
            payload["backing_off"] = state.backing_off
            self._record_system(sample, payload, state.level)

            self.bus.publish(Topics.TELEMETRY_SYSTEM, payload, qos=0)
            self.bus.publish(
                Topics.TELEMETRY_PIPELINE,
                {
                    "preview_fps": round(self.preview_fps.fps, 2),
                    "inference_fps": round(self.inference_fps.fps, 2),
                    "inference_ms": round(self._inference_ms, 2),
                    "dropped": self.latest.dropped,
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

            self._stopping.wait(timeout=min(period, interval))

    def _record_system(self, sample, payload: dict[str, Any], thermal_level: int) -> None:
        if self.recorder is None:
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
                "preview_fps": round(self.preview_fps.fps, 2),
                "inference_fps": round(self.inference_fps.fps, 2),
                "inference_ms": round(self._inference_ms, 2),
                "dropped_frames": self.latest.dropped,
                "thermal_level": thermal_level,
                "paused": self._paused.is_set(),
            }
        )

    def _counters(self) -> dict[str, int]:
        return {
            "frames_captured": self._frames_captured,
            "dropped_frames": self.latest.dropped,
            "events_persisted": self._events_written,
        }

    def _inference_loop(self) -> None:
        """Detect on the newest frame available, forever."""
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        info = self.detector.info
        base_interval = 1.0 / max(self.cfg.inference.target_fps, 1)

        while not self._stopping.is_set():
            self._drain_snapshots()

            if self._paused.is_set():
                self._stopping.wait(timeout=0.2)
                continue

            frame = self.latest.take(timeout=0.5)
            if frame is None:
                continue

            began = time.perf_counter()
            try:
                detections = self.detector.detect(frame.bgr)
            except Exception:  # noqa: BLE001
                log.exception("inference failed")
                self.db.log_system(level="error", event="inference_error", source="worker")
                continue
            self._inference_ms = (time.perf_counter() - began) * 1000.0
            self.inference_fps.mark()

            severity, damaged_area = severity_from_config(detections, self.cfg)
            self._record_inference(frame, detections, severity, damaged_area)
            self._publish_detections(frame, detections, severity, damaged_area)
            self._maybe_persist(frame, detections, severity, info)

            # The governor stretches this interval when hot. It does not touch
            # the capture loop, so the preview stays smooth while backing off.
            state = self.governor.update(self._last_telemetry.get("cpu_temp_c"))
            if state.multiplier > 1.0:
                self._stopping.wait(timeout=base_interval * (state.multiplier - 1.0))

    # -- publishing and persistence ---------------------------------------

    def _record_inference(
        self, frame: Frame, detections: list[Detection], severity: str, damaged_area: float
    ) -> None:
        if self.recorder is None:
            return
        timings = self.detector.timings
        damaged = [d for d in detections if d.class_name != "healthy"]
        self.recorder.record_inference(
            {
                "frame_seq": frame.seq,
                "preprocess_ms": timings.preprocess_ms,
                "infer_ms": timings.infer_ms,
                "postprocess_ms": timings.postprocess_ms,
                "total_ms": timings.total_ms,
                # Capture to result: queueing in the one-slot buffer plus the
                # whole detect call. What an observer of the live view waits.
                "end_to_end_ms": (time.time() - frame.ts) * 1000.0,
                "inference_fps": self.inference_fps.fps,
                "preview_fps": self.preview_fps.fps,
                "dropped_frames": self.latest.dropped,
                "detections": len(detections),
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
        self, frame: Frame, detections: list[Detection], severity: str, damaged_area: float
    ) -> None:
        self.bus.publish(
            Topics.DETECT_FRAME,
            {
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
        if severity != self._last_severity:
            damaged = [d for d in detections if d.class_name != "healthy"]
            self.bus.publish(
                Topics.DETECT_FLAG,
                {
                    "severity": severity,
                    "previous": self._last_severity,
                    "count": len(damaged),
                    "max_confidence": round(max((d.confidence for d in damaged), default=0.0), 4),
                    "damaged_area": round(damaged_area, 5),
                    "captured_at": utcnow(),
                },
                qos=1,
            )
            self._last_severity = severity

    def _maybe_persist(
        self, frame: Frame, detections: list[Detection], severity: str, info
    ) -> None:
        decision = self.policy.decide(severity)
        if not decision.write:
            return
        self._write_event(frame, detections, severity, info, trigger=decision.trigger, note=decision.reason)

    def _write_event(
        self,
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
        stem = f"{captured_at.strftime('%H%M%S%f')[:-3]}_{frame.seq}"

        image_rel = relative_dir / f"{stem}.jpg"
        thumb_rel = relative_dir / f"{stem}_thumb.jpg"
        quality = self.cfg.camera.jpeg_quality

        try:
            (self.media_root / image_rel).write_bytes(encode_jpeg(annotated, quality))
            (self.media_root / thumb_rel).write_bytes(
                encode_jpeg(make_thumbnail(annotated, self.cfg.persist.thumb_width), quality)
            )
        except Exception:  # noqa: BLE001
            log.exception("could not write media for frame %d", frame.seq)
            image_rel = thumb_rel = None  # type: ignore[assignment]

        event_id = self.db.insert_event(
            site=self.site,
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
            preview_fps=round(self.preview_fps.fps, 2),
            inference_fps=round(self.inference_fps.fps, 2),
            synthetic=info.synthetic,
            note=note,
        )
        self._events_written += 1

        self.bus.publish(
            Topics.EVENT_PERSISTED,
            {
                "event_id": event_id,
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

    def _apply_config(self, payload: dict[str, Any]) -> None:
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
        log.info("thresholds updated: conf=%s iou=%s", conf, iou)
        self.db.log_system(
            event="config_change", source="web", detail=f"conf={conf} iou={iou}"
        )

    def _drain_snapshots(self) -> None:
        with self._snapshot_lock:
            pending, self._pending_snapshots = self._pending_snapshots, []
        for request in pending:
            try:
                self._take_snapshot(request)
            except Exception:  # noqa: BLE001
                log.exception("snapshot failed")

    def _take_snapshot(self, request: dict[str, Any]) -> None:
        """Capture a high-resolution still and persist it as a manual event."""
        hi_res = bool(request.get("hi_res", True))
        still = self.camera.capture_still() if hi_res else None
        frame = self.latest.take(timeout=1.0)
        if frame is None:
            frame = self._await_first_frame(timeout=2.0)
        if frame is None:
            log.warning("snapshot requested but no frame available")
            return

        subject = still if still is not None else frame.bgr
        detections = self.detector.detect(subject)
        severity, _ = severity_from_config(detections, self.cfg)
        event_id = self._write_event(
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
    while True:
        worker = Worker(load())
        code = worker.run()
        if code != 0 or not worker.restart_requested:
            return code
        log.info("starting a new session")


if __name__ == "__main__":
    raise SystemExit(main())
