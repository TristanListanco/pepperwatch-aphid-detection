"""Detector factory.

Picks the ONNX detector when a model file exists and falls back to the stub
otherwise, so a fresh checkout runs end to end before any model is trained.
The fallback is loud: it logs a warning and marks every event ``synthetic``.
"""

from __future__ import annotations

import logging

from ..config import Config
from .base import Detector, DetectorInfo
from .postprocess import (
    Letterbox,
    decode_yolov8,
    derive_severity,
    letterbox,
    non_max_suppression,
    severity_from_config,
    to_tensor,
)

__all__ = [
    "Detector",
    "DetectorInfo",
    "Letterbox",
    "decode_yolov8",
    "derive_severity",
    "get_detector",
    "letterbox",
    "non_max_suppression",
    "severity_from_config",
    "to_tensor",
]

log = logging.getLogger(__name__)


def get_detector(cfg: Config) -> Detector:
    """Build the detector for the current configuration."""
    class_names = list(cfg.inference.class_names)
    model_path = cfg.model_path

    if model_path.exists():
        from .onnx_detector import OnnxDetector

        return OnnxDetector(
            model_path,
            class_names,
            input_size=cfg.inference.input_size,
            conf_threshold=cfg.inference.conf_threshold,
            iou_threshold=cfg.inference.iou_threshold,
            max_detections=cfg.inference.max_detections,
            intra_op_threads=cfg.inference.intra_op_threads,
        )

    from .stub_detector import StubDetector

    log.warning(
        "no model at %s — falling back to the heuristic stub. "
        "Results are NOT model output and every event will be flagged synthetic.",
        model_path,
    )
    return StubDetector(
        class_names,
        input_size=cfg.inference.input_size,
        conf_threshold=cfg.inference.conf_threshold,
        max_detections=cfg.inference.max_detections,
    )
