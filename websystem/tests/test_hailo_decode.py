"""Decoding a Hailo HEF's on-chip NMS output into frame-space detections.

HailoRT delivers NMS results either split per class or as one packed buffer,
with boxes as [y_min, x_min, y_max, x_max, score] normalised to the
letterboxed square. Both layouts must decode identically, and boxes must come
back through the same letterbox inverse the CPU path uses.
"""

from __future__ import annotations

import numpy as np
import pytest

from pepperwatch.detect.postprocess import decode_hailo_nms, letterbox

CLASSES = ["healthy", "aphid_damage"]


def _transform():
    # A 640x480 frame letterboxed into 640: 80 px of padding top and bottom.
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    return transform


def _box_on_content(x1, y1, x2, y2, score):
    """A box given in frame pixels, expressed as Hailo would report it."""
    size, pad = 640.0, 80.0
    return [(y1 + pad) / size, x1 / size, (y2 + pad) / size, x2 / size, score]


def test_split_layout_maps_back_to_the_frame() -> None:
    output = [
        np.array([_box_on_content(64, 48, 192, 144, 0.9)], np.float32),  # healthy
        np.array([_box_on_content(320, 240, 384, 336, 0.7)], np.float32),  # damage
    ]
    detections = decode_hailo_nms(output, _transform(), CLASSES, conf_threshold=0.25)

    by_class = {d.class_name: d for d in detections}
    damage = by_class["aphid_damage"]
    assert damage.x == pytest.approx(320 / 640, abs=1e-4)
    assert damage.y == pytest.approx(240 / 480, abs=1e-4)
    assert damage.w == pytest.approx(64 / 640, abs=1e-4)
    assert damage.h == pytest.approx(96 / 480, abs=1e-4)
    assert detections[0].confidence == pytest.approx(0.9)  # best first


def test_boxes_come_back_as_plain_floats_ready_to_publish() -> None:
    """NumPy 2 keeps float32 through the arithmetic; json.dumps rejects it, which stopped the HAT session."""
    import json

    output = [np.zeros((0, 5), np.float32), np.array([_box_on_content(320, 240, 384, 336, 0.7)], np.float32)]
    (damage,) = decode_hailo_nms(output, _transform(), CLASSES, conf_threshold=0.25)
    assert all(type(v) is float for v in (damage.confidence, damage.x, damage.y, damage.w, damage.h))
    json.dumps(damage.as_dict())


def test_packed_layout_decodes_identically() -> None:
    split = [
        np.array([_box_on_content(64, 48, 192, 144, 0.9)], np.float32),
        np.array([_box_on_content(320, 240, 384, 336, 0.7),
                  _box_on_content(10, 10, 40, 40, 0.3)], np.float32),
    ]
    packed = np.concatenate([[1.0], split[0].ravel(), [2.0], split[1].ravel()]).astype(np.float32)

    a = decode_hailo_nms(split, _transform(), CLASSES, conf_threshold=0.25)
    b = decode_hailo_nms(packed, _transform(), CLASSES, conf_threshold=0.25)
    assert a == b and len(a) == 3


def test_batch_wrapped_and_dict_outputs_are_unwrapped() -> None:
    split = [np.zeros((0, 5), np.float32), np.array([_box_on_content(0, 0, 64, 48, 0.8)], np.float32)]
    assert len(decode_hailo_nms([split], _transform(), CLASSES, conf_threshold=0.5)) == 1
    assert len(decode_hailo_nms({"yolov8n/nms": split}, _transform(), CLASSES, conf_threshold=0.5)) == 1


def test_threshold_and_cap_apply() -> None:
    boxes = np.array([_box_on_content(i * 10, 0, i * 10 + 8, 8, 0.1 + i * 0.05) for i in range(12)], np.float32)
    output = [np.zeros((0, 5), np.float32), boxes]
    kept = decode_hailo_nms(output, _transform(), CLASSES, conf_threshold=0.4, max_detections=3)
    assert len(kept) == 3
    assert all(d.confidence >= 0.4 for d in kept)
    assert [d.confidence for d in kept] == sorted((d.confidence for d in kept), reverse=True)


