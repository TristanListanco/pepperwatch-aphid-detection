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
