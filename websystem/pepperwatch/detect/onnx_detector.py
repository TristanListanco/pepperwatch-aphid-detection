"""ONNX Runtime detector — the real inference path.

Deliberately bypasses the Ultralytics Python wrapper and drives ONNX Runtime
directly. The wrapper carries PyTorch and a large import graph that buys nothing
at inference time, and on a four-core Pi the memory and startup cost are both
worth avoiding. Manuscript §3.2 names ONNX Runtime as the inference engine; this
is that, literally.

Thread count is the setting that matters most on a Pi 5. Giving ONNX all four
cores starves Flask and the capture thread, so the dashboard stutters exactly
when inference is busiest. Three is the default, leaving one core for everything
else — see the ``CPUAffinity`` note in the deploy units.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from ..models import Detection
from .base import Detector, DetectorInfo, StageTimings
from .postprocess import decode_yolov8, letterbox, to_tensor

log = logging.getLogger(__name__)


class OnnxDetector(Detector):
    """Runs a YOLOv8 ONNX export over BGR frames."""

    def __init__(
        self,
        model_path: Path,
        class_names: list[str],
        *,
        input_size: int = 640,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        max_detections: int = 100,
        intra_op_threads: int = 3,
    ) -> None:
        import onnxruntime as ort  # imported here so the stub path needs no ORT

        self._model_path = Path(model_path)
        if not self._model_path.exists():
            raise FileNotFoundError(f"no model at {self._model_path}")

        self._class_names = list(class_names)
        self._input_size = int(input_size)
        self._conf_threshold = float(conf_threshold)
        self._iou_threshold = float(iou_threshold)
        self._max_detections = int(max_detections)
        self._threads = int(intra_op_threads)

        options = ort.SessionOptions()
        options.intra_op_num_threads = int(intra_op_threads)
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.enable_cpu_mem_arena = True
        # Keep the log quiet; ORT is chatty about unsupported ops otherwise.
        options.log_severity_level = 3

        self._session = ort.InferenceSession(
            str(self._model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name

        # Honour the model's own input size when it is static, rather than the
        # config's guess. A mismatch here would silently letterbox to the wrong
        # square and misplace every box.
        declared = self._session.get_inputs()[0].shape
        if len(declared) == 4 and isinstance(declared[2], int) and declared[2] > 0:
            if int(declared[2]) != self._input_size:
                log.warning(
                    "model expects %dx%d but config says %d — using the model's size",
                    declared[2], declared[3], self._input_size,
                )
                self._input_size = int(declared[2])

        self._providers = self._session.get_providers()
        log.info(
            "loaded %s (%s, %dx%d, %d classes, %d threads)",
            self._model_path.name, ",".join(self._providers),
            self._input_size, self._input_size, len(self._class_names), intra_op_threads,
        )

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        began = time.perf_counter()
        canvas, transform = letterbox(bgr, self._input_size)
        tensor = to_tensor(canvas)
        prepared = time.perf_counter()
        outputs = self._session.run([self._output_name], {self._input_name: tensor})
        inferred = time.perf_counter()
        detections = decode_yolov8(
            outputs[0],
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
        """Adjust thresholds at runtime, for the Settings page and sweeps."""
        if conf is not None:
            self._conf_threshold = float(conf)
        if iou is not None:
            self._iou_threshold = float(iou)

    @property
    def info(self) -> DetectorInfo:
        return DetectorInfo(
            kind="onnx",
            model_version=self._model_path.stem,
            description=f"ONNX Runtime on the CPU ({self._threads} threads)",
            input_size=self._input_size,
            class_names=list(self._class_names),
            synthetic=False,
            detail={
                "model_path": str(self._model_path),
                "size_mb": round(self._model_path.stat().st_size / 1e6, 2),
                "conf_threshold": self._conf_threshold,
                "iou_threshold": self._iou_threshold,
                "providers": self._providers,
                "intra_op_threads": self._threads,
            },
        )

    def close(self) -> None:
        self._session = None  # type: ignore[assignment]
