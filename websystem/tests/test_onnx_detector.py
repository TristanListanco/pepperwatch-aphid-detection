"""The real ONNX inference path, driven by a shape-accurate fixture model.

These tests prove the plumbing that the trained model will later flow through:
session construction, tensor layout, the head decode, the letterbox inverse,
and threshold handling. Swapping the fixture for ``pepperwatch.onnx`` changes
nothing in the detector, which is the point of testing it this way now rather
than waiting for training to finish.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pepperwatch.accel import AcceleratorChoice
from pepperwatch.config import load
from pepperwatch.detect import get_detector
from pepperwatch.detect.onnx_detector import OnnxDetector
from tests.make_fixture_model import build

CPU = AcceleratorChoice(kind="cpu", name="CPU", profile="cpu", reason="test")
CLASSES = ["healthy", "aphid_damage"]


@pytest.fixture(scope="module")
def fixture_model(tmp_path_factory) -> Path:
    return build(tmp_path_factory.mktemp("model") / "fixture.onnx")


@pytest.fixture
def detector(fixture_model: Path) -> OnnxDetector:
    det = OnnxDetector(
        fixture_model, CLASSES, input_size=640, conf_threshold=0.25, iou_threshold=0.45
    )
    yield det
    det.close()


@pytest.fixture
def frame() -> np.ndarray:
    """A 640x480 frame, so letterboxing is genuinely exercised."""
    return np.full((480, 640, 3), 90, dtype=np.uint8)


def test_session_reports_itself_as_not_synthetic(detector: OnnxDetector) -> None:
    """The flag that keeps stub output out of the Chapter IV results."""
    info = detector.info
    assert info.kind == "onnx"
    assert info.synthetic is False
    assert info.input_size == 640
    assert info.class_names == CLASSES
    assert "CPUExecutionProvider" in info.detail["providers"]


def test_detects_the_planted_boxes(detector: OnnxDetector, frame: np.ndarray) -> None:
    detections = detector.detect(frame)
    assert len(detections) == 2
    assert {d.class_name for d in detections} == {"aphid_damage"}
    assert [round(d.confidence, 2) for d in detections] == [0.88, 0.51]


def test_planted_centre_box_maps_back_to_the_frame_centre(
    detector: OnnxDetector, frame: np.ndarray
) -> None:
    """End-to-end letterbox inverse: tensor centre must be frame centre."""
    best = detector.detect(frame)[0]
    assert best.x + best.w / 2 == pytest.approx(0.5, abs=0.01)
    assert best.y + best.h / 2 == pytest.approx(0.5, abs=0.01)
    # 64 tensor px at scale 1.0 over a 640x480 frame: 10% wide, 13.3% tall.
    assert best.w == pytest.approx(64 / 640, abs=0.01)
    assert best.h == pytest.approx(64 / 480, abs=0.01)


def test_offset_box_lands_where_planted(detector: OnnxDetector, frame: np.ndarray) -> None:
    """The second planted box sits above centre; pad_y must be undone."""
    second = detector.detect(frame)[1]
    # Tensor (160, 120) with pad_y = 80 -> frame (160, 40) -> normalized (0.25, 0.083).
    assert second.x + second.w / 2 == pytest.approx(0.25, abs=0.01)
    assert second.y + second.h / 2 == pytest.approx(40 / 480, abs=0.01)


def test_threshold_filters_the_weaker_box(fixture_model: Path, frame: np.ndarray) -> None:
    det = OnnxDetector(fixture_model, CLASSES, conf_threshold=0.75, iou_threshold=0.45)
    detections = det.detect(frame)
    det.close()
    assert len(detections) == 1
    assert round(detections[0].confidence, 2) == 0.88


def test_figure_3_3_threshold_of_0_80_would_drop_a_real_detection(
    fixture_model: Path, frame: np.ndarray
) -> None:
    """Documents why the shipped default is 0.25, not Figure 3.3's 0.80.

    The 0.51 box is a legitimate detection that a 0.80 filter discards. At
    real-world confidences on small aphid clusters this is the mechanism that
    would inflate the False Negative count §4 reports as a headline metric.
    """
    lenient = OnnxDetector(fixture_model, CLASSES, conf_threshold=0.25, iou_threshold=0.45)
    strict = OnnxDetector(fixture_model, CLASSES, conf_threshold=0.80, iou_threshold=0.45)
    try:
        assert len(lenient.detect(frame)) == 2
        assert len(strict.detect(frame)) == 1
    finally:
        lenient.close()
        strict.close()


def test_set_thresholds_takes_effect_without_reloading(
    detector: OnnxDetector, frame: np.ndarray
) -> None:
    """The Settings page adjusts thresholds live; reloading the session would stall inference."""
    assert len(detector.detect(frame)) == 2
    detector.set_thresholds(conf=0.80)
    assert len(detector.detect(frame)) == 1
    detector.set_thresholds(conf=0.25)
    assert len(detector.detect(frame)) == 2


@pytest.mark.parametrize("shape", [(480, 640), (640, 480), (1080, 1920), (640, 640)])
def test_accepts_any_frame_shape(detector: OnnxDetector, shape) -> None:
    detections = detector.detect(np.full((*shape, 3), 90, dtype=np.uint8))
    assert len(detections) == 2
    for det in detections:
        assert 0.0 <= det.x <= 1.0 and 0.0 <= det.y <= 1.0
        assert det.x + det.w <= 1.0 + 1e-6
        assert det.y + det.h <= 1.0 + 1e-6


def test_warmup_does_not_change_results(detector: OnnxDetector, frame: np.ndarray) -> None:
    before = detector.detect(frame)
    detector.warmup(frame, rounds=3)
    assert detector.detect(frame) == before


def test_missing_model_raises_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OnnxDetector(tmp_path / "absent.onnx", CLASSES)


def test_factory_prefers_the_model_when_one_exists(fixture_model: Path, tmp_path: Path) -> None:
    """The whole point of the stub fallback: dropping in a model changes nothing else."""
    cfg = load(
        {
            "inference": {"model_path": str(fixture_model)},
            "db": {"path": str(tmp_path / "t.db")},
        },
        use_env=False,
    )
    det, choice = get_detector(cfg, CPU)
    try:
        assert det.info.kind == "onnx"
        assert det.info.synthetic is False
        assert choice.kind == "cpu"
    finally:
        det.close()


def test_factory_falls_back_to_the_stub_and_flags_it(tmp_path: Path) -> None:
    cfg = load(
        {
            "inference": {"model_path": str(tmp_path / "nothing.onnx")},
            "db": {"path": str(tmp_path / "t.db")},
        },
        use_env=False,
    )
    det, _ = get_detector(cfg, CPU)
    assert det.info.kind == "stub"
    assert det.info.synthetic is True


def test_onnx_detector_reports_its_stage_timings(detector: OnnxDetector) -> None:
    """Session CSV rows split each inference into preprocess, model and decode."""
    frame = np.full((480, 640, 3), 90, dtype=np.uint8)
    detector.detect(frame)
    timings = detector.timings
    assert timings.preprocess_ms > 0 and timings.infer_ms > 0 and timings.postprocess_ms >= 0
    assert timings.total_ms == pytest.approx(
        timings.preprocess_ms + timings.infer_ms + timings.postprocess_ms
    )


def test_a_hailo_choice_that_cannot_start_falls_back_to_the_cpu(
    fixture_model: Path, tmp_path: Path
) -> None:
    """A HEF the runtime rejects must not stop the session; it runs on the CPU and says why."""
    hef = tmp_path / "pepperwatch_hailo8l.hef"
    hef.write_bytes(b"not a real HEF")
    cfg = load(
        {
            "inference": {"model_path": str(fixture_model)},
            "db": {"path": str(tmp_path / "t.db")},
        },
        use_env=False,
    )
    choice = AcceleratorChoice(
        kind="hailo", name="Hailo-8L", profile="hailo", reason="test", hef_path=hef
    )
    det, final = get_detector(cfg, choice)
    try:
        assert det.info.kind == "onnx"
        assert final.kind == "cpu" and final.profile == "cpu"
        assert "pepperwatch_hailo8l.hef" in final.reason
    finally:
        det.close()
