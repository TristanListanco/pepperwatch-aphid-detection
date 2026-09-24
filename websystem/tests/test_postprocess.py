"""Letterbox, NMS, YOLOv8 decode, and severity banding."""

from __future__ import annotations

import random

import numpy as np
import pytest

from pepperwatch.detect.postprocess import (
    Letterbox,
    decode_yolov8,
    derive_severity,
    iou,
    letterbox,
    non_max_suppression,
    to_tensor,
)
from pepperwatch.models import Detection

CLASSES = ["healthy", "aphid_damage"]
SEVERITY_KW = dict(low_max_count=2, moderate_max_count=5, low_max_area=0.02, moderate_max_area=0.08)


# -- letterbox -------------------------------------------------------------


@pytest.mark.parametrize("shape", [(480, 640), (640, 480), (1080, 1920), (640, 640), (481, 641)])
def test_letterbox_produces_a_square_canvas(shape) -> None:
    image = np.zeros((*shape, 3), np.uint8)
    canvas, transform = letterbox(image, 640)
    assert canvas.shape == (640, 640, 3)
    assert transform.source_width == shape[1]
    assert transform.source_height == shape[0]


def test_letterbox_preserves_aspect_ratio() -> None:
    image = np.zeros((480, 640, 3), np.uint8)
    _, t = letterbox(image, 640)
    # 640x480 into 640x640: full width, letterboxed top and bottom.
    assert t.scale == pytest.approx(1.0)
    assert t.pad_x == 0.0
    assert t.pad_y == pytest.approx(80.0)


def test_letterbox_pad_value_matches_ultralytics() -> None:
    """Pad grey must be 114 or the model meets a border it never trained on."""
    canvas, _ = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    assert canvas[0, 0].tolist() == [114, 114, 114]


@pytest.mark.parametrize("shape", [(480, 640), (640, 480), (1080, 1920), (3024, 4032), (481, 641)])
def test_letterbox_roundtrip_within_one_pixel(shape) -> None:
    """The property that matters: to_tensor and to_frame are inverses.

    Drift here presents as poor model accuracy rather than as an arithmetic
    bug, so it is checked over many random boxes and several frame shapes.
    """
    rng = random.Random(20260922)
    _, transform = letterbox(np.zeros((*shape, 3), np.uint8), 640)
    height, width = shape

    for _ in range(200):
        x = rng.uniform(0.0, 0.8)
        y = rng.uniform(0.0, 0.8)
        w = rng.uniform(0.02, 1.0 - x)
        h = rng.uniform(0.02, 1.0 - y)

        back = transform.to_frame(*transform.to_tensor(x, y, w, h))

        # Compare in pixels: one pixel of tolerance is the meaningful unit.
        assert abs(back[0] - x) * width < 1.0
        assert abs(back[1] - y) * height < 1.0
        assert abs(back[2] - w) * width < 1.0
        assert abs(back[3] - h) * height < 1.0


def test_to_tensor_shape_and_range() -> None:
    canvas, _ = letterbox(np.full((480, 640, 3), 200, np.uint8), 640)
    tensor = to_tensor(canvas)
    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert 0.0 <= float(tensor.min()) and float(tensor.max()) <= 1.0


def test_to_tensor_swaps_bgr_to_rgb() -> None:
    """A pure-blue BGR frame must land in the red-last RGB channel order."""
    bgr = np.zeros((640, 640, 3), np.uint8)
    bgr[:, :, 0] = 255  # blue in BGR
    tensor = to_tensor(bgr)
    assert float(tensor[0, 0].mean()) == pytest.approx(0.0)   # R channel empty
    assert float(tensor[0, 2].mean()) == pytest.approx(1.0)   # B channel full


# -- IoU and NMS -----------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ((0, 0, 10, 10), (0, 0, 10, 10), 1.0),          # identical
        ((0, 0, 10, 10), (20, 20, 30, 30), 0.0),        # disjoint
        ((0, 0, 10, 10), (5, 0, 15, 10), 1 / 3),        # half overlap: 50/150
        ((0, 0, 10, 10), (0, 0, 5, 10), 0.5),           # contained: 50/100
        ((0, 0, 10, 10), (10, 10, 20, 20), 0.0),        # touching corners only
    ],
)
def test_iou_matches_hand_computed_values(a, b, expected) -> None:
    assert iou(a, b) == pytest.approx(expected)


def test_nms_suppresses_overlapping_same_class() -> None:
    boxes = [(0, 0, 10, 10), (1, 1, 11, 11), (50, 50, 60, 60)]
    keep = non_max_suppression(boxes, [0.9, 0.8, 0.7], [1, 1, 1], iou_threshold=0.45)
    assert keep == [0, 2]  # the 0.8 box overlaps the 0.9 box and loses


def test_nms_is_per_class_not_global() -> None:
    """An aphid cluster sits ON a leaf; both readings must survive."""
    boxes = [(0, 0, 10, 10), (0, 0, 10, 10)]
    keep = non_max_suppression(boxes, [0.9, 0.8], [0, 1], iou_threshold=0.45)
    assert sorted(keep) == [0, 1]


def test_nms_returns_highest_score_first() -> None:
    boxes = [(0, 0, 5, 5), (20, 20, 25, 25), (40, 40, 45, 45)]
    keep = non_max_suppression(boxes, [0.3, 0.9, 0.6], [1, 1, 1], iou_threshold=0.45)
    assert keep == [1, 2, 0]


