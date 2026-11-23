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


def compose_cameras(panels: list[tuple[np.ndarray, str]]) -> np.ndarray:
    """Several cameras' annotated frames as one photo, each named in its corner.

    Two sit side by side; three or four go in a 2x2 grid, so a phone's gallery
    thumbnail stays readable. Every panel is scaled to the smallest height
    among them and centred in an equal cell, so a 16:9 still and a 4:3 webcam
    frame can share a photo without being stretched.
    """
    height = min(image.shape[0] for image, _ in panels)
    scaled = [
        image if image.shape[0] == height
        else cv2.resize(image, (max(1, round(image.shape[1] * height / image.shape[0])), height),
                        interpolation=cv2.INTER_AREA)
        for image, _ in panels
    ]
    width = max(image.shape[1] for image in scaled)
    columns = min(2, len(panels))
    rows = -(-len(panels) // columns)
    gap = max(2, height // 160)
    canvas = np.full(
        (rows * height + (rows - 1) * gap, columns * width + (columns - 1) * gap, 3), 24, np.uint8
    )
    scale = max(0.5, height / 900.0)
    thickness = max(1, round(scale * 1.5))
    inset = max(4, height // 24)  # clear of a gallery tile's rounded corner
    for slot, (image, (_, name)) in enumerate(zip(scaled, panels)):
        row, column = divmod(slot, columns)
        top, left = row * (height + gap), column * (width + gap)
        x = left + (width - image.shape[1]) // 2
        canvas[top:top + height, x:x + image.shape[1]] = image
        # Hershey fonts draw ASCII only.
        text = name.replace(" · ", " - ").encode("ascii", "replace").decode()
        (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        pad = max(4, text_h // 2)
        x0, y0 = left + inset, top + inset
        cv2.rectangle(canvas, (x0, y0), (x0 + text_w + 2 * pad, y0 + text_h + baseline + 2 * pad), (0, 0, 0), -1)
        cv2.putText(canvas, text, (x0 + pad, y0 + pad + text_h), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, COLOUR_TEXT, thickness, cv2.LINE_AA)
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


def digital_zoom(bgr: np.ndarray, zoom: float) -> np.ndarray:
    """The middle 1/zoom of the picture, enlarged back to its size.

    For a camera that cannot crop on its sensor (a USB webcam): leaves come out
    bigger, which the detector finds more often, but with no more detail.
    """
    if zoom <= 1.0:
        return bgr
    height, width = bgr.shape[:2]
    crop_h, crop_w = round(height / zoom), round(width / zoom)
    top, left = (height - crop_h) // 2, (width - crop_w) // 2
    return cv2.resize(bgr[top:top + crop_h, left:left + crop_w], (width, height), interpolation=cv2.INTER_LINEAR)
