"""Identify the single-board computer and how much it can be asked to do.

Pepper Watch runs on the Raspberry Pi 5 first, but also on older Raspberry Pis
and on other 64-bit ARM boards such as the Orange Pi and Banana Pi lines. They
differ by an order of magnitude in CPU speed and RAM, so one set of settings
cannot suit them all: the Pi 5's would leave a 1 GB board swapping and its
service memory limits above physical RAM.

Detection reads what Linux already knows, with no vendor tools:

* ``/proc/device-tree/model`` and ``compatible`` name the board and its SoC
  (``"OrangePi Zero3"``, ``"xunlong,orangepi-zero3"``,
  ``"allwinner,sun50i-h618"``);
* ``/proc/cpuinfo`` lists each core's ARM part number, which says what class of
  core it is (Cortex-A76, A72, A55, ...) far more reliably than a model string;
* ``/proc/meminfo`` gives the RAM.

From those comes a **tier**, the CPU class capped by RAM:

``high``  Cortex-A76 class (Pi 5, RK3588 boards such as the Orange Pi 5)
``mid``   Cortex-A72 class (Pi 4, Pi 400, CM4, RK3399)
``low``   Cortex-A53/A55 class, or under 1.5 GB RAM (Pi 3, Orange Pi Zero 3,
          Banana Pi M4 Berry, RK3566)
``tiny``  under 768 MB RAM (Pi Zero 2 W)

``config/profiles/<tier>.toml`` holds the settings for each tier, and
``config/profiles/<board>.toml`` may add board-specific ones on top.

Standard library only, so ``deploy/install.sh`` can run this with the system
Python before any dependency is installed::

    python3 -m pepperwatch.board          # human-readable
    python3 -m pepperwatch.board --shell  # KEY=value lines for a shell
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

DEVICE_TREE = Path("/proc/device-tree")
CPUINFO = Path("/proc/cpuinfo")
MEMINFO = Path("/proc/meminfo")

TIERS = ("tiny", "low", "mid", "high")

# ARM's part numbers (implementer 0x41), by how fast the core is for this
# workload. YOLOv8n on ONNX Runtime is dominated by NEON throughput; the
# A76-class cores are roughly three times an A72 per clock, and an A72 roughly
# twice an A53/A55.
ARM_PARTS = {
    "0xc07": ("Cortex-A7", "low"),
    "0xc0f": ("Cortex-A15", "low"),
    "0xd03": ("Cortex-A53", "low"),
    "0xd04": ("Cortex-A35", "low"),
    "0xd05": ("Cortex-A55", "low"),
    "0xd46": ("Cortex-A510", "low"),
    "0xd80": ("Cortex-A520", "low"),
    "0xd07": ("Cortex-A57", "mid"),
    "0xd08": ("Cortex-A72", "mid"),
    "0xd09": ("Cortex-A73", "mid"),
    "0xd0a": ("Cortex-A75", "high"),
    "0xd0b": ("Cortex-A76", "high"),
    "0xd0c": ("Neoverse-N1", "high"),
    "0xd0d": ("Cortex-A77", "high"),
    "0xd40": ("Neoverse-V1", "high"),
    "0xd41": ("Cortex-A78", "high"),
    "0xd44": ("Cortex-X1", "high"),
    "0xd47": ("Cortex-A710", "high"),
    "0xd49": ("Neoverse-N2", "high"),
    "0xd4d": ("Cortex-A715", "high"),
    "0xd81": ("Cortex-A720", "high"),
}

# When cpuinfo carries no part numbers, the SoC in the compatible string is the
# next best evidence.
SOC_TIERS = {
    "bcm2712": "high", "bcm2711": "mid", "bcm2837": "low", "bcm2710": "low",
    "rk3588": "high", "rk3588s": "high", "rk3576": "high",
    "rk3399": "mid", "rk3566": "low", "rk3568": "low", "rk3328": "low",
    "sun50i-h6": "low", "sun50i-h616": "low", "sun50i-h618": "low",
    "sun50i-a64": "low", "sun50i-h5": "low",
    "meson-sm1": "low", "meson-g12a": "low", "meson-g12b": "mid",
}

VENDORS = {
    "raspberrypi": "Raspberry Pi",
    "xunlong": "Orange Pi",
    "orangepi": "Orange Pi",
    "sinovoip": "Banana Pi",
    "bananapi": "Banana Pi",
    "radxa": "Radxa",
    "hardkernel": "ODROID",
    "friendlyarm": "FriendlyElec",
    "friendlyelec": "FriendlyElec",
    "pine64": "Pine64",
    "khadas": "Khadas",
    "libretech": "Libre Computer",
}

# Raspberry Pi model number -> (board key, CPU class).
_PI_MODELS = {5: ("pi5", "high"), 4: ("pi4", "mid"), 3: ("pi3", "low"), 2: ("pi2", "low"), 1: ("pi1", "low")}


@dataclass(frozen=True)
class BoardInfo:
    key: str              # pi5, pi4, pi3, pizero2, orangepi-zero3, ..., unknown
    family: str           # "Raspberry Pi", "Orange Pi", "Banana Pi", ..., "Unknown"
    model: str            # the device-tree model string, as the vendor wrote it
    soc: str | None       # e.g. "allwinner,sun50i-h618"
    cpu: str              # e.g. "4x Cortex-A76 + 4x Cortex-A55"
    cpu_tier: str         # what the cores alone could manage
    mem_mb: int | None
    tier: str             # cpu_tier capped by RAM: what the settings are sized for
    arch: str             # aarch64, armv7l, x86_64, ...
    supported: bool
    reason: str

    @property
    def is_raspberry_pi(self) -> bool:
        return self.family == "Raspberry Pi"

    @property
    def mem_class(self) -> str | None:
        """Which deploy/systemd/<class>/ memory limits this board needs."""
        return memory_class(self.mem_mb)

    def as_dict(self) -> dict:
        return {**asdict(self), "is_raspberry_pi": self.is_raspberry_pi, "mem_class": self.mem_class}


def detect(
    *,
    model: str | None = None,
    compatible: list[str] | None = None,
    cpuinfo: str | None = None,
    mem_mb: int | None = None,
    machine: str | None = None,
    environ: dict[str, str] | None = None,
) -> BoardInfo:
    """Identify this board. Every input can be supplied, for testing.

    ``PW_BOARD`` overrides the board key and ``PW_TIER`` the tier, which is how
    a profile is exercised without the hardware.
    """
    env = os.environ if environ is None else environ
    model = read_model() if model is None else model.strip()
    compatible = read_compatible() if compatible is None else compatible
    cpuinfo = _read(CPUINFO) if cpuinfo is None else cpuinfo
    mem_mb = read_memory_mb() if mem_mb is None else mem_mb
    machine = platform.machine() if machine is None else machine

    family = _family(model, compatible)
    soc = compatible[-1] if len(compatible) > 1 else None
    cores = cpu_cores(cpuinfo)

    key, pi_tier = _raspberry_pi(model) if family == "Raspberry Pi" else (None, None)
    if key is None:
        key = _board_slug(model, compatible) if model else "unknown"

    override = env.get("PW_BOARD", "").strip().lower()
    if override:
        key = override
        pi_tier = _PI_KEY_TIERS.get(override, pi_tier)

    cpu_tier = pi_tier or _tier_from_cores(cores) or _tier_from_soc(compatible) or "high"
    tier = env.get("PW_TIER", "").strip().lower() or _cap_by_memory(cpu_tier, mem_mb)
    if tier not in TIERS:
        tier = cpu_tier

    supported, reason = _supported(model, machine, key)
    return BoardInfo(
        key=key,
        family=family,
        model=model,
        soc=soc,
        cpu=_describe_cores(cores) or machine,
        cpu_tier=cpu_tier,
        mem_mb=mem_mb,
        tier=tier,
        arch=machine,
        supported=supported,
        reason=reason,
    )


def memory_class(mem_mb: int | None) -> str | None:
    """The systemd memory-limit set for this much RAM, or None for the defaults.

    The base units cap the worker at 2 GB and the web server at 1 GB. On a
    board with less than 3 GB those limits sit above physical memory and never
    engage, so the kernel's OOM killer acts first and picks its own victim.
    """
    if mem_mb is None:
        return None
    if mem_mb < 768:
        return "mem-512m"
    if mem_mb < 1536:
        return "mem-1g"
    if mem_mb < 3072:
        return "mem-2g"
    return None


# -- readers ---------------------------------------------------------------


def read_model() -> str:
    return _read_dt_string(DEVICE_TREE / "model")


def read_compatible() -> list[str]:
    try:
        raw = (DEVICE_TREE / "compatible").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def read_memory_mb() -> int | None:
    for line in _read(MEMINFO).splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(int(line.split()[1]) / 1024)
            except (IndexError, ValueError):
                return None
    return None


def cpu_cores(cpuinfo: str) -> list[str]:
    """ARM part number of each core, in order (e.g. ``["0xd05", ..., "0xd0b"]``)."""
    parts = []
    for block in re.split(r"\n\s*\n", cpuinfo):
        implementer = re.search(r"^CPU implementer\s*:\s*(0x[0-9a-fA-F]+)", block, re.M)
        part = re.search(r"^CPU part\s*:\s*(0x[0-9a-fA-F]+)", block, re.M)
        if part and (implementer is None or implementer.group(1).lower() == "0x41"):
            parts.append(part.group(1).lower())
    return parts


# -- classification ----------------------------------------------------------


_PI_KEY_TIERS = {"pi5": "high", "pi4": "mid", "pi3": "low", "pizero2": "low", "pi2": "low", "pi1": "low", "pizero": "low"}


def _raspberry_pi(model: str) -> tuple[str | None, str | None]:
    lowered = model.lower()
    # Zero before the numeric match: "Raspberry Pi Zero 2 W" contains a digit
    # that would otherwise read as a model number.
    if "zero 2" in lowered:
        return "pizero2", "low"
    if "zero" in lowered:
        return "pizero", "low"
    match = re.search(r"raspberry pi\s+(?:compute module\s+)?(\d+)", lowered)
    if not match:
        # The original boards carry no number: "Raspberry Pi Model B Plus",
        # "Raspberry Pi Compute Module Rev 1.0".
        return ("pi1", "low") if "raspberry pi" in lowered else (None, None)
    number = int(match.group(1))
    # The keyboard models: Pi 400 is a Pi 4, Pi 500 a Pi 5.
    if number >= 100:
        number //= 100
    return _PI_MODELS.get(number, (f"pi{number}", "low"))


def _family(model: str, compatible: list[str]) -> str:
    lowered = model.lower()
    if "raspberry pi" in lowered:
        return "Raspberry Pi"
    if "orange pi" in lowered or "orangepi" in lowered:
        return "Orange Pi"
    if "banana pi" in lowered or "bananapi" in lowered or lowered.startswith("bpi"):
        return "Banana Pi"
    if compatible:
        vendor = compatible[0].split(",", 1)[0].lower()
        if vendor in VENDORS:
            return VENDORS[vendor]
        if model:
            return vendor.capitalize()
    return "Unknown" if not model else model.split()[0]


def _board_slug(model: str, compatible: list[str]) -> str:
    if compatible and "," in compatible[0]:
        name = compatible[0].split(",", 1)[1]
    else:
        name = model
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def _tier_from_cores(cores: list[str]) -> str | None:
    tiers = [ARM_PARTS[part][1] for part in cores if part in ARM_PARTS]
    if not tiers:
        return None
    # The fastest cores set the class: inference threads land on them.
    return max(tiers, key=TIERS.index)


def _tier_from_soc(compatible: list[str]) -> str | None:
    for entry in compatible:
        soc = entry.split(",", 1)[-1].lower()
        if soc in SOC_TIERS:
            return SOC_TIERS[soc]
    return None


def _cap_by_memory(tier: str, mem_mb: int | None) -> str:
    if mem_mb is None:
        return tier
    cap = "tiny" if mem_mb < 768 else "low" if mem_mb < 1536 else "high"
    return min(tier, cap, key=TIERS.index)


def _describe_cores(cores: list[str]) -> str:
    counts: dict[str, int] = {}
    for part in cores:
        name = ARM_PARTS.get(part, (f"part {part}", ""))[0]
        counts[name] = counts.get(name, 0) + 1
    # Fastest first, as vendors list big.LITTLE clusters.
    ordered = sorted(
        counts.items(),
        key=lambda item: -TIERS.index(next((t for n, t in ARM_PARTS.values() if n == item[0]), "low")),
    )
    return " + ".join(f"{count}x {name}" for name, count in ordered)


def _supported(model: str, machine: str, key: str) -> tuple[bool, str]:
    if machine in ("armv6l", "armv7l", "armhf", "arm"):
        return False, (
            f"{machine} is a 32-bit userland; ONNX Runtime publishes no 32-bit ARM "
            "builds. Install a 64-bit (arm64/aarch64) OS image"
        )
    if key in ("pi1", "pizero", "pi2"):
        return False, f"{model} has a 32-bit CPU; ONNX Runtime needs a 64-bit ARM board"
    if machine != "aarch64":
        return False, f"{machine} is not a 64-bit ARM single-board computer"
    if not model:
        return False, "no /proc/device-tree/model; this does not look like a single-board computer"
    return True, "supported"


# -- helpers ------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _read_dt_string(path: Path) -> str:
    """A device-tree string property, which is NUL-terminated."""
    try:
        return path.read_bytes().decode("utf-8", "replace").rstrip("\x00").strip()
    except OSError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Identify this board and its tier.")
    parser.add_argument("--shell", action="store_true", help="print KEY=value lines for eval in a shell")
    args = parser.parse_args()

    info = detect()
    if args.shell:
        fields = {
            "BOARD_KEY": info.key,
            "BOARD_FAMILY": info.family,
            "BOARD_MODEL": info.model,
            "BOARD_TIER": info.tier,
            "BOARD_CPU": info.cpu,
            "BOARD_MEM_CLASS": info.mem_class or "",
            "BOARD_IS_RPI": "1" if info.is_raspberry_pi else "0",
            "BOARD_SUPPORTED": "1" if info.supported else "0",
            "BOARD_REASON": info.reason,
        }
        for name, value in fields.items():
            print(f"{name}={shlex.quote(str(value))}")
        return 0

    print(f"board    : {info.model or 'unknown'} ({info.family})")
    print(f"key      : {info.key}")
    print(f"cpu      : {info.cpu} -> {info.cpu_tier}")
    print(f"memory   : {info.mem_mb} MB -> limits {info.mem_class or 'default'}")
    print(f"tier     : {info.tier}")
    print(f"supported: {'yes' if info.supported else 'no'} ({info.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
