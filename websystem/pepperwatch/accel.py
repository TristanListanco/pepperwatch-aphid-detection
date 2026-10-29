"""Detect a Raspberry Pi AI HAT+ and decide what runs inference this session.

Checked once when a session starts. The answer picks the detector (Hailo or
ONNX Runtime on the CPU) and the matching performance profile, and is recorded
with the session so every row of CSV can be traced to the hardware that
produced it.

Detection reads sysfs rather than shelling out to ``lspci``: Hailo's PCI vendor
ID is 0x1e60. The Hailo-8 and Hailo-8L share a device ID, so telling them
apart takes ``hailortcli fw-control identify``. The Hailo-10H on the AI HAT+ 2
has its own ID.

Each step that can fail (on the bus, driver loaded, device node accessible,
runtime importable, compiled model present) has its own reason string, so a
session that fell back to the CPU says exactly why, on the dashboard and in
``session.json``. Falling back is deliberate even when ``mode = "hailo"``: a
field device that refuses to start is worse than one running slower and saying
so.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

PCI_DEVICES = Path("/sys/bus/pci/devices")
DEV = Path("/dev")
HAT = Path("/proc/device-tree/hat")

HAILO_VENDOR = "0x1e60"
# 0x2864 is shared by the Hailo-8 and Hailo-8L.
HAILO_DEVICE_IDS = {"0x2864": "hailo8", "0x45c4": "hailo10h"}
ARCH_NAMES = {"hailo8": "Hailo-8", "hailo8l": "Hailo-8L", "hailo10h": "Hailo-10H"}
# Packages providing the PCIe driver, firmware and Python bindings, per chip.
ARCH_PACKAGES = {"hailo8": "hailo-all", "hailo8l": "hailo-all", "hailo10h": "hailo-h10-all"}


@dataclass(frozen=True, slots=True)
class HailoProbe:
    """What the system can see of a Hailo accelerator."""

    present: bool = False
    arch: str | None = None              # hailo8 | hailo8l | hailo10h
    arch_confirmed: bool = False         # False when 8 vs 8L could not be told apart
    pci_address: str | None = None
    pcie_link: str | None = None         # e.g. "8.0 GT/s PCIe x1"
    device_path: str | None = None       # /dev/hailo0 once the driver is loaded
    device_accessible: bool = False
    runtime_version: str | None = None   # HailoRT Python bindings
    runtime_error: str | None = None
    hat_product: str | None = None

    @property
    def name(self) -> str:
        if not self.arch:
            return "Hailo"
        if self.arch == "hailo8" and not self.arch_confirmed:
            return "Hailo-8/8L"
        return ARCH_NAMES.get(self.arch, self.arch)

    def as_dict(self) -> dict:
        return {**asdict(self), "name": self.name}


@dataclass(frozen=True, slots=True)
class AcceleratorChoice:
    """What this session runs inference on, and why."""

    kind: str                  # "hailo" | "cpu"
    name: str                  # "Hailo-8L" | "CPU"
    profile: str               # which [profiles.*] table applies
    reason: str
    hef_path: Path | None = None
    probe: HailoProbe | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "profile": self.profile,
            "reason": self.reason,
            "hef_path": str(self.hef_path) if self.hef_path else None,
            "hailo": self.probe.as_dict() if self.probe else None,
        }


def probe_hailo(
    *,
    pci_root: Path = PCI_DEVICES,
    dev_root: Path = DEV,
    hat_root: Path = HAT,
    identify: bool = True,
    import_runtime: bool = True,
) -> HailoProbe:
    """Look for a Hailo device and everything needed to use it."""
    hat_product = _read_dt(hat_root / "product")

    address = arch = link = None
    for device in sorted(pci_root.glob("*")) if pci_root.is_dir() else []:
        if _read(device / "vendor").lower() != HAILO_VENDOR:
            continue
        address = device.name
        arch = HAILO_DEVICE_IDS.get(_read(device / "device").lower(), "hailo8")
        speed, width = _read(device / "current_link_speed"), _read(device / "current_link_width")
        link = f"{speed} x{width}" if speed and width else speed or None
        break

    if address is None:
        return HailoProbe(hat_product=hat_product)

    confirmed = arch == "hailo10h"
    if arch == "hailo8" and identify:
        identified = identify_arch()
        if identified:
            arch, confirmed = identified, True

    nodes = sorted(dev_root.glob("hailo*")) if dev_root.is_dir() else []
    node = nodes[0] if nodes else None
    accessible = bool(node) and os.access(node, os.R_OK | os.W_OK)

    version = error = None
    if import_runtime:
        version, error = runtime_version()

    return HailoProbe(
        present=True,
        arch=arch,
        arch_confirmed=confirmed,
        pci_address=address,
        pcie_link=link,
        device_path=str(node) if node else None,
        device_accessible=accessible,
        runtime_version=version,
        runtime_error=error,
        hat_product=hat_product,
    )


def identify_arch() -> str | None:
    """Ask the firmware which chip this is. ``None`` if hailortcli cannot say."""
    try:
        result = subprocess.run(
            ["hailortcli", "fw-control", "identify"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_identify(result.stdout)


def parse_identify(text: str) -> str | None:
    """``Device Architecture: HAILO8L`` -> ``hailo8l``."""
    match = re.search(r"Device Architecture:\s*(\S+)", text)
    if not match:
        return None
    arch = match.group(1).strip().lower().replace("_", "").replace("-", "")
    return arch if arch in ARCH_NAMES else None


def runtime_version() -> tuple[str | None, str | None]:
    """HailoRT's Python bindings: (version, None) or (None, why not)."""
    try:
        import hailo_platform  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — ImportError, or a driver/ABI failure at import
        return None, f"{type(exc).__name__}: {exc}"
    version = getattr(hailo_platform, "__version__", None)
    if not version:
        try:
            from importlib.metadata import version as dist_version

            version = dist_version("hailort")
        except Exception:  # noqa: BLE001
            version = "unknown"
    return str(version), None


