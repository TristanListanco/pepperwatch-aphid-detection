"""Per-session measurement records, as CSV, for the results chapter.

A *session* is one run of the worker: it starts when the worker starts (or the
dashboard's "New session" button restarts it) and ends when it stops. Each gets
its own folder under ``[sessions].root``::

    <root>/<session_id>/
        session.json    hardware, accelerator, camera, model and settings
        inference.csv   one row per inference: stage timings, latency, fps, detections
        system.csv      one row per second: temperatures, clocks, power, energy, fps
        summary.json    means, percentiles, energy -- rewritten as the session runs
    <root>/sessions.csv one row per session, rebuilt from every summary.json

``sessions.csv`` is the table to start from when writing up: one line per run,
with the accelerator, camera and model beside the frame rates, latency
percentiles and energy, so CPU and AI HAT+ sessions compare directly.

Rows are buffered and flushed every ``flush_interval_s``; ``summary.json`` is
rewritten every ``summary_interval_s``. A session cut off by a power failure
therefore keeps all but the last few seconds of rows and a summary at most one
interval stale, and the next session start folds it into ``sessions.csv``.

Latency percentiles come from a fixed-size uniform reservoir sample, so memory
stays flat over an eight-hour session; ``inference.csv`` keeps every value if
exact figures are needed.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INFERENCE_COLUMNS = (
    "timestamp", "elapsed_s", "frame_seq",
    "preprocess_ms", "infer_ms", "postprocess_ms", "total_ms", "end_to_end_ms",
    "inference_fps", "preview_fps", "dropped_frames",
    "detections", "damage_boxes", "max_confidence", "damaged_area", "severity",
    "thermal_level",
)

SYSTEM_COLUMNS = (
    "timestamp", "elapsed_s",
    "cpu_temp_c", "hailo_temp_c", "cpu_percent", "cpu_freq_mhz", "mem_used_mb", "fan_rpm",
    "throttled_now", "throttled_flags",
    "ina219_voltage_v", "ina219_current_ma", "ina219_power_w",
    "pmic_power_w", "input_voltage_v", "power_w", "power_source", "energy_wh",
    "preview_fps", "inference_fps", "inference_ms", "dropped_frames",
    "thermal_level", "paused",
)

SUMMARY_COLUMNS = (
    "session_id", "label", "status", "started_at", "ended_at", "duration_s",
    "accelerator", "accelerator_reason", "profile", "hailo_arch", "hailo_runtime", "pcie_link",
    "camera_kind", "camera_model", "camera_device", "preview_width", "preview_height",
    "preview_fps_target",
    "detector", "model_file", "model_sha256", "input_size", "conf_threshold", "iou_threshold",
    "intra_op_threads", "synthetic",
    "board", "board_family", "board_tier", "cpu", "ram_mb", "os", "kernel", "python",
    "frames_captured", "inferences", "dropped_frames", "events_persisted",
    "inference_fps_mean", "preview_fps_mean",
    "preprocess_ms_mean", "infer_ms_mean", "infer_ms_p50", "infer_ms_p95",
    "postprocess_ms_mean", "total_ms_mean", "total_ms_p50", "total_ms_p95",
    "end_to_end_ms_mean", "end_to_end_ms_p95",
    "frames_with_damage", "detections_total",
    "cpu_temp_mean_c", "cpu_temp_max_c", "hailo_temp_max_c", "cpu_percent_mean",
    "throttled_s", "throttle_flags_seen", "thermal_backoff_s",
    "power_source", "power_mean_w", "power_max_w", "energy_wh",
    "ina219_power_mean_w", "pmic_power_mean_w", "fps_per_watt",
)

NEXT_LABEL_FILE = ".next_label"
_RESERVOIR = 20_000


class Reservoir:
    """A uniform random sample of a stream, for percentiles in bounded memory."""

    def __init__(self, size: int = _RESERVOIR, *, seed: int = 1734) -> None:
        self._size = size
        self._seen = 0
        self._values: list[float] = []
        self._random = random.Random(seed)

    def add(self, value: float) -> None:
        self._seen += 1
        if len(self._values) < self._size:
            self._values.append(value)
            return
        slot = self._random.randrange(self._seen)
        if slot < self._size:
            self._values[slot] = value

    def percentile(self, q: float) -> float | None:
        if not self._values:
            return None
        ordered = sorted(self._values)
        rank = (len(ordered) - 1) * q
        low, high = math.floor(rank), math.ceil(rank)
        return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass
class Mean:
    total: float = 0.0
    count: int = 0
    peak: float | None = None

    def add(self, value: float | None) -> None:
        if value is None:
            return
        self.total += value
        self.count += 1
        self.peak = value if self.peak is None else max(self.peak, value)

    @property
    def value(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class _Stats:
    inferences: int = 0
    frames_with_damage: int = 0
    detections_total: int = 0
    preprocess: Mean = field(default_factory=Mean)
    infer: Mean = field(default_factory=Mean)
    postprocess: Mean = field(default_factory=Mean)
    total: Mean = field(default_factory=Mean)
    end_to_end: Mean = field(default_factory=Mean)
    infer_sample: Reservoir = field(default_factory=Reservoir)
    total_sample: Reservoir = field(default_factory=Reservoir)
    e2e_sample: Reservoir = field(default_factory=Reservoir)
    inference_fps: Mean = field(default_factory=Mean)
    preview_fps: Mean = field(default_factory=Mean)
    cpu_temp: Mean = field(default_factory=Mean)
    hailo_temp: Mean = field(default_factory=Mean)
    cpu_percent: Mean = field(default_factory=Mean)
    power: Mean = field(default_factory=Mean)
    ina219: Mean = field(default_factory=Mean)
    pmic: Mean = field(default_factory=Mean)
    energy_j: float = 0.0
    power_sources: set = field(default_factory=set)
    throttled_s: float = 0.0
    backoff_s: float = 0.0
    flags_seen: int = 0


class SessionRecorder:
    """Writes one session's CSV files and keeps its running summary."""

    def __init__(
        self,
        root: Path,
        meta: dict[str, Any],
        *,
        flush_interval_s: float = 2.0,
        summary_interval_s: float = 15.0,
        now: datetime | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.started = now or datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self.label = take_next_label(self.root)

        accelerator = str(meta.get("accelerator", {}).get("profile") or "cpu")
        slug = _slug(self.label)
        self.session_id = f"{self.started:%Y%m%dT%H%M%SZ}_{accelerator}" + (f"_{slug}" if slug else "")
        self.directory = self.root / self.session_id
        self.directory.mkdir(parents=True, exist_ok=True)

        self.meta = {
            "session_id": self.session_id,
            "label": self.label,
            "started_at": _iso(self.started),
            **meta,
        }
        self._flush_interval = float(flush_interval_s)
        self._summary_interval = float(summary_interval_s)
        self._stats = _Stats()
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._last_summary = 0.0
        self._last_power_at: float | None = None
        self._last_system_at: float | None = None
        self._closed = False

        _write_json(self.directory / "session.json", self.meta)
        self._inference_file = (self.directory / "inference.csv").open("w", newline="", buffering=1 << 16)
        self._system_file = (self.directory / "system.csv").open("w", newline="", buffering=1 << 14)
        self._inference = csv.DictWriter(self._inference_file, INFERENCE_COLUMNS, extrasaction="ignore")
        self._system = csv.DictWriter(self._system_file, SYSTEM_COLUMNS, extrasaction="ignore")
        self._inference.writeheader()
        self._system.writeheader()

        self.write_summary(status="running")
        rebuild_index(self.root)
        log.info("session %s recording to %s", self.session_id, self.directory)

    # -- rows ------------------------------------------------------------------

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def record_inference(self, row: dict[str, Any]) -> None:
        """One inference. Keys follow :data:`INFERENCE_COLUMNS`."""
        stats = self._stats
        with self._lock:
            if self._closed:
                return
            self._inference.writerow(_format(self._stamp(row)))
            stats.inferences += 1
            stats.detections_total += int(row.get("detections") or 0)
            if int(row.get("damage_boxes") or 0) > 0:
                stats.frames_with_damage += 1
            for key, mean in (
                ("preprocess_ms", stats.preprocess),
                ("infer_ms", stats.infer),
                ("postprocess_ms", stats.postprocess),
                ("total_ms", stats.total),
                ("end_to_end_ms", stats.end_to_end),
            ):
                mean.add(_num(row.get(key)))
            for key, sample in (
                ("infer_ms", stats.infer_sample),
                ("total_ms", stats.total_sample),
                ("end_to_end_ms", stats.e2e_sample),
            ):
                value = _num(row.get(key))
                if value is not None:
                    sample.add(value)
        self._maybe_flush()

    def record_system(self, row: dict[str, Any]) -> None:
        """One periodic system sample. Keys follow :data:`SYSTEM_COLUMNS`.

        Energy is integrated here from consecutive power readings, so the row
        carries the running total and ``summary.json`` the final one.
        """
        stats = self._stats
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            power = _num(row.get("power_w"))
            if power is not None:
                if self._last_power_at is not None:
                    # Cap the step so a stall (or a suspended Pi) cannot book
                    # an hour of energy against a single reading.
                    stats.energy_j += power * min(now - self._last_power_at, 5.0)
                self._last_power_at = now
                stats.power.add(power)
                if row.get("power_source"):
                    stats.power_sources.add(str(row["power_source"]))
            stats.ina219.add(_num(row.get("ina219_power_w")))
            stats.pmic.add(_num(row.get("pmic_power_w")))
            stats.cpu_temp.add(_num(row.get("cpu_temp_c")))
            stats.hailo_temp.add(_num(row.get("hailo_temp_c")))
            stats.cpu_percent.add(_num(row.get("cpu_percent")))
            stats.preview_fps.add(_num(row.get("preview_fps")))
            if not row.get("paused"):
                fps = _num(row.get("inference_fps"))
                if fps:
                    stats.inference_fps.add(fps)

            step = min(now - self._last_system_at, 5.0) if self._last_system_at else 0.0
            self._last_system_at = now
            if row.get("throttled_now"):
                stats.throttled_s += step
            if int(row.get("thermal_level") or 0) > 0:
                stats.backoff_s += step
            stats.flags_seen |= _flags(row.get("throttled_flags"))

            self._system.writerow(
                _format(self._stamp({**row, "energy_wh": stats.energy_j / 3600.0}))
            )
        self._maybe_flush()

    def update_counters(self, **counters: int) -> None:
        """Totals owned by the worker: frames captured, dropped, events saved."""
        with self._lock:
            self._counters.update({k: int(v) for k, v in counters.items()})

    # -- summary ---------------------------------------------------------------

    def summary(self, *, status: str = "running") -> dict[str, Any]:
        """Flat summary, keyed by :data:`SUMMARY_COLUMNS`."""
        meta, s = self.meta, self._stats
        with self._lock:
            accel = meta.get("accelerator", {})
            hailo = accel.get("hailo") or {}
            camera = meta.get("camera", {})
            detector = meta.get("detector", {})
            host = meta.get("host", {})
            settings = meta.get("settings", {})
            power_mean = s.power.value
            fps_mean = s.inference_fps.value
            sources = sorted(s.power_sources)

            summary = {
                "session_id": self.session_id,
                "label": self.label,
                "status": status,
                "started_at": meta["started_at"],
                "ended_at": _iso(datetime.now(timezone.utc)) if status != "running" else None,
                "duration_s": round(self.elapsed_s(), 1),
                "accelerator": accel.get("name"),
                "accelerator_reason": accel.get("reason"),
                "profile": accel.get("profile"),
                "hailo_arch": hailo.get("arch") if accel.get("kind") == "hailo" else None,
                "hailo_runtime": hailo.get("runtime_version") if accel.get("kind") == "hailo" else None,
                "pcie_link": hailo.get("pcie_link") if accel.get("kind") == "hailo" else None,
                "camera_kind": camera.get("kind"),
                "camera_model": camera.get("model"),
                "camera_device": camera.get("device"),
                "preview_width": camera.get("width"),
                "preview_height": camera.get("height"),
                "preview_fps_target": settings.get("preview_fps"),
                "detector": detector.get("kind"),
                "model_file": detector.get("model_file"),
                "model_sha256": detector.get("model_sha256"),
                "input_size": detector.get("input_size"),
                "conf_threshold": settings.get("conf_threshold"),
                "iou_threshold": settings.get("iou_threshold"),
                "intra_op_threads": settings.get("intra_op_threads"),
                "synthetic": detector.get("synthetic"),
                "board": host.get("board"),
                "board_family": host.get("board_family"),
                "board_tier": host.get("board_tier"),
                "cpu": host.get("cpu"),
                "ram_mb": host.get("ram_mb"),
                "os": host.get("os"),
                "kernel": host.get("kernel"),
                "python": host.get("python"),
                "frames_captured": self._counters.get("frames_captured"),
                "inferences": s.inferences,
                "dropped_frames": self._counters.get("dropped_frames"),
                "events_persisted": self._counters.get("events_persisted"),
                "inference_fps_mean": _r(fps_mean, 2),
                "preview_fps_mean": _r(s.preview_fps.value, 2),
                "preprocess_ms_mean": _r(s.preprocess.value),
                "infer_ms_mean": _r(s.infer.value),
                "infer_ms_p50": _r(s.infer_sample.percentile(0.50)),
                "infer_ms_p95": _r(s.infer_sample.percentile(0.95)),
                "postprocess_ms_mean": _r(s.postprocess.value),
                "total_ms_mean": _r(s.total.value),
                "total_ms_p50": _r(s.total_sample.percentile(0.50)),
                "total_ms_p95": _r(s.total_sample.percentile(0.95)),
                "end_to_end_ms_mean": _r(s.end_to_end.value),
                "end_to_end_ms_p95": _r(s.e2e_sample.percentile(0.95)),
                "frames_with_damage": s.frames_with_damage,
                "detections_total": s.detections_total,
                "cpu_temp_mean_c": _r(s.cpu_temp.value, 1),
                "cpu_temp_max_c": _r(s.cpu_temp.peak, 1),
                "hailo_temp_max_c": _r(s.hailo_temp.peak, 1),
                "cpu_percent_mean": _r(s.cpu_percent.value, 1),
                "throttled_s": round(s.throttled_s, 1),
                "throttle_flags_seen": hex(s.flags_seen),
                "thermal_backoff_s": round(s.backoff_s, 1),
                "power_source": "+".join(sources) if sources else None,
                "power_mean_w": _r(power_mean),
                "power_max_w": _r(s.power.peak),
                "energy_wh": _r(s.energy_j / 3600.0, 6),
                "ina219_power_mean_w": _r(s.ina219.value),
                "pmic_power_mean_w": _r(s.pmic.value),
                "fps_per_watt": _r(fps_mean / power_mean, 3) if fps_mean and power_mean else None,
            }
        return summary

    def write_summary(self, *, status: str = "running") -> dict[str, Any]:
        summary = self.summary(status=status)
        _write_json(self.directory / "summary.json", summary)
        self._last_summary = time.monotonic()
        return summary

    def summary_due(self) -> bool:
        return time.monotonic() - self._last_summary >= self._summary_interval

    # -- lifecycle -------------------------------------------------------------

    def flush(self) -> None:
        with self._lock:
            for handle in (self._inference_file, self._system_file):
                if not handle.closed:
                    handle.flush()
            self._last_flush = time.monotonic()

    def close(self) -> dict[str, Any]:
        """Flush, write the final summary, and rebuild ``sessions.csv``."""
        self.flush()
        summary = self.write_summary(status="complete")
        with self._lock:
            self._closed = True
            for handle in (self._inference_file, self._system_file):
                handle.close()
        rebuild_index(self.root)
        log.info(
            "session %s closed: %s inferences, %s fps mean, %s Wh",
            self.session_id, summary["inferences"], summary["inference_fps_mean"], summary["energy_wh"],
        )
        return summary

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= self._flush_interval:
            self.flush()

    def _stamp(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "timestamp": _iso(datetime.now(timezone.utc)),
            "elapsed_s": round(self.elapsed_s(), 3),
            **row,
        }


# -- the cross-session index ---------------------------------------------------


def list_sessions(root: Path) -> list[dict[str, Any]]:
    """Every session's summary, newest first."""
    summaries = []
    for path in Path(root).glob("*/summary.json"):
        try:
            summaries.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            log.warning("unreadable %s", path)
    summaries.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return summaries


def rebuild_index(root: Path) -> Path:
    """Rewrite ``sessions.csv`` from every ``summary.json``, oldest first.

    Rebuilt rather than appended, so a session that died without closing still
    appears (with status "running" and its last periodic summary), and deleting
    a session's folder removes it from the table.
    """
    root = Path(root)
    target = root / "sessions.csv"
    rows = list(reversed(list_sessions(root)))
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_format(row))
    os.replace(temporary, target)
    return target


