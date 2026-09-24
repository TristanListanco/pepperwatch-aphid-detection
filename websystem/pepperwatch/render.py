"""Drawing detections onto a frame.

The live preview is streamed **clean** and the browser draws boxes on a canvas
over it, which keeps the worker's CPU on inference rather than on
``cv2.rectangle`` and gives crisp labels at any phone's pixel ratio.

Saved images are the opposite case: an archived scan has to be self-contained,
because the Historical Detection Log is evidence and a JPEG that only makes
sense alongside a live overlay stream is not. So snapshots are annotated here,
once, at save time.
"""

from __future__ import annotations

import cv2
import numpy as np

from .models import Detection

# BGR. Amber for damage rather than pure red: it stays legible against both the
# dark green of leaf tissue and the pale plastic mulch that fills half these
# frames, where red goes muddy.
COLOUR_DAMAGE = (40, 130, 235)
COLOUR_HEALTHY = (90, 190, 110)
COLOUR_TEXT = (255, 255, 255)


def colour_for(detection: Detection) -> tuple[int, int, int]:
    return COLOUR_HEALTHY if detection.class_name == "healthy" else COLOUR_DAMAGE


def draw_detections(
    bgr: np.ndarray,
    detections: list[Detection],
    *,
    label: bool = True,
    thickness: int | None = None,
) -> np.ndarray:
    """Return a copy of ``bgr`` with boxes and confidence labels drawn.

    Line weight and text scale follow the frame's size so a 640-wide preview and
    a 1600-wide snapshot both come out legible.
    """
    canvas = bgr.copy()
    height, width = canvas.shape[:2]
    line = thickness if thickness is not None else max(1, round(min(width, height) / 320))
    scale = max(0.35, min(width, height) / 1400.0)

    for detection in detections:
        x1, y1, x2, y2 = detection.clipped().to_pixels(width, height)
        colour = colour_for(detection)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, line)

        if not label:
            continue

        text = f"{detection.class_name} {detection.confidence * 100:.0f}%"
        (text_w, text_h), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1
        )

        # Keep the whole label on-frame. A box against the top edge has no
        # room above it, so the label moves inside; a box against the right
        # edge would otherwise have its text clipped mid-word, which matters
        # because these annotated frames are the archived evidence.
        top = y1 - text_h - baseline - 2
        if top < 0:
            top = min(y1 + 2, max(0, height - text_h - baseline - 2))

        left = x1
        label_w = text_w + 6
        if left + label_w > width:
            left = max(0, width - label_w)

        cv2.rectangle(
            canvas,
            (left, top),
            (left + label_w, top + text_h + baseline + 2),
            colour,
            -1,
        )
        cv2.putText(
            canvas,
            text,
            (left + 3, top + text_h + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            COLOUR_TEXT,
            1,
            cv2.LINE_AA,
        )

    return canvas


def encode_jpeg(bgr: np.ndarray, quality: int = 75) -> bytes:
    """Encode to JPEG bytes, or raise if OpenCV refuses."""
    ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buffer.tobytes()


def make_thumbnail(bgr: np.ndarray, width: int) -> np.ndarray:
    """Downscale preserving aspect ratio, for the gallery grid."""
    height, source_width = bgr.shape[:2]
    if source_width <= width:
        return bgr.copy()
    target_height = max(1, round(height * width / source_width))
    return cv2.resize(bgr, (width, target_height), interpolation=cv2.INTER_AREA)
