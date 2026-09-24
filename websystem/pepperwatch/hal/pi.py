"""Raspberry Pi 5 implementations of the HAL.

Two camera backends, because the manuscript specifies two different things:
Table 3.2 lists a MIPI CSI Camera Module, while Figure 3.3's hardware block
says "Web Camera". Both are supported, selected by ``[camera].kind``, so the
figure can be corrected either way without touching code.

Picamera2 is **apt-only**. ``sudo apt install python3-picamera2`` provides it
along with the libcamera bindings it needs; pip cannot build working ones. That
is why the venv must be created with ``--system-site-packages`` — and why
``python -m pepperwatch.doctor`` probes for the numpy ABI mismatch that flag
can introduce.

Telemetry reads real sensors and degrades to ``None`` for anything absent,
never to a plausible-looking zero. An INA219 over I²C supplies the voltage and
current that Chapter IV's power analysis needs; without one those fields stay
``None`` and the dashboard shows a dash rather than inventing a number.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import psutil

from ..config import Config
from .base import CameraError, CameraInfo, CameraSource, Frame, Telemetry, TelemetrySource

log = logging.getLogger(__name__)

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")

# vcgencmd get_throttled bit meanings. The "has occurred" bits latch since boot,
# which is what makes an 8-hour soak test reviewable after the fact.
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "arm frequency capped now",
    2: "currently throttled",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "arm frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


# -- cameras ---------------------------------------------------------------


class CsiCamera(CameraSource):
    """MIPI CSI camera via Picamera2, per manuscript Table 3.2 and §3.4.1.

    Configured with two streams: a ``lores`` stream sized for preview and
    inference, and a full-resolution ``main`` for snapshots. Requesting both up
    front means a manual high-resolution capture does not have to stop and
    reconfigure the camera, which would drop the live feed for a second or two
    each time.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._camera = None
        self._seq = 0
        self._started = False
        self._lock = threading.Lock()
        self._model = "unknown"

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            try:
                from picamera2 import Picamera2  # type: ignore[import-not-found]
            except ImportError as exc:
                raise CameraError(
                    "Picamera2 is not importable. It is apt-only:\n"
                    "  sudo apt install -y python3-picamera2\n"
                    "and the venv must be created with --system-site-packages.\n"
                    "Run `python -m pepperwatch.doctor` to diagnose.\n"
                    f"  ({exc})"
                ) from exc

            camera = Picamera2()
            self._model = str(camera.camera_properties.get("Model", "unknown"))

            config = camera.create_video_configuration(
                main={
                    "size": (self.cfg.camera.still_width, self.cfg.camera.still_height),
                    "format": "RGB888",
                },
                lores={
                    "size": (self.cfg.camera.preview_width, self.cfg.camera.preview_height),
                    "format": "RGB888",
                },
                # Only one buffered frame: the worker always wants the newest,
                # and a deeper queue would just add latency between what the
                # camera sees and what the boxes describe.
                buffer_count=2,
            )
            camera.configure(config)
            camera.start()
            # Auto exposure and white balance need a moment to settle, or the
            # first several frames come out badly exposed.
            time.sleep(1.5)

            self._camera = camera
            self._started = True
            log.info(
                "CSI camera started: %s, preview %dx%d, still %dx%d",
                self._model,
                self.cfg.camera.preview_width, self.cfg.camera.preview_height,
                self.cfg.camera.still_width, self.cfg.camera.still_height,
            )

    def read(self) -> Frame | None:
        if not self._started or self._camera is None:
            raise CameraError("read() before start()")
        try:
            array = self._camera.capture_array("lores")
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"CSI capture failed: {exc}") from exc

        self._seq += 1
        return Frame(seq=self._seq, ts=time.time(), bgr=_to_bgr(array, self.cfg.camera.rotate))

    def capture_still(self) -> np.ndarray:
        if not self._started or self._camera is None:
            raise CameraError("capture_still() before start()")
        array = self._camera.capture_array("main")
        return _to_bgr(array, self.cfg.camera.rotate)

    def stop(self) -> None:
        with self._lock:
            if self._camera is not None:
                try:
                    self._camera.stop()
                    self._camera.close()
                except Exception:  # noqa: BLE001
                    pass
                self._camera = None
            self._started = False

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(
            kind="csi",
            description=f"MIPI CSI via Picamera2 ({self._model})",
            width=self.cfg.camera.preview_width,
            height=self.cfg.camera.preview_height,
            fps=float(self.cfg.camera.preview_fps),
            synthetic=False,
            detail={"model": self._model, "rotate": self.cfg.camera.rotate},
        )


