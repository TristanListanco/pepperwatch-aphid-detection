"""Value types shared between the detector, the database, and the web layer.

These live outside both ``detect`` and ``db`` so neither has to import the
other. A :class:`Detection` is the single currency the whole system passes
around: the detector produces them, the database stores them, MQTT serialises
them, and the browser canvas draws them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Severity ordering, lowest first. Used to decide whether severity "moved"
# (which forces a database write) and to colour the KPI tile.
SEVERITY_ORDER = ("none", "low", "moderate", "high")


@dataclass(frozen=True, slots=True)
class Detection:
    """One detected box, in normalized frame coordinates.

    ``x``/``y`` are the top-left corner and ``w``/``h`` the extent, all as
    fractions of frame width and height. Normalized because the dashboard draws
    these on a canvas sized by whatever phone is holding it — see the note at
    the top of ``schema.sql``.
    """

    class_id: int
    class_name: str
    confidence: float
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        """Fraction of the frame this box covers."""
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Convert to integer pixel ``(x1, y1, x2, y2)`` for drawing."""
        return (
            int(round(self.x * width)),
            int(round(self.y * height)),
            int(round(self.x2 * width)),
            int(round(self.y2 * height)),
        )

    def clipped(self) -> "Detection":
        """Clamp to the frame. Boxes may legitimately overhang an edge."""
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        return Detection(
            class_id=self.class_id,
            class_name=self.class_name,
            confidence=self.confidence,
            x=x,
            y=y,
            w=min(self.w, 1.0 - x),
            h=min(self.h, 1.0 - y),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 4),
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "w": round(self.w, 5),
            "h": round(self.h, 5),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Detection":
        return cls(
            class_id=int(data["class_id"]),
            class_name=str(data["class_name"]),
            confidence=float(data["confidence"]),
            x=float(data["x"]),
            y=float(data["y"]),
            w=float(data["w"]),
            h=float(data["h"]),
        )


def severity_rank(severity: str) -> int:
    """Position in :data:`SEVERITY_ORDER`; unknown values sort lowest."""
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return 0


def union_area(detections: Iterable[Detection]) -> float:
    """Fraction of the frame covered by the union of the given boxes.

    Summing box areas would double-count overlaps, and overlapping boxes are the
    normal case for a dense aphid cluster — a summed figure can exceed 1.0 and
    would inflate the severity band.

    Computed exactly, by coordinate compression: slice the plane at every box
    edge in x, and within each vertical slab merge the y-intervals of the boxes
    spanning it. Exactness matters here because the severity thresholds sit at
    0.02 and 0.08, where a rasterised approximation's edge error is the same
    order as the decision boundary itself.

    O(n^2 log n) in the number of boxes, which at the configured cap of 100 is
    a few tens of thousands of operations — negligible per inference.
    """
    boxes = [d.clipped() for d in detections]
    boxes = [b for b in boxes if b.w > 0.0 and b.h > 0.0]
    if not boxes:
        return 0.0

    edges = sorted({b.x for b in boxes} | {b.x2 for b in boxes})
    total = 0.0

    for left, right in zip(edges, edges[1:]):
        slab_width = right - left
        if slab_width <= 0.0:
            continue

        # y-intervals of every box spanning this slab, merged.
        spans = sorted(
            (b.y, b.y2) for b in boxes if b.x <= left and b.x2 >= right
        )
        if not spans:
            continue

        covered = 0.0
        cur_start, cur_end = spans[0]
        for start, end in spans[1:]:
            if start > cur_end:
                covered += cur_end - cur_start
                cur_start, cur_end = start, end
            elif end > cur_end:
                cur_end = end
        covered += cur_end - cur_start

        total += slab_width * covered

    return min(total, 1.0)
