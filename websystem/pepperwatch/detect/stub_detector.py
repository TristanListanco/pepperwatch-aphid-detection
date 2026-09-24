"""A stand-in detector for development, before the trained model exists.

This is **not** a model and never pretends to be one: ``info.synthetic`` is
True, the model version is ``stub-heuristic-1``, and every event it produces is
flagged in the database.

What it does is cheap classical computer vision that happens to suit the
subject. Aphid colonies and their feeding damage show up on a bell pepper leaf
as pale, low-saturation speckling against green tissue, so thresholding for
bright desaturated pixels inside green regions finds something genuinely
present in each photograph. That matters more than it sounds: boxes that track
real image content exercise the overlay alignment, the severity bands, and the
persistence triggers against realistic variation, whereas boxes at random
coordinates would make every one of those look fine while proving nothing.

Deterministic for a given frame — no RNG — so tests and benchmarks repeat.

Known limitation, recorded so nobody mistakes it for a model defect later: a
top-hat responds to *local* brightness excess, so it finds discrete speckling
well but under-detects the diffuse silvering of a uniformly heavily-damaged
leaf, where there is no local contrast to find. Tuning that away is not worth
doing — this file exists to be deleted the moment a trained model is dropped
into ``models/``.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..models import Detection
from .base import Detector, DetectorInfo

MODEL_VERSION = "stub-heuristic-1"


class StubDetector(Detector):
    """Finds pale speckled regions on green tissue and boxes them."""

    def __init__(
        self,
        class_names: list[str],
        *,
        input_size: int = 640,
        conf_threshold: float = 0.25,
        max_detections: int = 100,
        min_area_fraction: float = 0.0012,
        max_area_fraction: float = 0.18,
        cluster_cap: int = 12,
    ) -> None:
        self._class_names = list(class_names)
        self._input_size = int(input_size)
        self._conf_threshold = float(conf_threshold)
        self._max_detections = int(max_detections)
        self._min_area = float(min_area_fraction)
        self._max_area = float(max_area_fraction)
        # A real colony count on one leaf is single digits; capping here keeps
        # the severity bands meaningful instead of pinning everything to high.
        self._cluster_cap = int(cluster_cap)

        self._damage_id = (
            self._class_names.index("aphid_damage")
            if "aphid_damage" in self._class_names
            else max(len(self._class_names) - 1, 0)
        )
        self._healthy_id = (
            self._class_names.index("healthy") if "healthy" in self._class_names else 0
        )

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        height, width = bgr.shape[:2]
        frame_area = float(height * width)
        if frame_area <= 0:
            return []

        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hue, saturation, value = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        # Green tissue: hue roughly 25..95 in OpenCV's 0..179 scale, with enough
        # saturation and brightness to be foliage rather than shadow or soil.
        leaf = ((hue >= 25) & (hue <= 95) & (saturation >= 40) & (value >= 40)).astype(np.uint8)
        leaf = cv2.morphologyEx(leaf, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

        subject = self._dominant_foliage(leaf, frame_area)
        if subject is None:
            # Barely any foliage in view — a photo of soil or a shoe. Report
            # nothing rather than inventing damage in the dirt.
            return []

        # Stay clear of the leaf rim: specular highlights collect along edges and
        # would otherwise read as a ring of phantom clusters on every frame.
        interior = cv2.erode(subject, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)))
        if int(interior.sum()) < frame_area * 0.01:
            interior = subject

        # White top-hat isolates small BRIGHT features relative to their local
        # background, which is exactly what aphid speckling is. Thresholding
        # absolute brightness instead (the obvious first approach) cannot tell a
        # speckled leaf from a merely sunlit one, and fires on soil and mulch.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        tophat = cv2.morphologyEx(value, cv2.MORPH_TOPHAT, kernel)

        speckle = ((tophat >= 28) & (saturation <= 110)).astype(np.uint8)
        mask = cv2.bitwise_and(speckle, interior)

        # Merge neighbouring specks into the cluster an agronomist would count as
        # one, rather than emitting a box per speck.
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
        )

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates: list[Detection] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            fraction = (w * h) / frame_area
            if fraction < self._min_area or fraction > self._max_area:
                continue

            region = tophat[y : y + h, x : x + w]
            if region.size == 0:
                continue

            # Confidence from how densely the box is filled, how strong the local
            # brightness excess is, and how substantial the cluster is. Bounded to
            # 0.30..0.95 so it reads on the dashboard like a model score.
            filled = float(cv2.contourArea(contour)) / max(float(w * h), 1.0)
            strength = min(float(region.mean()) / 45.0, 1.0)
            size_term = min(fraction / 0.02, 1.0)
            confidence = 0.30 + 0.34 * filled + 0.19 * strength + 0.12 * size_term
            confidence = float(min(max(confidence, 0.30), 0.95))
            if confidence < self._conf_threshold:
                continue

            candidates.append(
                Detection(
                    class_id=self._damage_id,
                    class_name=self._class_names[self._damage_id],
                    confidence=round(confidence, 4),
                    x=x / width,
                    y=y / height,
                    w=w / width,
                    h=h / height,
                ).clipped()
            )

        candidates.sort(key=lambda d: d.confidence, reverse=True)
        detections = candidates[: min(self._max_detections, self._cluster_cap)]

        if not detections:
            # Nothing pale found on the subject leaf: report it as healthy, so the
            # clean-scan path through severity, persistence and the advisory panel
            # gets exercised too.
            ys, xs = np.nonzero(subject)
            if xs.size:
                x1, x2 = int(xs.min()), int(xs.max())
                y1, y2 = int(ys.min()), int(ys.max())
                detections = [
                    Detection(
                        class_id=self._healthy_id,
                        class_name=self._class_names[self._healthy_id],
                        confidence=0.71,
                        x=x1 / width,
                        y=y1 / height,
                        w=max(x2 - x1, 1) / width,
                        h=max(y2 - y1, 1) / height,
                    ).clipped()
                ]

        return detections

    def _dominant_foliage(self, leaf: np.ndarray, frame_area: float) -> np.ndarray | None:
        """Keep only the main subject: the largest leaf mass and its peers.

        A field photograph contains the leaf being inspected plus a whole canopy
        behind it. A real deployment frames one subject at a fixed focal length,
        so restricting to the dominant connected mass (and anything at least
        half its size) approximates that framing and stops the background
        canopy, soil, and plastic mulch from generating detections.
        """
        count, labels, stats, _ = cv2.connectedComponentsWithStats(leaf, connectivity=8)
        if count <= 1:
            return None

        areas = stats[1:, cv2.CC_STAT_AREA]
        largest = int(areas.max())
        if largest < frame_area * 0.03:
            return None

        keep = [i + 1 for i, area in enumerate(areas) if area >= largest * 0.5]
        mask = np.isin(labels, keep).astype(np.uint8)
        return mask

    @property
    def info(self) -> DetectorInfo:
        return DetectorInfo(
            kind="stub",
            model_version=MODEL_VERSION,
            description="Heuristic stand-in (pale speckling on green tissue) — NOT a trained model",
            input_size=self._input_size,
            class_names=list(self._class_names),
            synthetic=True,
            detail={
                "conf_threshold": self._conf_threshold,
                "min_area_fraction": self._min_area,
                "max_area_fraction": self._max_area,
                "cluster_cap": self._cluster_cap,
            },
        )
