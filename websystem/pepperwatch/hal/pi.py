"""Raspberry Pi 5 camera and sensor drivers.

Two camera backends, because the manuscript specifies two different things:
Table 3.2 lists a MIPI CSI Camera Module, while Figure 3.3's hardware block
says "Web Camera". Both are supported, and :func:`open_camera` picks whichever
is attached (see ``hal.discovery``) unless ``[camera].kind`` forces one.

Picamera2 is **apt-only**. ``sudo apt install python3-picamera2`` provides it
along with the libcamera bindings it needs; pip cannot build working ones. That
is why the venv must be created with ``--system-site-packages``, and why
``python -m pepperwatch.doctor`` probes for the numpy ABI mismatch that flag
can introduce.

Telemetry reads real sensors and degrades to ``None`` for anything absent,
never to a plausible-looking zero.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import psutil

from ..config import Config
from .base import CameraError, CameraInfo, CameraSource, Frame, Telemetry, TelemetrySource
from .discovery import CameraCandidate, choose_camera, discover_cameras
from .power import Vcgencmd, parse_pmic_adc, parse_throttled, read_cpu_freq_mhz, read_fan_rpm

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

    The Pi 5's ISP can deliver ``lores`` as RGB. The VideoCore ISP on a Pi 4,
    Pi 3 or Zero 2 W only produces it as YUV420, so if RGB is refused the
    stream is configured as YUV420 and converted on read, which costs about a
    millisecond at 640x480.
    """

    def __init__(self, cfg: Config, candidate: CameraCandidate) -> None:
        self.cfg = cfg
        self.candidate = candidate
        self._camera = None
        self._seq = 0
        self._started = False
        self._lock = threading.Lock()
        self._lores_yuv = False

    def _configuration(self, camera, lores_format: str):
        return camera.create_video_configuration(
            main={
                "size": (self.cfg.camera.still_width, self.cfg.camera.still_height),
                "format": "RGB888",
            },
            lores={
                "size": (self.cfg.camera.preview_width, self.cfg.camera.preview_height),
                "format": lores_format,
            },
            # Only one buffered frame: the worker always wants the newest, and
            # a deeper queue would just add latency between what the camera
            # sees and what the boxes describe.
            buffer_count=2,
            # libcamera clamps this to what the sensor mode allows.
            controls={"FrameRate": float(self.cfg.camera.preview_fps)},
        )

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

            try:
                camera = Picamera2(camera_num=int(self.candidate.device))
                try:
                    camera.configure(self._configuration(camera, "RGB888"))
                except Exception as exc:  # noqa: BLE001 — VideoCore ISP: lores must be YUV
                    log.info("RGB lores refused (%s); using YUV420, as on a Pi 4 or earlier", exc)
                    camera.configure(self._configuration(camera, "YUV420"))
                    self._lores_yuv = True
                camera.start()
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"CSI camera {self.candidate.device} would not start: {exc}") from exc

            # Auto exposure and white balance need a moment to settle, or the
            # first several frames come out badly exposed.
            time.sleep(1.5)

            self._camera = camera
            self._started = True
            log.info(
                "CSI camera started: %s, preview %dx%d @ %s fps, still %dx%d",
                self.candidate.model,
                self.cfg.camera.preview_width, self.cfg.camera.preview_height,
                self.cfg.camera.preview_fps,
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
        if self._lores_yuv:
            # I420 arrives as one (height * 3/2, width) plane stack.
            array = cv2.cvtColor(array, cv2.COLOR_YUV2BGR_I420)
            return Frame(seq=self._seq, ts=time.time(), bgr=_rotate(array, self.cfg.camera.rotate))
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
            description=f"MIPI CSI camera {self.candidate.device} ({self.candidate.model})",
            width=self.cfg.camera.preview_width,
            height=self.cfg.camera.preview_height,
            fps=float(self.cfg.camera.preview_fps),
            detail={
                **self.candidate.as_dict(),
                "rotate": self.cfg.camera.rotate,
                "lores_format": "YUV420" if self._lores_yuv else "RGB888",
            },
        )


