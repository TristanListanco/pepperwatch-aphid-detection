"""Hardware access for the Raspberry Pi 5.

``open_cameras`` (``open_camera`` for one) and ``get_telemetry`` are the entry points the rest of the
system uses. Picamera2 is imported lazily, inside the camera driver, so tools
that only need telemetry or discovery do not pay its start-up cost.
"""

from __future__ import annotations

from ..config import Config
from .base import (
    CameraError,
    CameraInfo,
    CameraSource,
    Frame,
    Telemetry,
    TelemetrySource,
)
from .discovery import CameraCandidate, discover_cameras

__all__ = [
    "CameraCandidate",
    "CameraError",
    "CameraInfo",
    "CameraSource",
    "Frame",
    "Telemetry",
    "TelemetrySource",
    "discover_cameras",
    "get_telemetry",
    "open_camera",
    "open_cameras",
]


def open_camera(cfg: Config) -> tuple[CameraSource, list[CameraCandidate]]:
    """Discover the attached cameras and start the preferred one only."""
    from .pi import open_camera as _open

    return _open(cfg)


def open_cameras(cfg: Config) -> tuple[list[CameraSource], list[CameraCandidate]]:
    """Discover the attached cameras and start up to ``[camera].max_cameras``."""
    from .pi import open_cameras as _open

    return _open(cfg)


def get_telemetry(cfg: Config) -> TelemetrySource:
    from .pi import PiTelemetry

    return PiTelemetry(cfg)