def request_next_label(root: Path, label: str) -> None:
    """Name the next session. Read and cleared when that session starts."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / NEXT_LABEL_FILE).write_text(label.strip()[:80])


def take_next_label(root: Path) -> str:
    path = Path(root) / NEXT_LABEL_FILE
    try:
        label = path.read_text().strip()
    except OSError:
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    return label


# -- helpers -------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flags(value: Any) -> int:
    """Throttle bits, given as an int or as vcgencmd's hex string."""
    if value in (None, ""):
        return 0
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return 0


def _r(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def csv_safe(value: Any) -> Any:
    """Neutralise text a spreadsheet would run as a formula (=, +, -, @).

    Labels and model names are free text; opened in Excel, a cell starting
    with "=" is executed. A leading apostrophe makes it plain text.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _format(row: dict[str, Any]) -> dict[str, Any]:
    """CSV-friendly: floats rounded, None as an empty cell, bools as 0/1.

    Energy keeps six decimals: a one-minute benchmark at 5 W is 0.08 Wh, and
    four decimals would leave that with two significant figures.
    """
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = ""
        elif isinstance(value, bool):
            out[key] = int(value)
        elif isinstance(value, float):
            digits = 6 if key.endswith("_wh") else 4
            out[key] = round(value, digits) if math.isfinite(value) else ""
        else:
            out[key] = csv_safe(value)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(temporary, path)