class UsbCamera(CameraSource):
    """USB (UVC) webcam via OpenCV's V4L2 backend, Figure 3.3's "Web Camera"."""

    def __init__(self, cfg: Config, candidate: CameraCandidate) -> None:
        self.cfg = cfg
        self.candidate = candidate
        self._capture: cv2.VideoCapture | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._actual: tuple[int, int, float] | None = None

    def start(self) -> None:
        with self._lock:
            if self._capture is not None:
                return
            capture = cv2.VideoCapture(self.candidate.device, cv2.CAP_V4L2)
            if not capture.isOpened():
                raise CameraError(f"could not open USB camera {self.candidate.device}")

            # FOURCC first: V4L2 picks the frame sizes and rates on offer per
            # pixel format, and most webcams reach 30 fps only in MJPG.
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera.preview_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera.preview_height)
            capture.set(cv2.CAP_PROP_FPS, self.cfg.camera.preview_fps)
            # A driver-side buffer would hand us stale frames; the worker wants
            # the newest one and drops the rest itself.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            self._actual = (
                int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                float(capture.get(cv2.CAP_PROP_FPS)),
            )
            self._capture = capture
            log.info(
                "USB camera %s (%s) opened at %dx%d @ %.0f fps",
                self.candidate.device, self.candidate.model, *self._actual,
            )

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
        width, height, fps = self._actual or (
            self.cfg.camera.preview_width,
            self.cfg.camera.preview_height,
            float(self.cfg.camera.preview_fps),
        )
        return CameraInfo(
            kind="usb",
            description=f"USB camera {self.candidate.device} ({self.candidate.model})",
            width=width,
            height=height,
            fps=fps,
            detail={**self.candidate.as_dict(), "rotate": self.cfg.camera.rotate},
        )


def open_camera(cfg: Config) -> tuple[CameraSource, list[CameraCandidate]]:
    """Find the attached cameras and start the preferred one.

    Returns the started camera and everything that was found, for the session
    record. If the preferred camera will not start, the next one is tried, so a
    CSI module with a loose ribbon does not stop a session that has a webcam.
    """
    candidates = discover_cameras()
    for found in candidates:
        log.info("found camera: %s", found.describe())

    kind = str(cfg.camera.kind)
    device = str(cfg.camera.device or "")
    preferred = choose_camera(candidates, kind=kind, device=device)
    if preferred is None:
        wanted = kind if not device else f"{kind} {device}"
        seen = ", ".join(c.describe() for c in candidates) or "none"
        raise CameraError(
            f"no camera matching [camera] kind={wanted!r}; found: {seen}. "
            "Check the ribbon cable or USB connection, and `rpicam-hello --list-cameras`."
        )

    ordered = [preferred] + [
        c for c in candidates
        if c is not preferred and (kind == "auto" or c.kind == kind) and not device
    ]
    errors = []
    for candidate in ordered:
        camera: CameraSource = CsiCamera(cfg, candidate) if candidate.kind == "csi" else UsbCamera(cfg, candidate)
        try:
            camera.start()
            return camera, candidates
        except CameraError as exc:
            log.error("%s would not start: %s", candidate.describe(), exc)
            errors.append(f"{candidate.describe()}: {exc}")
            camera.stop()
    raise CameraError("no camera would start — " + "; ".join(errors))


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
    """Readings from the Pi's own sensors, plus an INA219 if one is fitted."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ina219 = None
        self._ina219_tried = False
        self._vcgencmd = Vcgencmd()

    def sample(self) -> Telemetry:
        memory = psutil.virtual_memory()
        try:
            disk_free_mb = round(psutil.disk_usage("/").free / 1e6, 1)
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

        pmic_text = self._vcgencmd.run("pmic_read_adc")
        pmic = parse_pmic_adc(pmic_text) if pmic_text else None

        flags = parse_throttled(self._vcgencmd.run("get_throttled") or "")
        throttled, detail = _decode_throttle(flags)

        return Telemetry(
            ts=time.time(),
            cpu_temp_c=self._cpu_temp(),
            cpu_percent=psutil.cpu_percent(interval=None),
            cpu_freq_mhz=read_cpu_freq_mhz(),
            mem_used_mb=round((memory.total - memory.available) / 1e6, 1),
            mem_total_mb=round(memory.total / 1e6, 1),
            disk_free_mb=disk_free_mb,
            voltage_v=voltage_v,
            current_ma=current_ma,
            power_w=power_w,
            pmic_power_w=pmic.rails_w if pmic else None,
            input_voltage_v=pmic.input_voltage_v if pmic else None,
            fan_rpm=read_fan_rpm(),
            throttled=throttled,
            throttled_flags=flags,
            throttle_detail=detail,
        )

    def _cpu_temp(self) -> float | None:
        try:
            return round(int(THERMAL_ZONE.read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            return None

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
                "no INA219 power monitor (%s); power comes from the Pi 5 PMIC "
                "rail readings only",
                type(exc).__name__,
            )
            self._ina219 = None
        return self._ina219


def _decode_throttle(flags: int | None) -> tuple[bool, str]:
    """Whether the SoC is throttled now, and every reason set since boot.

    The latched "has occurred" bits are the valuable ones: they survive the
    event, so an 8-hour soak can be reviewed afterwards for under-voltage or
    thermal capping that would otherwise have gone unnoticed.
    """
    if not flags:
        return False, ""
    reasons = [label for bit, label in THROTTLE_BITS.items() if flags & (1 << bit)]
    active = any(flags & (1 << bit) for bit in (0, 1, 2, 3))
    return active, "; ".join(reasons)
