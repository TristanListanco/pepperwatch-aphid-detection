"""HAL factories.

``get_camera`` and ``get_telemetry`` are the only entry points the rest of the
system uses. Platform modules are imported lazily so that importing this
package on a Mac never touches Picamera2, and importing it on a Pi never
requires the mock frame cache to exist.
"""

from __future__ import annotations

import logging

from ..config import Config
from .base import (
    CameraError,
    CameraInfo,
    CameraSource,
    Frame,
    Telemetry,
    TelemetrySource,
)

__all__ = [
    "CameraError",
    "CameraInfo",
    "CameraSource",
    "Frame",
    "Telemetry",
    "TelemetrySource",
    "get_camera",
    "get_telemetry",
]

log = logging.getLogger(__name__)


def get_camera(cfg: Config, *, hold: bool = False) -> CameraSource:
    """Build the camera for this platform.

    ``hold`` freezes the mock on a single frame; the overlay-alignment tests
    rely on it and it is ignored on real hardware.
    """
    kind = cfg.platform_kind
    if kind == "pi":
        from .pi import build_pi_camera

        return build_pi_camera(cfg)

    from .mock import MockCamera

    return MockCamera(
        cache_dir=cfg.path("var/mock_frames"),
        width=cfg.camera.preview_width,
        height=cfg.camera.preview_height,
        fps=cfg.camera.preview_fps,
        hold=hold,
    )


def get_telemetry(cfg: Config) -> TelemetrySource:
    """Build the telemetry source for this platform."""
    if cfg.platform_kind == "pi":
        from .pi import PiTelemetry

        return PiTelemetry(cfg)

    from .mock import MockTelemetry

    return MockTelemetry()
