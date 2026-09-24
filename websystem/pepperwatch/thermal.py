"""Thermal governor.

Three ONNX threads hold a Pi 5 near 100% CPU. Inside a sealed weatherproof
enclosure under Mindanao sun, internal ambient can pass 50 °C, and the SoC soft-
throttles at 80 °C. Left alone, the failure mode is the worst possible one: FPS
sags partway through an eight-hour field cycle, or mid-demo, for reasons the
dashboard cannot explain.

So instead of waiting to be throttled, back off first. Above a warning
temperature the governor stretches the interval between inferences, which drops
average power and lets the heatsink catch up; the live preview keeps streaming at
full rate throughout, because that costs almost nothing. Every transition is
logged and surfaced in the System Health Monitor, which turns that panel from
decoration into the thing that explains the device's own behaviour.

Deliberately hysteretic: it takes a higher temperature to escalate than to
recover, so the governor cannot oscillate between levels on sensor noise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Level 0 is unrestricted. Each further level multiplies the inference interval.
_LEVEL_MULTIPLIERS = (1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True, slots=True)
class ThermalState:
    level: int
    multiplier: float
    cpu_temp_c: float | None
    reason: str

    @property
    def backing_off(self) -> bool:
        return self.level > 0


class ThermalGovernor:
    """Maps CPU temperature to an inference-rate multiplier."""

    def __init__(
        self,
        *,
        warn_c: float = 75.0,
        step_c: float = 3.0,
        recover_margin_c: float = 4.0,
        max_level: int = 3,
    ) -> None:
        self._warn_c = float(warn_c)
        self._step_c = float(step_c)
        # Recovery needs the temperature to fall this far below the threshold
        # that escalated, which is what prevents level flapping.
        self._recover_margin_c = float(recover_margin_c)
        self._max_level = min(int(max_level), len(_LEVEL_MULTIPLIERS) - 1)
        self._level = 0

    def update(self, cpu_temp_c: float | None) -> ThermalState:
        """Fold in a new temperature reading and return the resulting state."""
        if cpu_temp_c is None:
            # No sensor is not a licence to run flat out on unknown hardware,
            # but neither is it evidence of heat. Hold the current level.
            return self._state(cpu_temp_c, "no temperature sensor")

        previous = self._level

        escalate_at = self._warn_c + self._level * self._step_c
        recover_below = self._warn_c + (self._level - 1) * self._step_c - self._recover_margin_c

        if cpu_temp_c >= escalate_at and self._level < self._max_level:
            self._level += 1
        elif self._level > 0 and cpu_temp_c < recover_below:
            self._level -= 1

        if self._level != previous:
            log.warning(
                "thermal backoff %s: %.1f C -> level %d (inference interval x%.0f)",
                "up" if self._level > previous else "down",
                cpu_temp_c,
                self._level,
                _LEVEL_MULTIPLIERS[self._level],
            )

        reason = (
            f"{cpu_temp_c:.1f} C at or above {escalate_at:.1f} C"
            if self._level > 0
            else f"{cpu_temp_c:.1f} C below {self._warn_c:.1f} C"
        )
        return self._state(cpu_temp_c, reason)

    def _state(self, cpu_temp_c: float | None, reason: str) -> ThermalState:
        return ThermalState(
            level=self._level,
            multiplier=_LEVEL_MULTIPLIERS[self._level],
            cpu_temp_c=cpu_temp_c,
            reason=reason,
        )

    @property
    def level(self) -> int:
        return self._level
