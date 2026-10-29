"""Detector factory.

The session's accelerator decision (see ``pepperwatch.accel``) picks the
detector: the AI HAT+ when one is usable, ONNX Runtime on the CPU otherwise,
and a heuristic stub when there is no model at all, so the system still runs
end to end before training finishes. The stub is loud about it: it logs a
warning and marks every event ``synthetic``.
"""

from __future__ import annotations

import dataclasses
import logging

from ..accel import AcceleratorChoice
from ..config import Config
from .base import Detector, DetectorInfo, StageTimings
from .postprocess import (
    Letterbox,
    decode_hailo_nms,
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
    "StageTimings",
    "decode_hailo_nms",
    "decode_yolov8",
    "derive_severity",
    "get_detector",
    "letterbox",
    "non_max_suppression",
    "severity_from_config",
    "to_tensor",
]

log = logging.getLogger(__name__)


def get_detector(cfg: Config, choice: AcceleratorChoice) -> tuple[Detector, AcceleratorChoice]:
    """Build this session's detector.

    Returns the detector and the accelerator decision as it finally stood: if
    the AI HAT+ was chosen but would not initialise (a HEF compiled for the
    wrong chip, say), the session falls back to the CPU and the returned choice
    says why.
    """
    class_names = list(cfg.inference.class_names)

    if choice.kind == "hailo" and choice.hef_path is not None:
        try:
            from .hailo_detector import HailoDetector

            probe = choice.probe
            detector = HailoDetector(
                choice.hef_path,
                class_names,
                conf_threshold=cfg.inference.conf_threshold,
                iou_threshold=cfg.inference.iou_threshold,
                max_detections=cfg.inference.max_detections,
                timeout_ms=cfg.accelerator.timeout_ms,
                accelerator=choice.name,
                runtime_version=probe.runtime_version if probe else None,
            )
            return detector, choice
        except Exception as exc:  # noqa: BLE001 — any HailoRT failure falls back to the CPU
            log.error("AI HAT+ would not initialise (%s); falling back to the CPU", exc)
            choice = dataclasses.replace(
                choice,
                kind="cpu",
                name="CPU",
                profile="cpu",
                reason=f"{choice.name} failed to start {choice.hef_path.name}: {exc}",
            )

    model_path = cfg.model_path
    if model_path.exists():
        from .onnx_detector import OnnxDetector

        detector = OnnxDetector(
            model_path,
            class_names,
            input_size=cfg.inference.input_size,
            conf_threshold=cfg.inference.conf_threshold,
            iou_threshold=cfg.inference.iou_threshold,
            max_detections=cfg.inference.max_detections,
            intra_op_threads=cfg.inference.intra_op_threads,
        )
        return detector, choice

    from .stub_detector import StubDetector

    log.warning(
        "no model at %s — falling back to the heuristic stub. "
        "Results are NOT model output and every event will be flagged synthetic.",
        model_path,
    )
    detector = StubDetector(
        class_names,
        input_size=cfg.inference.input_size,
        conf_threshold=cfg.inference.conf_threshold,
        max_detections=cfg.inference.max_detections,
    )
    return detector, choice
