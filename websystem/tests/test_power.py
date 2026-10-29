"""Power, throttle and telemetry readings from the Pi 5's own tools."""

from __future__ import annotations

import pytest

from pepperwatch.hal.base import Telemetry
from pepperwatch.hal.power import parse_pmic_adc, parse_throttled

# Trimmed from a real `vcgencmd pmic_read_adc` on a Pi 5.
PMIC = """\
     3V7_WL_SW_A current(0)=0.10000000A
       3V3_SYS_A current(1)=0.05000000A
     VDD_CORE_A current(7)=2.00000000A
     0V8_AON_A current(16)=0.00384615A
     3V7_WL_SW_V volt(8)=3.70000000V
       3V3_SYS_V volt(9)=3.30000000V
     VDD_CORE_V volt(15)=0.80000000V
     0V8_AON_V volt(23)=0.80000000V
        EXT5V_V volt(24)=5.10000000V
         BATT_V volt(25)=0.00000000V
"""


def test_pmic_rails_are_summed_as_volts_times_amps() -> None:
    reading = parse_pmic_adc(PMIC)
    expected = 3.7 * 0.1 + 3.3 * 0.05 + 0.8 * 2.0 + 0.8 * 0.00384615
    assert reading.rails_w == pytest.approx(expected, abs=1e-3)
    assert reading.input_voltage_v == pytest.approx(5.1)
    # Rails missing either half (EXT5V has no current, BATT none either) are left out.
    assert set(reading.rails) == {"3V7_WL_SW", "3V3_SYS", "VDD_CORE", "0V8_AON"}


def test_pmic_output_that_is_not_there_gives_no_reading() -> None:
    reading = parse_pmic_adc("error=1 error_msg=\"Command not registered\"\n")
    assert reading.rails_w is None and reading.input_voltage_v is None


@pytest.mark.parametrize(("text", "flags"), [("throttled=0x0\n", 0), ("throttled=0x50005\n", 0x50005), ("", None)])
def test_throttled_bits_are_parsed(text: str, flags: int | None) -> None:
    assert parse_throttled(text) == flags


def test_ina219_is_preferred_over_the_pmic_estimate() -> None:
    both = Telemetry(ts=0.0, power_w=6.2, pmic_power_w=3.9)
    pmic_only = Telemetry(ts=0.0, pmic_power_w=3.9)
    neither = Telemetry(ts=0.0)
    assert (both.power_source, both.best_power_w) == ("ina219", 6.2)
    assert (pmic_only.power_source, pmic_only.best_power_w) == ("pmic", 3.9)
    assert (neither.power_source, neither.best_power_w) == (None, None)
    assert both.as_dict()["power_source"] == "ina219"