def test_empty_output_means_no_detections() -> None:
    empty = [np.zeros((0, 5), np.float32), np.zeros((0, 5), np.float32)]
    assert decode_hailo_nms(empty, _transform(), CLASSES, conf_threshold=0.1) == []
    assert decode_hailo_nms(np.zeros(2, np.float32), _transform(), CLASSES, conf_threshold=0.1) == []


def test_a_malformed_packed_buffer_is_rejected() -> None:
    with pytest.raises(ValueError, match="malformed"):
        decode_hailo_nms(np.array([5.0, 0.1, 0.2], np.float32), _transform(), CLASSES, conf_threshold=0.1)


def test_the_wrong_class_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="classes"):
        decode_hailo_nms([np.zeros((0, 5))] * 3, _transform(), CLASSES, conf_threshold=0.1)


def test_the_hat_reports_the_thresholds_its_chip_applies(tmp_path) -> None:
    """With NMS on the chip, an IoU change waits for the next session, and the
    chip's own score cut is a floor under confidence: the record says so."""
    from pepperwatch.detect.hailo_detector import HailoDetector

    hef = tmp_path / "m.hef"
    hef.write_bytes(b"x")
    det = HailoDetector.__new__(HailoDetector)
    det._hef_path, det._input_size, det._class_names = hef, 640, ["healthy", "aphid_damage"]
    det._accelerator, det._runtime_version, det._nms = "Hailo-8L", "4.20.0", True
    det._conf_threshold, det._iou_threshold, det._chip_score = 0.25, 0.45, 0.05

    det.set_thresholds(conf=0.03, iou=0.3)
    detail = det.info.detail
    assert (detail["conf_threshold"], detail["iou_threshold"]) == (0.05, 0.45)

    det._nms = False  # NMS in Python: both apply at once
    det.set_thresholds(iou=0.3)
    assert det.info.detail["iou_threshold"] == 0.3


def _hat_reading(*answers):
    """A HailoDetector whose chip answers temperature reads in turn."""
    import threading
    from types import SimpleNamespace

    from pepperwatch.detect.hailo_detector import HailoDetector

    queue = list(answers)

    def read():
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    det = HailoDetector.__new__(HailoDetector)
    det._lock, det._temperature_ok, det._temperature_retry_at = threading.Lock(), True, 0.0
    chip = SimpleNamespace(control=SimpleNamespace(get_chip_temperature=read))
    det._vdevice = SimpleNamespace(get_physical_devices=lambda: [chip])
    return det


def test_a_failed_temperature_read_is_tried_again_later() -> None:
    """Given up for good, the session's peak would read low and the HAT's backoff go blind."""
    from types import SimpleNamespace

    det = _hat_reading(RuntimeError("control busy"), SimpleNamespace(ts0_temperature=71.2, ts1_temperature=73.9))
    assert det.chip_temperature() is None
    assert det.chip_temperature() is None  # not asked again straight away
    det._temperature_retry_at = 0.0  # 30 s later
    assert det.chip_temperature() == 73.9  # the hotter sensor


def test_a_runtime_without_the_call_is_not_asked_again() -> None:
    det = _hat_reading(AttributeError("no get_chip_temperature"))
    assert det.chip_temperature() is None
    det._temperature_retry_at = 0.0
    assert det.chip_temperature() is None and det._temperature_ok is False


def test_a_hat_that_will_not_start_says_why_in_plain_words() -> None:
    from pepperwatch.detect import start_hint

    busy = RuntimeError("Failed to create vdevice. status=HAILO_OUT_OF_PHYSICAL_DEVICES(74)")
    assert start_hint(busy).startswith("another program is probably using the AI HAT+")
    assert "HAILO_OUT_OF_PHYSICAL_DEVICES(74)" in start_hint(busy)  # the error itself is kept
    assert start_hint(RuntimeError("something new")) == "something new"