def test_nms_honours_max_detections() -> None:
    boxes = [(i * 20, 0, i * 20 + 10, 10) for i in range(10)]
    keep = non_max_suppression(
        boxes, [0.5] * 10, [1] * 10, iou_threshold=0.45, max_detections=3
    )
    assert len(keep) == 3


# -- YOLOv8 head decode ----------------------------------------------------


def _head(entries: list[tuple[float, float, float, float, float, float]]) -> np.ndarray:
    """Build a (1, 4+2, anchors) head from (cx, cy, w, h, healthy, aphid) rows."""
    return np.array(entries, dtype=np.float32).T[None, ...]


def test_decode_filters_below_threshold() -> None:
    head = _head([(320, 320, 100, 100, 0.01, 0.10)])
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    assert decode_yolov8(head, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45) == []


def test_decode_maps_centre_form_to_frame_coordinates() -> None:
    """A box centred in the tensor must land centred in the frame."""
    head = _head([(320, 320, 64, 64, 0.02, 0.88)])
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    dets = decode_yolov8(head, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45)

    assert len(dets) == 1
    det = dets[0]
    assert det.class_name == "aphid_damage"
    assert det.confidence == pytest.approx(0.88, abs=1e-6)
    assert det.x + det.w / 2 == pytest.approx(0.5, abs=0.01)
    assert det.y + det.h / 2 == pytest.approx(0.5, abs=0.01)


def test_decode_accepts_transposed_head() -> None:
    head = _head([(320, 320, 64, 64, 0.02, 0.88)])
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    straight = decode_yolov8(head, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45)
    flipped = decode_yolov8(head[0].T, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45)
    assert straight == flipped


def test_decode_rejects_a_head_with_the_wrong_class_count() -> None:
    """Better a loud failure than plausible-looking garbage."""
    head = np.zeros((1, 9, 20), np.float32)  # 4 + 5 classes, but we declare 2
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    with pytest.raises(ValueError, match="expected 6"):
        decode_yolov8(head, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45)


def test_decode_clips_boxes_to_the_frame() -> None:
    head = _head([(10, 10, 200, 200, 0.01, 0.9)])  # runs off the top-left
    _, transform = letterbox(np.zeros((480, 640, 3), np.uint8), 640)
    det = decode_yolov8(head, transform, CLASSES, conf_threshold=0.25, iou_threshold=0.45)[0]
    assert det.x >= 0.0 and det.y >= 0.0
    assert det.x + det.w <= 1.0 + 1e-9
    assert det.y + det.h <= 1.0 + 1e-9


# -- severity --------------------------------------------------------------


def _boxes(count: int, size: float) -> list[Detection]:
    """``count`` non-overlapping aphid boxes of the given edge length."""
    out = []
    for i in range(count):
        row, col = divmod(i, 8)
        out.append(
            Detection(1, "aphid_damage", 0.9, 0.01 + col * 0.12, 0.01 + row * 0.12, size, size)
        )
    return out


def test_severity_none_when_nothing_damaged() -> None:
    healthy = [Detection(0, "healthy", 0.9, 0.1, 0.1, 0.3, 0.3)]
    assert derive_severity(healthy, **SEVERITY_KW) == ("none", 0.0)


@pytest.mark.parametrize(
    "count,size,expected",
    [
        (1, 0.05, "low"),        # 1 box, 0.25% area -> low on both axes
        (2, 0.05, "low"),        # at the count boundary
        (3, 0.05, "moderate"),   # count crosses into moderate
        (5, 0.05, "moderate"),   # at the upper count boundary
        (6, 0.05, "high"),       # count crosses into high
    ],
)
def test_severity_rises_with_cluster_count(count, size, expected) -> None:
    assert derive_severity(_boxes(count, size), **SEVERITY_KW)[0] == expected


def test_severity_rises_on_area_even_with_one_cluster() -> None:
    """One large confluent colony is serious even though it is a single box."""
    one_big = [Detection(1, "aphid_damage", 0.9, 0.1, 0.1, 0.5, 0.5)]  # 25% of frame
    band, area = derive_severity(one_big, **SEVERITY_KW)
    assert band == "high"
    assert area == pytest.approx(0.25)


def test_severity_takes_the_worse_of_count_and_area() -> None:
    """Either axis can raise the band; neither can lower it."""
    # 2 boxes (low by count) covering 18% (high by area) -> high
    two_big = [
        Detection(1, "aphid_damage", 0.9, 0.05, 0.05, 0.3, 0.3),
        Detection(1, "aphid_damage", 0.9, 0.55, 0.55, 0.3, 0.3),
    ]
    assert derive_severity(two_big, **SEVERITY_KW)[0] == "high"


def test_severity_ignores_healthy_boxes() -> None:
    mixed = _boxes(1, 0.05) + [Detection(0, "healthy", 0.9, 0.5, 0.5, 0.4, 0.4)] * 6
    band, area = derive_severity(mixed, **SEVERITY_KW)
    assert band == "low"
    assert area == pytest.approx(0.0025, abs=1e-4)


def test_severity_area_does_not_double_count_overlaps() -> None:
    """Stacked duplicate boxes must not inflate the band."""
    stacked = [Detection(1, "aphid_damage", 0.9, 0.1, 0.1, 0.12, 0.12)] * 2
    _, area = derive_severity(stacked, **SEVERITY_KW)
    assert area == pytest.approx(0.0144)