class UsbCamera(CameraSource):
    """USB camera via OpenCV, matching Figure 3.3's "Web Camera" block.

    Also the fallback when Picamera2 will not import, which on a Pi is usually
    a numpy ABI mismatch introduced by pip-installing numpy into a
    ``--system-site-packages`` venv.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._capture: cv2.VideoCapture | None = None
        self._seq = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._capture is not None:
                return
            capture = cv2.VideoCapture(self.cfg.camera.device_index)
            if not capture.isOpened():
                raise CameraError(f"could not open USB camera {self.cfg.camera.device_index}")

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera.preview_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera.preview_height)
            capture.set(cv2.CAP_PROP_FPS, self.cfg.camera.preview_fps)
            # A driver-side buffer would hand us stale frames; the worker wants
            # the newest one and drops the rest itself.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # MJPG lets the camera do the compression, which matters on a CPU
            # already saturated by inference.
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

            self._capture = capture
            log.info("USB camera %d opened", self.cfg.camera.device_index)

    def read(self) -> Frame | None:
        if self._capture is None:
            raise CameraError("read() before start()")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None
        self._seq += 1
        return Frame(seq=self._seq, ts=time.time(), bgr=_rotate(frame, self.cfg.camera.rotate))

    def capture_still(self) -> np.ndarray:
        frame = self.read()
        if frame is None:
            raise CameraError("no frame available for a still")
        return frame.bgr

    def stop(self) -> None:
        with self._lock:
            if self._capture is not None:
                self._capture.release()
                self._capture = None

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(
            kind="usb",
            description=f"USB camera (OpenCV device {self.cfg.camera.device_index})",
            width=self.cfg.camera.preview_width,
            height=self.cfg.camera.preview_height,
            fps=float(self.cfg.camera.preview_fps),
            synthetic=False,
            detail={"device_index": self.cfg.camera.device_index},
        )


def build_pi_camera(cfg: Config) -> CameraSource:
    """Open the configured camera, falling back to USB if CSI will not start."""
    if cfg.camera.kind == "usb":
        return UsbCamera(cfg)

    camera = CsiCamera(cfg)
    try:
        camera.start()
        return camera
    except CameraError as exc:
        log.error("CSI camera unavailable (%s) — falling back to USB", exc)
        return UsbCamera(cfg)


def _to_bgr(array: np.ndarray, rotate: int) -> np.ndarray:
    """Picamera2 hands back RGB; everything downstream expects BGR."""
    if array.ndim == 3 and array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_RGBA2BGR)
    elif array.ndim == 3 and array.shape[2] == 3:
        array = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    return _rotate(array, rotate)


def _rotate(image: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate for a camera mounted sideways in the enclosure."""
    if degrees == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


# -- telemetry -------------------------------------------------------------


class PiTelemetry(TelemetrySource):
    """Real sensor readings from the Pi, plus an INA219 if one is fitted."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ina219 = None
        self._ina219_tried = False
        self._vcgencmd_available = True

    def sample(self) -> Telemetry:
        cpu_temp = self._cpu_temp()
        throttled, detail = self._throttle_state()
        memory = psutil.virtual_memory()

        try:
            disk_free_mb = psutil.disk_usage("/").free / 1e6
        except OSError:
            disk_free_mb = None

        voltage_v = current_ma = power_w = None
        sensor = self._get_ina219()
        if sensor is not None:
            try:
                voltage_v = round(float(sensor.bus_voltage + sensor.shunt_voltage), 3)
                current_ma = round(float(sensor.current), 1)
                power_w = round(float(sensor.power), 3)
            except Exception as exc:  # noqa: BLE001 — an I2C glitch is not fatal
                log.debug("INA219 read failed: %s", exc)

        return Telemetry(
            ts=time.time(),
            synthetic=False,
            cpu_temp_c=cpu_temp,
            cpu_percent=psutil.cpu_percent(interval=None),
            mem_used_mb=round((memory.total - memory.available) / 1e6, 1),
            mem_total_mb=round(memory.total / 1e6, 1),
            disk_free_mb=round(disk_free_mb, 1) if disk_free_mb is not None else None,
            voltage_v=voltage_v,
            current_ma=current_ma,
            power_w=power_w,
            # Deliberately None rather than a guess: without a battery monitor
            # there is no honest state of charge to report.
            battery_pct=None,
            throttled=throttled,
            throttle_detail=detail,
        )

    def _cpu_temp(self) -> float | None:
        try:
            return round(int(THERMAL_ZONE.read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            return None

    def _throttle_state(self) -> tuple[bool, str]:
        """Decode ``vcgencmd get_throttled``.

        The latched "has occurred" bits are the valuable ones: they survive the
        event, so an 8-hour soak can be reviewed afterwards for under-voltage
        or thermal capping that would otherwise have gone unnoticed.
        """
        if not self._vcgencmd_available:
            return False, ""
        try:
            result = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
            )
        except (OSError, subprocess.SubprocessError):
            self._vcgencmd_available = False
            return False, ""

        match = re.search(r"0x([0-9a-fA-F]+)", result.stdout)
        if not match:
            return False, ""

        flags = int(match.group(1), 16)
        if flags == 0:
            return False, ""

        reasons = [label for bit, label in THROTTLE_BITS.items() if flags & (1 << bit)]
        active = any(flags & (1 << bit) for bit in (0, 1, 2, 3))
        return active, "; ".join(reasons)

    def _get_ina219(self):
        """Lazily open the INA219, once. Absence is normal, not an error."""
        if self._ina219_tried:
            return self._ina219
        self._ina219_tried = True

        try:
            import board  # type: ignore[import-not-found]
            from adafruit_ina219 import INA219  # type: ignore[import-not-found]

            self._ina219 = INA219(board.I2C())
            log.info("INA219 power monitor detected")
        except Exception as exc:  # noqa: BLE001
            log.info(
                "no INA219 power monitor (%s) — voltage, current and power will "
                "be reported as unavailable rather than estimated",
                type(exc).__name__,
            )
            self._ina219 = None
        return self._ina219
