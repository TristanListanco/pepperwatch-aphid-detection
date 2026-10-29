"""Find the cameras attached to this Pi.

Run at the start of every session, so swapping a USB webcam for the CSI module
(or the reverse) needs only a restart, not a config edit.

Two sources, because the two kinds of camera are visible in different places:

**CSI** cameras on the ribbon connector are enumerated by libcamera, through
``Picamera2.global_camera_info()``. libcamera also lists UVC webcams on some
builds; those entries are skipped here and picked up by the USB scan instead,
so each camera is reported exactly once and opened with the driver that suits
it.

**USB** webcams are found in sysfs, not by probing ``/dev/video*``. A Pi 5
exposes thirty-odd video nodes for its own camera front end, ISP and HEVC
decoder, none of which is a camera; the USB ones are those bound to the
``uvcvideo`` driver. Each UVC camera also registers a metadata node, which has
``index`` 1 and is skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SYSFS_VIDEO = Path("/sys/class/video4linux")


@dataclass(frozen=True, slots=True)
class CameraCandidate:
    """A camera that was found, before it is opened."""

    kind: str          # "csi" | "usb"
    device: str        # CSI camera number ("0") or USB device path ("/dev/video0")
    model: str
    detail: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        where = f"camera {self.device}" if self.kind == "csi" else self.device
        return f"{self.kind.upper()} {self.model} ({where})"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "device": self.device, "model": self.model, **self.detail}


def csi_cameras(info_provider: Callable[[], list[dict]] | None = None) -> list[CameraCandidate]:
    """CSI cameras known to libcamera. Empty if Picamera2 is unavailable."""
    if info_provider is None:
        try:
            from picamera2 import Picamera2  # type: ignore[import-not-found]
        except ImportError as exc:
            log.warning("Picamera2 not importable (%s); CSI cameras cannot be listed", exc)
            return []
        info_provider = Picamera2.global_camera_info

    try:
        entries = list(info_provider())
    except Exception as exc:  # noqa: BLE001 — a libcamera failure must not stop the USB scan
        log.warning("libcamera enumeration failed: %s", exc)
        return []

    found = []
    for position, entry in enumerate(entries):
        ident = str(entry.get("Id", ""))
        if "usb" in ident.lower():
            continue
        number = entry.get("Num", position)
        found.append(
            CameraCandidate(
                kind="csi",
                device=str(number),
                model=str(entry.get("Model", "unknown")),
                detail={"id": ident, "location": entry.get("Location"), "rotation": entry.get("Rotation")},
            )
        )
    return found


def usb_cameras(sysfs_root: Path = SYSFS_VIDEO) -> list[CameraCandidate]:
    """UVC webcams, one entry per physical camera."""
    if not sysfs_root.is_dir():
        return []

    found = []
    for node in sorted(sysfs_root.glob("video*"), key=_node_number):
        try:
            driver = (node / "device" / "driver").resolve().name
        except OSError:
            continue
        if driver != "uvcvideo":
            continue
        # Index 0 is the capture node; a UVC camera's metadata node is index 1.
        if _read(node / "index", "0") != "0":
            continue

        interface = (node / "device").resolve()
        usb_device = interface.parent
        vendor = _read(usb_device / "idVendor")
        product = _read(usb_device / "idProduct")
        found.append(
            CameraCandidate(
                kind="usb",
                device=f"/dev/{node.name}",
                model=_read(node / "name", "USB camera") or "USB camera",
                detail={"usb_id": f"{vendor}:{product}" if vendor and product else None},
            )
        )
    return found


def discover_cameras(
    *,
    info_provider: Callable[[], list[dict]] | None = None,
    sysfs_root: Path = SYSFS_VIDEO,
) -> list[CameraCandidate]:
    """Every attached camera, CSI first."""
    return [*csi_cameras(info_provider), *usb_cameras(sysfs_root)]


def choose_camera(
    candidates: list[CameraCandidate], *, kind: str = "auto", device: str = ""
) -> CameraCandidate | None:
    """Pick the camera the config asks for, or the best available.

    ``auto`` prefers CSI: on a Pi 5 it costs no USB bandwidth and Picamera2
    hands over frames without a JPEG decode.
    """
    kind = (kind or "auto").lower()
    pool = candidates if kind == "auto" else [c for c in candidates if c.kind == kind]
    if device:
        pool = [c for c in pool if c.device == str(device)]
    if kind == "auto":
        pool = sorted(pool, key=lambda c: 0 if c.kind == "csi" else 1)
    return pool[0] if pool else None


def _node_number(path: Path) -> int:
    digits = path.name.removeprefix("video")
    return int(digits) if digits.isdigit() else 1 << 30


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default
