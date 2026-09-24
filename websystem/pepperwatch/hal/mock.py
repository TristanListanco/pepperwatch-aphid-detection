"""macOS development implementations of the HAL.

:class:`MockCamera` replays the Naawan field photographs built by
``scripts/prepare_mock_frames.py``, pacing them like a real camera and varying
exposure slightly so downstream code meets the kind of lighting spread it will
see in the field. :class:`MockTelemetry` fabricates a plausible thermal and
solar day.

Every telemetry sample this module produces is flagged ``synthetic=True``. That
flag rides through MQTT, the health panel, and ``system_log``, so a synthetic
thermal curve can never be mistaken for a measurement in Chapter IV.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
import psutil

from .base import CameraError, CameraInfo, CameraSource, Frame, Telemetry, TelemetrySource

log = logging.getLogger(__name__)

# How many decoded preview frames to keep in memory. The cache is a loop, so a
# small LRU turns every pass after the first into pure memory reads.
_DECODE_CACHE = 48


class MockCamera(CameraSource):
    """Replays the prepared field-photo cache at a configured frame rate.

    Pacing matters: :meth:`read` returns ``None`` until the next frame is due,
    exactly as a real camera does between exposures. Code that busy-loops on
    ``read()`` therefore behaves the same here as on the Pi, and the worker's
    frame-drop logic gets genuinely exercised on the development machine.
    """

    def __init__(
        self,
        cache_dir: Path,
        width: int,
        height: int,
        fps: float,
        *,
        vary_lighting: bool = True,
        hold: bool = False,
        seed: int = 1734,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._width = int(width)
        self._height = int(height)
        self._fps = float(fps)
        self._interval = 1.0 / self._fps if self._fps > 0 else 0.0
        self._vary_lighting = vary_lighting
        # hold=True freezes on one frame, which is what the overlay-alignment
        # and golden-image tests need to be deterministic.
        self._hold = hold
        self._rng = random.Random(seed)

        self._preview_paths: list[Path] = []
        self._still_paths: list[Path] = []
        self._decoded: OrderedDict[int, np.ndarray] = OrderedDict()
        self._index = 0
        self._seq = 0
        self._next_due = 0.0
        self._started = False
        self._lock = threading.Lock()
        self._manifest: dict[str, object] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._preview_paths = sorted((self._cache_dir / "preview").glob("*.jpg"))
            self._still_paths = sorted((self._cache_dir / "still").glob("*.jpg"))
            if not self._preview_paths:
                raise CameraError(
                    f"no mock frames in {self._cache_dir / 'preview'} — "
                    "run: python scripts/prepare_mock_frames.py"
                )

            manifest_path = self._cache_dir / "manifest.json"
            if manifest_path.exists():
                self._manifest = json.loads(manifest_path.read_text())
                built = self._manifest.get("preview_size")
                if built and list(built) != [self._width, self._height]:
                    # Not fatal — we resize on decode — but it costs CPU per
                    # frame and silently skews any benchmark, so say so.
                    log.warning(
                        "mock cache was built at %sx%s but preview is %sx%s; "
                        "rebuild with --force to avoid per-frame resizing",
                        built[0], built[1], self._width, self._height,
                    )

            self._next_due = time.monotonic()
            self._started = True
            log.info(
                "mock camera: %d frames from %s at %.1f fps",
                len(self._preview_paths), self._cache_dir, self._fps,
            )

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._decoded.clear()

    # -- capture -----------------------------------------------------------

    def read(self) -> Frame | None:
        if not self._started:
            raise CameraError("read() before start()")

        now = time.monotonic()
        if now < self._next_due:
            return None

        # Advance the schedule rather than resetting it, so a slow consumer
        # sees frames dropped instead of the whole stream sliding late.
        self._next_due += self._interval
        if self._next_due < now:
            self._next_due = now + self._interval

        with self._lock:
            index = self._index
            if not self._hold:
                self._index = (self._index + 1) % len(self._preview_paths)
            self._seq += 1
            seq = self._seq

        bgr = self._decode(index)
        if self._vary_lighting and not self._hold:
            bgr = self._apply_lighting(bgr, seq)
        return Frame(seq=seq, ts=time.time(), bgr=bgr)

    def capture_still(self) -> np.ndarray:
        """Return the high-resolution counterpart of the current frame."""
        if not self._started:
            raise CameraError("capture_still() before start()")
        with self._lock:
            # _index has already advanced past the frame read() last returned.
            index = (self._index - 1) % len(self._preview_paths)
        pool = self._still_paths or self._preview_paths
        image = cv2.imread(str(pool[index % len(pool)]), cv2.IMREAD_COLOR)
        if image is None:
            raise CameraError(f"could not decode still {pool[index % len(pool)]}")
        return image

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(
            kind="mock",
            description=f"Naawan field-photo replay ({len(self._preview_paths)} frames)",
            width=self._width,
            height=self._height,
            fps=self._fps,
            synthetic=True,
            detail={
                "cache_dir": str(self._cache_dir),
                "origin": self._manifest.get("origin", "unknown"),
                "built_at": self._manifest.get("built_at", "unknown"),
                "hold": self._hold,
            },
        )

    # -- internals ---------------------------------------------------------

    def _decode(self, index: int) -> np.ndarray:
        cached = self._decoded.get(index)
        if cached is not None:
            self._decoded.move_to_end(index)
            return cached.copy()

        path = self._preview_paths[index]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise CameraError(f"could not decode {path}")
        if image.shape[1] != self._width or image.shape[0] != self._height:
            image = cv2.resize(image, (self._width, self._height), interpolation=cv2.INTER_AREA)

        self._decoded[index] = image
        if len(self._decoded) > _DECODE_CACHE:
            self._decoded.popitem(last=False)
        return image.copy()

    def _apply_lighting(self, bgr: np.ndarray, seq: int) -> np.ndarray:
        """Nudge brightness and contrast on a slow cycle.

        Field light drifts as cloud passes; a perfectly stable replay would let
        a brittle fixed-threshold assumption survive development and fail in
        Naawan. The cycle is deterministic in ``seq`` so runs stay comparable.
        """
        phase = (seq % 240) / 240.0 * 2.0 * math.pi
        gain = 1.0 + 0.10 * math.sin(phase)          # +/-10% contrast
        bias = 12.0 * math.sin(phase * 0.5)          # +/-12 levels brightness
        return cv2.convertScaleAbs(bgr, alpha=gain, beta=bias)


class MockTelemetry(TelemetrySource):
    """Fabricates a plausible thermal and solar day.

    CPU percent and memory are read from the host for real — they respond to
    actual load, which makes the health panel useful while developing. Thermal,
    power, and battery are modelled, because a MacBook has no 85 °C throttle
    point, no INA219, and no solar panel.
    """

    # Bracket the Pi 5's behaviour: idle around 45 °C, loaded high 60s/low 70s,
    # soft throttle at 80 °C. The model deliberately approaches but does not
    # cross 80 °C, so the thermal governor's backoff path is reachable in
    # development by forcing a temperature, not by waiting for one.
    IDLE_C = 45.0
    LOADED_C = 72.0
    THROTTLE_C = 80.0

    def __init__(self, *, seed: int = 1734, day_length_s: float = 8 * 3600) -> None:
        self._rng = random.Random(seed)
        self._started = time.monotonic()
        self._day_length = float(day_length_s)
        self._temp = self.IDLE_C
        self._disk_root = Path.cwd().anchor or "/"

    def sample(self) -> Telemetry:
        now = time.monotonic()
        elapsed = now - self._started

        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        try:
            disk_free_mb = psutil.disk_usage(self._disk_root).free / 1e6
        except OSError:
            disk_free_mb = None

        # First-order thermal lag toward a load-dependent target, so the curve
        # rises and falls smoothly the way a heatsink actually behaves rather
        # than tracking CPU instantaneously.
        target = self.IDLE_C + (self.LOADED_C - self.IDLE_C) * (cpu_percent / 100.0)
        self._temp += (target - self._temp) * 0.15
        cpu_temp_c = round(self._temp + self._rng.uniform(-0.4, 0.4), 1)

        # A half-sine over the configured daylight window: the panel wakes, peaks
        # at solar noon, and fades. Battery tracks the integral loosely.
        day_phase = (elapsed % self._day_length) / self._day_length
        solar = max(0.0, math.sin(day_phase * math.pi))
        voltage_v = round(12.2 + 1.4 * solar + self._rng.uniform(-0.05, 0.05), 2)
        current_ma = round(420.0 + 260.0 * (cpu_percent / 100.0) + self._rng.uniform(-15, 15), 1)
        power_w = round(voltage_v * current_ma / 1000.0, 2)
        battery_pct = round(min(100.0, 62.0 + 34.0 * solar), 1)

        return Telemetry(
            ts=time.time(),
            synthetic=True,
            cpu_temp_c=cpu_temp_c,
            cpu_percent=round(cpu_percent, 1),
            mem_used_mb=round((memory.total - memory.available) / 1e6, 1),
            mem_total_mb=round(memory.total / 1e6, 1),
            disk_free_mb=round(disk_free_mb, 1) if disk_free_mb is not None else None,
            voltage_v=voltage_v,
            current_ma=current_ma,
            power_w=power_w,
            battery_pct=battery_pct,
            throttled=cpu_temp_c >= self.THROTTLE_C,
            throttle_detail="synthetic" if cpu_temp_c >= self.THROTTLE_C else "",
        )
