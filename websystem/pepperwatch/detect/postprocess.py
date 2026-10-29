"""Pre- and post-processing for the detection pipeline.

This implements the "Input and Processing" and "Post-Processing & Logic" blocks
of manuscript Figure 3.3: resize with letterbox padding, BGR to RGB, normalize
to a tensor, then confidence thresholding, Non-Maximum Suppression, and scaling
the surviving boxes back to frame coordinates.

The letterbox transform is the part most likely to go quietly wrong. Padding a
non-square frame into a square tensor shifts every coordinate, and if the
inverse does not undo that shift by exactly the same amount, boxes drift — a
bug that looks like poor model accuracy rather than an arithmetic error.
:class:`Letterbox` therefore carries the scale and padding it applied, and
:meth:`Letterbox.to_frame` is its exact inverse; ``tests/test_postprocess.py``
asserts the round trip over hundreds of random boxes.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..models import Detection, union_area


@dataclass(frozen=True, slots=True)
class Letterbox:
    """A performed letterbox, and the inverse mapping back to frame space.

    ``scale`` is what the source was multiplied by; ``pad_x``/``pad_y`` are the
    pixel offsets of the image content inside the square tensor.
    """

    source_width: int
    source_height: int
    size: int
    scale: float
    pad_x: float
    pad_y: float

    def to_frame(self, x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
        """Map tensor-space pixel corners back to normalized frame coordinates.

        Returns ``(x, y, w, h)`` as fractions of the source frame — the form
        :class:`~pepperwatch.models.Detection` stores.
        """
        fx1 = (x1 - self.pad_x) / self.scale
        fy1 = (y1 - self.pad_y) / self.scale
        fx2 = (x2 - self.pad_x) / self.scale
        fy2 = (y2 - self.pad_y) / self.scale

        return (
            fx1 / self.source_width,
            fy1 / self.source_height,
            (fx2 - fx1) / self.source_width,
            (fy2 - fy1) / self.source_height,
        )

    def to_tensor(self, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """Forward mapping, normalized frame coords to tensor pixel corners.

        Only the tests and the stub detector need this direction, but having it
        next to its inverse is what makes the round-trip property checkable.
        """
        fx1, fy1 = x * self.source_width, y * self.source_height
        fx2, fy2 = (x + w) * self.source_width, (y + h) * self.source_height
        return (
            fx1 * self.scale + self.pad_x,
            fy1 * self.scale + self.pad_y,
            fx2 * self.scale + self.pad_x,
            fy2 * self.scale + self.pad_y,
        )


def letterbox(
    bgr: np.ndarray, size: int, *, pad_value: int = 114
) -> tuple[np.ndarray, Letterbox]:
    """Resize preserving aspect ratio and pad to ``size`` x ``size``.

    ``pad_value`` is 114 to match Ultralytics' convention, so a model trained
    with their pipeline meets the same grey border at inference that it saw
    during training. A different pad colour is a subtle train/serve skew.
    """
    height, width = bgr.shape[:2]
    scale = min(size / width, size / height)
    new_width = int(round(width * scale))
    new_height = int(round(height * scale))

    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(bgr, (new_width, new_height), interpolation=interpolation)

    canvas = np.full((size, size, 3), pad_value, dtype=bgr.dtype)
    pad_x = (size - new_width) / 2.0
    pad_y = (size - new_height) / 2.0
    top, left = int(round(pad_y)), int(round(pad_x))
    canvas[top : top + new_height, left : left + new_width] = resized

    # Report the integer offsets actually used, not the fractional ideal, so
    # the inverse undoes precisely what was done.
    return canvas, Letterbox(
        source_width=width,
        source_height=height,
        size=size,
        scale=scale,
        pad_x=float(left),
        pad_y=float(top),
    )


def to_tensor(letterboxed_bgr: np.ndarray) -> np.ndarray:
    """BGR HWC uint8 to the RGB NCHW float32 batch the model expects.

    This is Figure 3.3's "Color Conversion" and "Normalization" blocks: BGR to
    RGB, scale to 0..1, transpose to channels-first, add the batch axis.
    """
    rgb = cv2.cvtColor(letterboxed_bgr, cv2.COLOR_BGR2RGB)
    normalized = rgb.astype(np.float32) / 255.0
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None, ...])


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection over union of two ``(x1, y1, x2, y2)`` boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter_w, inter_h = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = inter_w * inter_h
    if intersection <= 0.0:
        return 0.0

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0.0 else 0.0


def non_max_suppression(
    boxes: list[tuple[float, float, float, float]],
    scores: list[float],
    class_ids: list[int],
    *,
    iou_threshold: float,
    max_detections: int = 100,
) -> list[int]:
    """Greedy NMS, applied per class. Returns surviving indices, best first.

    Per class, not globally: a healthy leaf and an aphid cluster can legitimately
    overlap in frame — the cluster sits *on* the leaf — and class-agnostic
    suppression would delete one of the two readings the dashboard needs.
    """
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    keep: list[int] = []

    for index in order:
        if len(keep) >= max_detections:
            break
        suppressed = False
        for kept in keep:
            if class_ids[kept] != class_ids[index]:
                continue
            if iou(boxes[index], boxes[kept]) > iou_threshold:
                suppressed = True
                break
        if not suppressed:
            keep.append(index)

    return keep


def decode_yolov8(
    output: np.ndarray,
    transform: Letterbox,
    class_names: list[str],
    *,
    conf_threshold: float,
    iou_threshold: float,
    max_detections: int = 100,
) -> list[Detection]:
    """Turn a raw YOLOv8 head tensor into frame-space detections.

    YOLOv8 emits ``(1, 4 + num_classes, num_anchors)`` with box coordinates as
    centre-x, centre-y, width, height in tensor pixels, and one score per class
    with no separate objectness term. Anything else means the export did not
    come from the pipeline this expects, so the shape is checked rather than
    assumed — a silently transposed tensor would produce plausible-looking
    garbage.
    """
    array = np.asarray(output)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        raise ValueError(f"expected a 2D head after removing the batch axis, got {array.shape}")

    expected_rows = 4 + len(class_names)
    if array.shape[0] != expected_rows and array.shape[1] == expected_rows:
        # Some exporters emit (anchors, 4+classes); accept it explicitly.
        array = array.T
    if array.shape[0] != expected_rows:
        raise ValueError(
            f"head has {array.shape[0]} rows; expected {expected_rows} "
            f"for {len(class_names)} classes ({class_names})"
        )

    class_scores = array[4:, :]
    best_class = np.argmax(class_scores, axis=0)
    best_score = class_scores[best_class, np.arange(class_scores.shape[1])]

    above = np.flatnonzero(best_score >= conf_threshold)
    if above.size == 0:
        return []

    cx, cy, bw, bh = array[0, above], array[1, above], array[2, above], array[3, above]
    corners = [
        (float(cx[i] - bw[i] / 2), float(cy[i] - bh[i] / 2),
         float(cx[i] + bw[i] / 2), float(cy[i] + bh[i] / 2))
        for i in range(above.size)
    ]
    scores = [float(best_score[i]) for i in range(above.size)]
    ids = [int(best_class[above[i]]) for i in range(above.size)]

    keep = non_max_suppression(
        corners, scores, ids, iou_threshold=iou_threshold, max_detections=max_detections
    )

    detections: list[Detection] = []
    for index in keep:
        x, y, w, h = transform.to_frame(*corners[index])
        class_id = ids[index]
        detections.append(
            Detection(
                class_id=class_id,
                class_name=class_names[class_id] if class_id < len(class_names) else str(class_id),
                confidence=scores[index],
                x=x,
                y=y,
                w=w,
                h=h,
            ).clipped()
        )
    return detections


def decode_hailo_nms(
    output,
    transform: Letterbox,
    class_names: list[str],
    *,
    conf_threshold: float,
    max_detections: int = 100,
) -> list[Detection]:
    """Turn a Hailo HEF's on-chip NMS output into frame-space detections.

    A YOLOv8 HEF compiled with NMS (the Hailo model zoo default) returns, for
    each class, boxes as ``[y_min, x_min, y_max, x_max, score]`` normalised to
    the model input, i.e. to the letterboxed square. HailoRT hands that over in
    one of two shapes, both accepted here:

    * already split: a list with one ``(N, 5)`` array per class, possibly
      wrapped in a one-element batch list;
    * packed (``HAILO_NMS_BY_CLASS``): a flat float buffer holding, for each
      class in turn, a box count followed by that many 5-tuples.

    Suppression already ran on the chip, so only the confidence threshold and
    the detection cap remain. Note the axis order: y before x, unlike YOLO's
    own head.
    """
    per_class = _split_hailo_nms(output, len(class_names))
    size = float(transform.size)

    candidates: list[tuple[float, int, tuple[float, float, float, float]]] = []
    for class_id, boxes in enumerate(per_class):
        for y1, x1, y2, x2, score in boxes:
            if score < conf_threshold:
                continue
            candidates.append((float(score), class_id, (x1 * size, y1 * size, x2 * size, y2 * size)))
    candidates.sort(key=lambda item: item[0], reverse=True)

    detections: list[Detection] = []
    for score, class_id, corners in candidates[:max_detections]:
        x, y, w, h = transform.to_frame(*corners)
        if w <= 0.0 or h <= 0.0:
            continue
        detections.append(
            Detection(
                class_id=class_id,
                class_name=class_names[class_id] if class_id < len(class_names) else str(class_id),
                confidence=score,
                x=x,
                y=y,
                w=w,
                h=h,
            ).clipped()
        )
    return detections


def _split_hailo_nms(output, num_classes: int) -> list[np.ndarray]:
    """Normalise either NMS layout to one ``(N, 5)`` float array per class."""
    if isinstance(output, dict):
        if len(output) != 1:
            raise ValueError(f"expected one NMS output, got {sorted(output)}")
        output = next(iter(output.values()))

    if isinstance(output, (list, tuple)):
        if len(output) == 1 and isinstance(output[0], (list, tuple)):
            output = output[0]  # batch of one
        arrays = [np.asarray(item, dtype=np.float32) for item in output]
        if len(arrays) != num_classes:
            raise ValueError(f"NMS output has {len(arrays)} classes; expected {num_classes}")
        return [a.reshape(-1, 5) if a.size else np.zeros((0, 5), np.float32) for a in arrays]

    flat = np.asarray(output, dtype=np.float32).ravel()
    per_class: list[np.ndarray] = []
    offset = 0
    for _ in range(num_classes):
        if offset >= flat.size:
            per_class.append(np.zeros((0, 5), np.float32))
            continue
        count = int(flat[offset])
        offset += 1
        if count < 0 or offset + count * 5 > flat.size:
            raise ValueError(f"packed NMS buffer is malformed at offset {offset - 1} (count {count})")
        per_class.append(flat[offset : offset + count * 5].reshape(count, 5))
        offset += count * 5
    return per_class


def derive_severity(
    detections: list[Detection],
    *,
    low_max_count: int,
    moderate_max_count: int,
    low_max_area: float,
    moderate_max_area: float,
    damage_class: str = "aphid_damage",
) -> tuple[str, float]:
    """Map detections onto a four-band severity, and report damaged area.

    The model predicts only ``healthy`` and ``aphid_damage``, but Figure 3.4
    draws a Severity tile and the Actionable Output Panel needs bands to key its
    advice to. Chapter III never defines them, so this is the operationalization
    the thesis should state: severity rises on **either** the number of distinct
    clusters **or** the fraction of frame they cover, whichever is worse.

    Either-not-both is deliberate: a single large confluent colony and a dozen
    small scattered ones are both serious, and requiring both conditions would
    miss each of them.
    """
    damaged = [d for d in detections if d.class_name == damage_class]
    if not damaged:
        return "none", 0.0

    count = len(damaged)
    area = union_area(damaged)

    by_count = "low" if count <= low_max_count else "moderate" if count <= moderate_max_count else "high"
    by_area = "low" if area <= low_max_area else "moderate" if area <= moderate_max_area else "high"

    ranks = {"low": 1, "moderate": 2, "high": 3}
    worst = by_count if ranks[by_count] >= ranks[by_area] else by_area
    return worst, area


def severity_from_config(detections: list[Detection], cfg) -> tuple[str, float]:
    """:func:`derive_severity` with thresholds taken from config."""
    return derive_severity(
        detections,
        low_max_count=cfg.severity.low_max_count,
        moderate_max_count=cfg.severity.moderate_max_count,
        low_max_area=cfg.severity.low_max_area,
        moderate_max_area=cfg.severity.moderate_max_area,
    )
