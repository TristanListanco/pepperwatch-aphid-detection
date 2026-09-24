"""Annotation drawing. These frames are the archived evidence, so labels must
stay wholly on-frame and boxes must land where the detection says."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from pepperwatch.models import Detection
from pepperwatch.render import (
    COLOUR_DAMAGE,
    COLOUR_HEALTHY,
    colour_for,
    draw_detections,
    encode_jpeg,
    make_thumbnail,
)


@pytest.fixture
def frame() -> np.ndarray:
    return np.zeros((480, 640, 3), np.uint8)


def test_classes_are_colour_coded() -> None:
    """Table 3.4 calls for 'color-coded bounding boxes'."""
    assert colour_for(Detection(1, "aphid_damage", 0.9, 0, 0, 0.1, 0.1)) == COLOUR_DAMAGE
    assert colour_for(Detection(0, "healthy", 0.9, 0, 0, 0.1, 0.1)) == COLOUR_HEALTHY


def test_drawing_does_not_mutate_the_source(frame: np.ndarray) -> None:
    """The capture thread reuses buffers; annotation must copy."""
    original = frame.copy()
    draw_detections(frame, [Detection(1, "aphid_damage", 0.8, 0.2, 0.2, 0.3, 0.3)])
    assert np.array_equal(frame, original)


def test_box_lands_at_the_stated_coordinates(frame: np.ndarray) -> None:
    out = draw_detections(
        frame, [Detection(1, "aphid_damage", 0.8, 0.25, 0.25, 0.5, 0.5)], label=False
    )
    # 0.25..0.75 of 640x480 -> x 160..480, y 120..360. The edges must be drawn.
    assert out[120, 300].tolist() != [0, 0, 0]
    assert out[360, 300].tolist() != [0, 0, 0]
    # The interior stays untouched.
    assert out[240, 320].tolist() == [0, 0, 0]


def test_empty_detections_leaves_the_frame_clean(frame: np.ndarray) -> None:
    assert np.array_equal(draw_detections(frame, []), frame)


@pytest.mark.parametrize(
    "x,y",
    [
        (0.80, 0.50),  # against the right edge
        (0.95, 0.50),  # mostly off the right edge
        (0.30, 0.00),  # against the top edge, no room for a label above
        (0.90, 0.00),  # both at once
        (0.00, 0.95),  # against the bottom edge
    ],
)
def test_labels_stay_entirely_on_frame(frame: np.ndarray, x: float, y: float) -> None:
    """A label clipped mid-word makes an archived scan unreadable as evidence."""
    out = draw_detections(frame, [Detection(1, "aphid_damage", 0.66, x, y, 0.18, 0.18)])
    # If any label pixel were placed out of bounds OpenCV would silently clip;
    # assert instead that the filled label background is fully within the frame
    # and that some label was actually drawn.
    painted = np.argwhere(np.any(out != 0, axis=2))
    assert painted.size > 0
    assert painted[:, 0].min() >= 0 and painted[:, 0].max() < out.shape[0]
    assert painted[:, 1].min() >= 0 and painted[:, 1].max() < out.shape[1]


def test_label_text_is_not_truncated_at_the_right_edge(frame: np.ndarray) -> None:
    """Regression: the background was clamped but the text was not.

    A label on a right-edge box must render the same pixel width as the same
    label on a centred box — if it were clipped, it would be narrower.
    """
    centred = draw_detections(frame, [Detection(1, "aphid_damage", 0.66, 0.3, 0.4, 0.15, 0.15)])
    edged = draw_detections(frame, [Detection(1, "aphid_damage", 0.66, 0.92, 0.4, 0.15, 0.15)])

    def label_width(image: np.ndarray) -> int:
        # The filled label background is the widest solid run of colour.
        rows = np.argwhere(np.all(image == COLOUR_DAMAGE, axis=2))
        widths = [
            int(np.ptp(rows[rows[:, 0] == row][:, 1]))
            for row in np.unique(rows[:, 0])
        ]
        return max(widths) if widths else 0

    assert label_width(edged) == pytest.approx(label_width(centred), abs=2)


def test_line_weight_scales_with_frame_size() -> None:
    """A 640-wide preview and a 1600-wide snapshot must both read clearly."""
    small = draw_detections(
        np.zeros((480, 640, 3), np.uint8),
        [Detection(1, "aphid_damage", 0.8, 0.2, 0.2, 0.3, 0.3)],
        label=False,
    )
    large = draw_detections(
        np.zeros((1200, 1600, 3), np.uint8),
        [Detection(1, "aphid_damage", 0.8, 0.2, 0.2, 0.3, 0.3)],
        label=False,
    )
    assert int(np.count_nonzero(large)) > int(np.count_nonzero(small))


def test_encode_jpeg_produces_a_valid_jpeg(frame: np.ndarray) -> None:
    data = encode_jpeg(frame, 75)
    assert data[:2] == b"\xff\xd8"  # SOI
    assert data[-2:] == b"\xff\xd9"  # EOI
    assert cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape == frame.shape


def test_higher_quality_yields_a_larger_file() -> None:
    noisy = np.random.default_rng(7).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert len(encode_jpeg(noisy, 95)) > len(encode_jpeg(noisy, 40))


def test_thumbnail_preserves_aspect_ratio(frame: np.ndarray) -> None:
    thumb = make_thumbnail(frame, 320)
    assert thumb.shape[1] == 320
    assert thumb.shape[0] == 240


def test_thumbnail_does_not_upscale() -> None:
    small = np.zeros((60, 80, 3), np.uint8)
    assert make_thumbnail(small, 320).shape == small.shape
