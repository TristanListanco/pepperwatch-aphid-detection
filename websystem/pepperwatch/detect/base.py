"""The detector contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..models import Detection


@dataclass(frozen=True, slots=True)
class StageTimings:
    """Milliseconds spent in each stage of the last :meth:`Detector.detect`.

    Split three ways so a session's CSV shows where the time goes: on the CPU
    path ``infer_ms`` dominates, while on the AI HAT+ it shrinks to a few
    milliseconds and pre/post-processing on the CPU becomes the larger share.
    """

    preprocess_ms: float = 0.0
    infer_ms: float = 0.0
    postprocess_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.preprocess_ms + self.infer_ms + self.postprocess_ms


@dataclass(frozen=True, slots=True)
class DetectorInfo:
    """What a detector reports about itself.

    ``synthetic`` is the important one: it marks output that did not come from a
    trained model, and it propagates into ``detection_event.synthetic`` so
    development data can never be mistaken for a field measurement.
    """

    kind: str
    model_version: str
    description: str
    input_size: int
    class_names: list[str]
    synthetic: bool
    detail: dict[str, Any] = field(default_factory=dict)


class Detector(ABC):
    """Turns a BGR frame into normalized detections."""

    #: Set by every :meth:`detect` call.
    timings: StageTimings = StageTimings()

    @abstractmethod
    def detect(self, bgr: np.ndarray) -> list[Detection]:
        """Run one inference. Coordinates are normalized to the input frame."""

    @property
    @abstractmethod
    def info(self) -> DetectorInfo:
        ...

    def warmup(self, bgr: np.ndarray, *, rounds: int = 2) -> None:
        """Run a few throwaway inferences.

        The first ONNX Runtime call allocates arenas and picks kernels, so it
        can take several times longer than steady state. Without a warmup that
        cost lands in the first measured frame and skews every benchmark.
        """
        for _ in range(rounds):
            self.detect(bgr)

    def close(self) -> None:
        """Release the session. Default is a no-op."""
