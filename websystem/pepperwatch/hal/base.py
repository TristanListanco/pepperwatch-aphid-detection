"""Hardware abstraction contracts.

Two implementations satisfy these: ``hal.mock`` for macOS development, which
replays the Naawan field photographs, and ``hal.pi`` for the Raspberry Pi 5,
which drives a real camera and reads real sensors. Everything above the HAL —
the worker, the detector, the database, the dashboard — is written against
these types alone and never learns which platform it is on.

One field deserves emphasis: :attr:`Telemetry.synthetic`. Mock telemetry is
plausible, smooth, and completely made up. It propagates through MQTT, the
System Health Monitor, and ``system_log``, so every sample carries a flag
saying whether it came from real silicon. Chapter IV must never accidentally
report a synthetic thermal curve as a measurement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured frame.

    ``bgr`` is OpenCV's native channel order, kept all the way to the point of
    JPEG encoding so no conversion happens more than once per frame.
    """

    seq: int
    ts: float
    bgr: np.ndarray

    @property
    def width(self) -> int:
        return int(self.bgr.shape[1])

    @property
    def height(self) -> int:
        return int(self.bgr.shape[0])


@dataclass(frozen=True, slots=True)
class Telemetry:
    """A hardware health sample.

    Every optional field is ``None`` when the platform genuinely cannot measure
    it, which is different from zero. The System Health Monitor renders "—" for
    ``None`` rather than a misleading 0.0.
    """

    ts: float
    synthetic: bool
    cpu_temp_c: float | None = None
    cpu_percent: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None
    disk_free_mb: float | None = None
    voltage_v: float | None = None
    current_ma: float | None = None
    power_w: float | None = None
    battery_pct: float | None = None
    throttled: bool = False
    throttle_detail: str = ""

    @property
    def mem_percent(self) -> float | None:
        if self.mem_used_mb is None or not self.mem_total_mb:
            return None
        return 100.0 * self.mem_used_mb / self.mem_total_mb

    def as_dict(self) -> dict[str, Any]:
        """Flat JSON-safe mapping for MQTT, SSE, and system_log."""
        return {
            "ts": self.ts,
            "synthetic": self.synthetic,
            "cpu_temp_c": self.cpu_temp_c,
            "cpu_percent": self.cpu_percent,
            "mem_used_mb": self.mem_used_mb,
            "mem_total_mb": self.mem_total_mb,
            "mem_percent": self.mem_percent,
            "disk_free_mb": self.disk_free_mb,
            "voltage_v": self.voltage_v,
            "current_ma": self.current_ma,
            "power_w": self.power_w,
            "battery_pct": self.battery_pct,
            "throttled": self.throttled,
            "throttle_detail": self.throttle_detail,
        }


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """What a camera reports about itself, for /api/health and `pw doctor`."""

    kind: str
    description: str
    width: int
    height: int
    fps: float
    synthetic: bool
    detail: dict[str, Any] = field(default_factory=dict)


class CameraError(RuntimeError):
    """Camera could not be opened, or died mid-run."""


class CameraSource(ABC):
    """A source of frames.

    Contract notes that matter to the worker:

    * :meth:`read` is **non-blocking and returns the newest frame available**,
      never a queued backlog. A detector that runs slower than the camera must
      fall behind in time, not in memory, so stale frames are dropped rather
      than buffered.
    * :meth:`read` returns ``None`` when no new frame has arrived since the
      last call. That is normal pacing, not an error.
    * ``seq`` increases monotonically across the life of the source and is the
      identity used to correlate a frame with the detections drawn over it.
    """

    @abstractmethod
    def start(self) -> None:
        """Open the device. Idempotent."""

    @abstractmethod
    def read(self) -> Frame | None:
        """Newest frame, or ``None`` if nothing new. Must not block."""

    @abstractmethod
    def capture_still(self) -> np.ndarray:
        """A single high-resolution BGR still for a manual snapshot.

        May briefly interrupt the preview stream; the worker serialises this
        against the capture loop.
        """

    @abstractmethod
    def stop(self) -> None:
        """Release the device. Idempotent, and safe to call after a failure."""

    @property
    @abstractmethod
    def info(self) -> CameraInfo:
        """Describe the open device."""

    def __enter__(self) -> "CameraSource":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class TelemetrySource(ABC):
    """A source of hardware health samples."""

    @abstractmethod
    def sample(self) -> Telemetry:
        """Read the sensors once. Must not raise; degrade to ``None`` fields."""

    def close(self) -> None:
        """Release any bus handles. Default is a no-op."""
