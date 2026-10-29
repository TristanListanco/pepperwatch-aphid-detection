"""Power, clock, fan and throttle readings from the Raspberry Pi 5 itself.

The PMIC on a Pi 5 measures the voltage and current of each supply rail, and
``vcgencmd pmic_read_adc`` reports them. Summing volts times amps over the
rails gives the board's own consumption with no extra hardware, which is what
makes a power figure available for every session even without an INA219.

What it does not see: USB peripherals (a webcam), the AI HAT+, the fan, and
the losses in getting 5 V down to those rails. So it reads below what the
supply delivers, and the CSV keeps it in its own column, apart from INA219
readings, rather than letting one stand in for the other unlabelled.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

CPU_FREQ = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
FAN_GLOB = "/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input"

_PMIC_LINE = re.compile(r"^\s*(?P<rail>\S+?)_(?P<unit>[AV])\s+(?:current|volt)\(\d+\)=(?P<value>[-0-9.]+)[AV]\s*$")


@dataclass(frozen=True, slots=True)
class PmicReading:
    rails_w: float | None        # sum of V*A over rails reporting both
    input_voltage_v: float | None  # EXT5V, the supply as it reaches the board
    rails: dict[str, tuple[float, float]]  # rail -> (volts, amps)


def parse_pmic_adc(text: str) -> PmicReading:
    """Parse ``vcgencmd pmic_read_adc`` output."""
    volts: dict[str, float] = {}
    amps: dict[str, float] = {}
    for line in text.splitlines():
        match = _PMIC_LINE.match(line)
        if not match:
            continue
        value = float(match.group("value"))
        (amps if match.group("unit") == "A" else volts)[match.group("rail")] = value

    rails = {name: (volts[name], amps[name]) for name in amps if name in volts}
    total = sum(v * a for v, a in rails.values()) if rails else None
    return PmicReading(
        rails_w=round(total, 3) if total is not None else None,
        input_voltage_v=round(volts["EXT5V"], 3) if "EXT5V" in volts else None,
        rails=rails,
    )


def parse_throttled(text: str) -> int | None:
    """The bit field from ``vcgencmd get_throttled`` (``throttled=0x50000``)."""
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    return int(match.group(1), 16) if match else None


class Vcgencmd:
    """Runs vcgencmd, and stops trying after the first sign it is missing."""

    def __init__(self) -> None:
        self.available = True

    def run(self, *args: str) -> str | None:
        if not self.available:
            return None
        try:
            result = subprocess.run(
                ["vcgencmd", *args], capture_output=True, text=True, timeout=2
            )
        except (OSError, subprocess.SubprocessError):
            self.available = False
            return None
        return result.stdout if result.returncode == 0 else None


def read_cpu_freq_mhz(path: Path = CPU_FREQ) -> float | None:
    try:
        return round(int(path.read_text().strip()) / 1000.0, 0)
    except (OSError, ValueError):
        return None


def read_fan_rpm(pattern: str = FAN_GLOB) -> float | None:
    """The Active Cooler's tachometer, if one is fitted."""
    for candidate in sorted(Path("/").glob(pattern.lstrip("/"))):
        try:
            return float(candidate.read_text().strip())
        except (OSError, ValueError):
            continue
    return None