def hef_candidates(cfg: Config, arch: str | None, *, confirmed: bool = True) -> list[Path]:
    """Where to look for a compiled model, best match first.

    A HEF built for the Hailo-8L also runs on the Hailo-8 (at Hailo-8L speed),
    but not the other way round. So when the chip could not be identified, the
    8L build is tried before the 8.
    """
    configured = cfg.hef_path
    stem, folder = configured.stem, configured.parent

    order: list[str] = []
    if arch == "hailo8" and not confirmed:
        order = ["hailo8l", "hailo8"]
    elif arch == "hailo8":
        order = ["hailo8", "hailo8l"]
    elif arch:
        order = [arch]

    paths = [folder / f"{stem}_{name}.hef" for name in order]
    paths.append(configured)
    return paths


def find_hef(cfg: Config, probe: HailoProbe) -> Path | None:
    for candidate in hef_candidates(cfg, probe.arch, confirmed=probe.arch_confirmed):
        if candidate.is_file():
            return candidate
    return None


def choose_accelerator(cfg: Config, probe: HailoProbe | None = None) -> AcceleratorChoice:
    """Decide between the AI HAT+ and the CPU for this session."""
    mode = str(cfg.accelerator.mode).lower()
    if mode == "cpu":
        return _cpu("[accelerator] mode is cpu")

    probe = probe if probe is not None else probe_hailo()
    problem = _hailo_problem(cfg, probe)
    if problem:
        if mode == "hailo":
            log.error("AI HAT+ requested but unusable: %s; falling back to the CPU", problem)
            problem = f"requested the AI HAT+ but {problem}"
        return _cpu(problem, probe)

    hef = find_hef(cfg, probe)
    return AcceleratorChoice(
        kind="hailo",
        name=probe.name,
        profile="hailo",
        reason=f"{probe.name} on PCIe {probe.pcie_link or '?'}, HailoRT {probe.runtime_version}",
        hef_path=hef,
        probe=probe,
    )


def _hailo_problem(cfg: Config, probe: HailoProbe) -> str | None:
    """Why the HAT cannot be used, or ``None`` if it can."""
    if not probe.present:
        return "no AI HAT+ on the PCIe bus"
    package = ARCH_PACKAGES.get(probe.arch or "hailo8", "hailo-all")
    if not probe.device_path:
        return (
            f"{probe.name} found but its driver is not loaded (no /dev/hailo0); "
            f"install {package} and reboot"
        )
    if not probe.device_accessible:
        return f"no read/write permission on {probe.device_path}"
    if not probe.runtime_version:
        return f"HailoRT Python bindings unavailable ({probe.runtime_error}); install {package}"
    if find_hef(cfg, probe) is None:
        tried = ", ".join(p.name for p in hef_candidates(cfg, probe.arch, confirmed=probe.arch_confirmed))
        return f"no compiled model for the {probe.name} (looked for {tried} in models/)"
    return None


def _cpu(reason: str, probe: HailoProbe | None = None) -> AcceleratorChoice:
    return AcceleratorChoice(kind="cpu", name="CPU", profile="cpu", reason=reason, probe=probe)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_dt(path: Path) -> str | None:
    """A device-tree string property, which is NUL-terminated."""
    try:
        return path.read_bytes().decode("utf-8", "replace").rstrip("\x00").strip() or None
    except OSError:
        return None
