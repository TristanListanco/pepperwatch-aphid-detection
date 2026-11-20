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

import ast
import logging
import math
import mmap
import re
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..models import Detection
from .base import Detector, DetectorInfo, StageTimings
from .postprocess import decode_yolov8, letterbox, to_tensor

log = logging.getLogger(__name__)

# Millions of weights in each size, trained on two classes: names an export's size.
YOLO_SIZES = {
    "YOLOv8": {"n": 3.0, "s": 11.1, "m": 25.9, "l": 43.6, "x": 68.2},
    "YOLO11": {"n": 2.6, "s": 9.4, "m": 20.1, "l": 25.3, "x": 56.9},
}


def _literal(metadata: dict[str, str], key: str) -> Any:
    try:
        return ast.literal_eval(metadata.get(key, ""))
    except (ValueError, SyntaxError):
        return None


def trained_names(metadata: dict[str, str]) -> list[str] | None:
    """The class names exactly as trained, in class-index order.

    The metadata holds ``names`` as ``"{0: 'aphid_infested', 1: 'healthy'}"``.
    """
    names = _literal(metadata, "names")
    if not isinstance(names, dict) or not names:
        return None
    return [str(names[index]) for index in sorted(names)]


def model_class_names(metadata: dict[str, str]) -> list[str] | None:
    """Class names from an Ultralytics export, mapped to this system's names.

    Anything healthy becomes ``healthy`` and anything aphid- or damage-related
    ``aphid_damage``, the names severity, the advice and the charts key on.
    """
    ordered = trained_names(metadata)
    return [_canonical(name) for name in ordered] if ordered else None


def yolo_architecture(path: Path, bytes_per_weight: int) -> tuple[str | None, float]:
    """Which YOLO an Ultralytics ONNX export is, and its millions of weights.

    The export does not say: a fine-tuned ``best.pt`` exports as "Ultralytics
    best model". But every node keeps its Ultralytics layer path
    (``/model.22/dfl/...``): the detection head is layer 22 in YOLOv8 and 23 in
    YOLO11, and only YOLO11 has attention. The size letter is the nearest by
    weight count, which the file size gives at a known precision.
    shortcut: recognises only YOLOv8 and YOLO11, the families this thesis trained;
    add a family's layer signature here before deploying another.
    """
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        found = re.findall(rb"/model\.(\d+)(?=/)|/(attn|dfl|cv4|one2one)", data)
    head = max((int(layer) for layer, _ in found if layer), default=0)
    marks = {mark for _, mark in found if mark}
    weights_m = path.stat().st_size / bytes_per_weight / 1e6
    family = None
    # One-to-one heads are YOLOv10 or YOLO26, no DFL is YOLO26, cv4 is YOLOv9.
    if b"dfl" in marks and not marks & {b"one2one", b"cv4"}:
        if head == 22 and b"attn" not in marks:
            family = "YOLOv8"
        elif head == 23 and b"attn" in marks:
            family = "YOLO11"
    if family is None:
        return None, weights_m
    size, typical = min(YOLO_SIZES[family].items(), key=lambda item: abs(math.log(weights_m / item[1])))
    return (family + size if abs(weights_m / typical - 1) < 0.2 else family), weights_m


def describe_export(path: Path, metadata: dict[str, str], producer: str) -> dict[str, Any]:
    """What an Ultralytics export records about itself, for Settings and the session record."""
    args = _literal(metadata, "args")
    args = args if isinstance(args, dict) else {}
    if producer == "onnx.quantize":  # ONNX Runtime's quantizer
        precision, width = "INT8", 1
    elif args.get("half") or args.get("quantize") in ("fp16", "half"):
        precision, width = "FP16", 2
    else:
        precision, width = "FP32", 4
    architecture, weights_m = yolo_architecture(path, width)
    return {
        "architecture": architecture,
        "weights_m": round(weights_m, 1),
        "precision": precision,
        "exported": metadata.get("date"),
        "ultralytics_version": metadata.get("version"),
        "trained_classes": trained_names(metadata),
    }


def _canonical(name: str) -> str:
    lowered = name.lower()
    if "healthy" in lowered:
        return "healthy"
    if any(word in lowered for word in ("aphid", "damage", "infest")):
        return "aphid_damage"
    return name


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

        # Ultralytics stores the class order it was trained with. Trust that
        # over the config: a model trained as {0: aphid_infested, 1: healthy}
        # would otherwise have every label swapped, and severity inverted.
        meta = self._session.get_modelmeta()
        self._export = describe_export(self._model_path, meta.custom_metadata_map, meta.producer_name)
        self._runtime_version = ort.__version__
        trained = model_class_names(meta.custom_metadata_map)
        if trained and trained != self._class_names:
            log.info("using the model's class order %s (config says %s)", trained, self._class_names)
            self._class_names = trained

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
            description=f"ONNX Runtime {self._runtime_version} on the CPU ({self._threads} threads)",
            input_size=self._input_size,
            class_names=list(self._class_names),
            synthetic=False,
            detail={
                **self._export,
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
