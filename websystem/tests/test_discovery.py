"""Camera discovery: which CSI and USB cameras are attached, and which is used.

The USB scan reads sysfs, so these tests build a small fake of it: a Pi 5
exposes dozens of /dev/video* nodes for its own ISP and decoder, and only the
ones bound to uvcvideo (index 0) are cameras.
"""

from __future__ import annotations

import os
from pathlib import Path

from pepperwatch.hal.discovery import CameraCandidate, choose_camera, csi_cameras, usb_cameras


def _node(root: Path, name: str, *, driver: str, index: str = "0", label: str = "cam",
          usb: tuple[str, str] | None = None) -> None:
    devices = root / "devices"
    drivers = root / "drivers"
    (drivers / driver).mkdir(parents=True, exist_ok=True)

    parent = devices / f"dev_{name}"
    interface = parent / "interface"
    interface.mkdir(parents=True)
    os.symlink(drivers / driver, interface / "driver")
    if usb:
        (parent / "idVendor").write_text(usb[0] + "\n")
        (parent / "idProduct").write_text(usb[1] + "\n")

    node = root / "video4linux" / name
    node.mkdir(parents=True)
    (node / "name").write_text(label + "\n")
    (node / "index").write_text(index + "\n")
    os.symlink(interface, node / "device")


def test_usb_scan_finds_only_uvc_capture_nodes(tmp_path: Path) -> None:
    _node(tmp_path, "video0", driver="rp1-cfe", label="rp1-cfe")
    _node(tmp_path, "video19", driver="rpivid", label="rpivid")
    _node(tmp_path, "video8", driver="uvcvideo", label="HD Pro Webcam C920", usb=("046d", "082d"))
    _node(tmp_path, "video9", driver="uvcvideo", index="1", label="HD Pro Webcam C920")

    found = usb_cameras(tmp_path / "video4linux")

    assert [c.device for c in found] == ["/dev/video8"]
    assert found[0].kind == "usb"
    assert found[0].model == "HD Pro Webcam C920"
    assert found[0].detail["usb_id"] == "046d:082d"


def test_usb_scan_tolerates_a_missing_sysfs(tmp_path: Path) -> None:
    assert usb_cameras(tmp_path / "absent") == []


def test_csi_listing_skips_usb_cameras_libcamera_also_reports() -> None:
    entries = [
        {"Model": "imx708", "Id": "/base/axi/pcie@120000/rp1/i2c@88000/imx708@1a", "Num": 0, "Location": 2},
        {"Model": "C920", "Id": "/base/axi/pcie@120000/rp1/usb@200000-1:1.0-046d:082d", "Num": 1},
    ]
    found = csi_cameras(lambda: entries)
    assert [(c.kind, c.device, c.model) for c in found] == [("csi", "0", "imx708")]


def test_csi_listing_survives_a_libcamera_failure() -> None:
    def broken():
        raise RuntimeError("no camera manager")

    assert csi_cameras(broken) == []


CSI = CameraCandidate(kind="csi", device="0", model="imx708")
USB = CameraCandidate(kind="usb", device="/dev/video8", model="C920")


def test_auto_prefers_the_csi_camera() -> None:
    assert choose_camera([USB, CSI], kind="auto") is CSI


def test_auto_uses_a_webcam_when_there_is_no_csi_camera() -> None:
    assert choose_camera([USB], kind="auto") is USB


def test_an_explicit_kind_and_device_are_honoured() -> None:
    assert choose_camera([CSI, USB], kind="usb") is USB
    assert choose_camera([CSI, USB], kind="usb", device="/dev/video3") is None
    assert choose_camera([CSI, USB], kind="csi", device="0") is CSI


def test_nothing_attached_means_no_choice() -> None:
    assert choose_camera([], kind="auto") is None
