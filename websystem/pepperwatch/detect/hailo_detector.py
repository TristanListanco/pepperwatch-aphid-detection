"""Inference on a Raspberry Pi AI HAT+ (Hailo-8L, Hailo-8 or Hailo-10H).

Uses HailoRT's InferModel API (``VDevice.create_infer_model``), which both
HailoRT 4.x (Hailo-8/8L, ``hailo-all``) and 5.x (Hailo-10H, ``hailo-h10-all``)
provide. The device is opened and the network configured once, when the
session starts, and kept that way: configuring a HEF takes a noticeable
fraction of a second and must not happen per frame.

What moves off the CPU, and what does not:

* The network itself, including normalisation (folded into the HEF at compile
  time), runs on the HAT. The input is the letterboxed frame as plain uint8
  RGB: no float conversion on the CPU.
* Non-maximum suppression runs on the chip too, for a HEF compiled with NMS
  (the Hailo model zoo default for YOLOv8). Only thresholding and mapping boxes
  back through the letterbox happen in Python.
* Letterboxing and the BGR->RGB swap stay on the CPU. At 640x480 in they cost
  roughly a millisecond, which is why the per-stage timings in the session CSV
  matter: with inference on the HAT, these become a visible share of the frame.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from ..models import Detection
from .base import Detector, DetectorInfo, StageTimings
from .postprocess import decode_hailo_nms, decode_yolov8, letterbox

log = logging.getLogger(__name__)

# Floor for the on-chip score threshold. Boxes between this and the
# configured threshold are filtered in Python, so lowering the confidence
# threshold from the Settings page still works without reconfiguring the HAT.
_ON_CHIP_SCORE_FLOOR = 0.05


class HailoDetector(Detector):
    """Runs a compiled YOLOv8 HEF on the AI HAT+."""

    def __init__(
        self,
        hef_path: Path,
        class_names: list[str],
        *,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        max_detections: int = 100,
        timeout_ms: int = 1000,
        accelerator: str = "Hailo",
        runtime_version: str | None = None,
    ) -> None:
        from hailo_platform import HEF, FormatType, VDevice  # type: ignore[import-not-found]

        self._hef_path = Path(hef_path)
        self._class_names = list(class_names)
        self._conf_threshold = float(conf_threshold)
        self._iou_threshold = float(iou_threshold)
        self._max_detections = int(max_detections)
        self._timeout_ms = int(timeout_ms)
        self._accelerator = accelerator
        self._runtime_version = runtime_version
        self._lock = threading.Lock()
        self._temperature_ok = True

        outputs = HEF(str(self._hef_path)).get_output_vstream_infos()
        if len(outputs) != 1:
            raise RuntimeError(
                f"{self._hef_path.name} has {len(outputs)} outputs; expected one. "
                "Compile the HEF with on-chip NMS (the Hailo model zoo default for YOLOv8)."
            )
        self._nms = "NMS" in str(outputs[0].format.order).upper()

        params = VDevice.create_params()
        try:
            from hailo_platform import HailoSchedulingAlgorithm  # type: ignore[import-not-found]

            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        except (ImportError, AttributeError):
            pass
        self._vdevice = VDevice(params)

        try:
            model = self._vdevice.create_infer_model(str(self._hef_path))
            model.set_batch_size(1)
            model.input().set_format_type(FormatType.UINT8)
            model.output().set_format_type(FormatType.FLOAT32)
            if self._nms:
                self._tune_on_chip_nms(model.output())

            shape = tuple(int(x) for x in model.input().shape)
            if len(shape) != 3 or shape[0] != shape[1]:
                raise RuntimeError(f"expected a square HxWx3 input, the HEF declares {shape}")
            self._input_size = shape[0]
            self._output_name = model.output().name
            self._output_shape = tuple(model.output().shape)

            self._configured_context = model.configure()
            self._configured = self._configured_context.__enter__()
            self._model = model
        except Exception:
            self._release_device()
            raise

        log.info(
            "loaded %s on the %s (%dx%d input, NMS %s, HailoRT %s)",
            self._hef_path.name, accelerator, self._input_size, self._input_size,
            "on chip" if self._nms else "in Python", runtime_version or "?",
        )

    def _tune_on_chip_nms(self, output) -> None:
        """Set the chip's NMS thresholds where this HailoRT version allows it."""
        for setter, value in (
            ("set_nms_score_threshold", min(self._conf_threshold, _ON_CHIP_SCORE_FLOOR)),
            ("set_nms_iou_threshold", self._iou_threshold),
            ("set_nms_max_proposals_per_class", self._max_detections),
        ):
            try:
                getattr(output, setter)(value)
            except Exception as exc:  # noqa: BLE001 — older runtimes lack some setters
                log.debug("%s unavailable: %s", setter, exc)

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        began = time.perf_counter()
        canvas, transform = letterbox(bgr, self._input_size)
        rgb = np.ascontiguousarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        prepared = time.perf_counter()

        with self._lock:
            bindings = self._configured.create_bindings(
                output_buffers={self._output_name: np.empty(self._output_shape, dtype=np.float32)}
            )
            bindings.input().set_buffer(rgb)
            self._configured.run([bindings], self._timeout_ms)
            raw = bindings.output().get_buffer()
        inferred = time.perf_counter()

        if self._nms:
            detections = decode_hailo_nms(
                raw,
                transform,
                self._class_names,
                conf_threshold=self._conf_threshold,
                max_detections=self._max_detections,
            )
        else:
            detections = decode_yolov8(
                np.asarray(raw),
                transform,
                self._class_names,
                conf_threshold=self._conf_threshold,
                iou_threshold=self._iou_threshold,
                max_detections=self._max_detections,
            )

        self.timings = StageTimings(
            preprocess_ms=(prepared - began) * 1000.0,
            infer_ms=(inferred - prepared) * 1000.0,
            postprocess_ms=(time.perf_counter() - inferred) * 1000.0,
        )
        return detections

    def set_thresholds(self, *, conf: float | None = None, iou: float | None = None) -> None:
        """Adjust thresholds at runtime.

        Confidence applies immediately. The IoU threshold is baked into the
        configured network when NMS runs on the chip, so a change takes effect
        from the next session.
        """
        if conf is not None:
            self._conf_threshold = float(conf)
        if iou is not None:
            self._iou_threshold = float(iou)
            if self._nms:
                log.info("IoU threshold %.2f applies from the next session on the AI HAT+", iou)

    def chip_temperature(self) -> float | None:
        """The Hailo die temperature, or ``None`` where the runtime cannot read it."""
        if not self._temperature_ok:
            return None
        try:
            with self._lock:
                device = self._vdevice.get_physical_devices()[0]
                reading = device.control.get_chip_temperature()
            return round(float(reading.ts0_temperature), 1)
        except Exception as exc:  # noqa: BLE001
            log.info("Hailo temperature unavailable (%s); not asking again", exc)
            self._temperature_ok = False
            return None

    @property
    def info(self) -> DetectorInfo:
        return DetectorInfo(
            kind="hailo",
            model_version=self._hef_path.stem,
            description=f"{self._accelerator} AI HAT+ via HailoRT {self._runtime_version or ''}".strip(),
            input_size=self._input_size,
            class_names=list(self._class_names),
            synthetic=False,
            detail={
                "hef_path": str(self._hef_path),
                "size_mb": round(self._hef_path.stat().st_size / 1e6, 2),
                "conf_threshold": self._conf_threshold,
                "iou_threshold": self._iou_threshold,
                "nms_on_chip": self._nms,
                "accelerator": self._accelerator,
                "runtime_version": self._runtime_version,
            },
        )

    def close(self) -> None:
        context = getattr(self, "_configured_context", None)
        if context is not None:
            try:
                context.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._configured_context = None
        self._release_device()

    def _release_device(self) -> None:
        device = getattr(self, "_vdevice", None)
        if device is not None:
            try:
                device.release()
            except Exception:  # noqa: BLE001
                pass
            self._vdevice = None
