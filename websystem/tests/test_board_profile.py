"""Board detection and per-board tuning profiles.

`config/default.toml` is sized for a Raspberry Pi 5. A Pi 3 has an eighth of
the RAM and a CPU several times slower, so shipping the Pi 5 settings to one
means the service memory ceilings sit *above* physical memory -- they never
engage, and the kernel's OOM killer fires instead, choosing its victim by
heuristic rather than by our intent.

These tests cover the detection, the profile layering, and the arithmetic that
keeps the ceilings under RAM.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from pepperwatch.config import (
    KNOWN_BOARDS,
    PROFILE_DIR,
    detect_board,
    load,
    profile_path,
)

ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = ROOT / "deploy" / "systemd"

# Physical RAM actually visible to the OS on a 1 GB Pi 3, after the GPU split.
PI3_VISIBLE_MB = 940
# Headroom the OS, NetworkManager, mosquitto and sshd need.
OS_RESERVE_MB = 150


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("Raspberry Pi 5 Model B Rev 1.0", "pi5"),
        ("Raspberry Pi 4 Model B Rev 1.4", "pi4"),
        ("Raspberry Pi 3 Model B Plus Rev 1.3", "pi3"),
        ("Raspberry Pi 3 Model B Rev 1.2", "pi3"),
        ("Raspberry Pi 2 Model B Rev 1.1", "pi2"),
        ("Raspberry Pi Compute Module 4 Rev 1.1", "pi4"),
        ("Apple MacBook Pro", "unknown"),
        ("", "unknown"),
    ],
)
def test_board_detection(monkeypatch, model, expected) -> None:
    monkeypatch.delenv("PW_BOARD", raising=False)
    assert detect_board(model) == expected


def test_zero_2_is_not_read_as_a_pi_2(monkeypatch) -> None:
    """'Raspberry Pi Zero 2 W' contains a digit that must not be a model number."""
    monkeypatch.delenv("PW_BOARD", raising=False)
    assert detect_board("Raspberry Pi Zero 2 W Rev 1.0") == "pizero2"
    assert detect_board("Raspberry Pi Zero W Rev 1.1") == "pizero"


def test_env_overrides_detection(monkeypatch) -> None:
    """How the tests, and a developer, exercise a profile without the hardware."""
    monkeypatch.setenv("PW_BOARD", "pi3")
    assert detect_board("Raspberry Pi 5 Model B Rev 1.0") == "pi3"


# -- profile layering ------------------------------------------------------


def test_a_pi3_profile_ships() -> None:
    assert profile_path("pi3") is not None, "config/profiles/pi3.toml is missing"


def test_profiles_are_named_for_known_boards() -> None:
    for candidate in PROFILE_DIR.glob("*.toml"):
        assert candidate.stem in KNOWN_BOARDS, f"unexpected profile {candidate.name}"


def test_no_profile_applies_off_device(monkeypatch) -> None:
    monkeypatch.setenv("PW_BOARD", "unknown")
    cfg = load()
    assert cfg.platform.get("profile") is None
    assert cfg.inference.input_size == 640  # the Pi 5 default


def test_pi3_profile_lowers_the_expensive_settings(monkeypatch) -> None:
    monkeypatch.setenv("PW_BOARD", "pi3")
    pi3 = load()
    monkeypatch.setenv("PW_BOARD", "unknown")
    default = load()

    assert pi3.platform.get("profile") == "pi3"
    assert pi3.inference.input_size < default.inference.input_size
    assert pi3.inference.intra_op_threads < default.inference.intra_op_threads
    assert pi3.camera.preview_fps < default.camera.preview_fps
    assert pi3.frames.slot_bytes < default.frames.slot_bytes
    assert pi3.web.max_stream_clients < default.web.max_stream_clients


def test_pi3_profile_does_not_lower_the_fps_requirement(monkeypatch) -> None:
    """§3.2's 17 FPS is the requirement, not a tuning knob.

    Lowering it per board would hide the shortfall the dashboard should be
    reporting honestly. The Pi 3 showing "below the 17 fps target" is correct.
    """
    monkeypatch.setenv("PW_BOARD", "pi3")
    assert load().inference.target_fps == 17


def test_profile_does_not_touch_deployment_identity(monkeypatch) -> None:
    """A profile tunes for hardware; it must not rewrite what the site is."""
    monkeypatch.setenv("PW_BOARD", "pi3")
    pi3 = load()
    monkeypatch.setenv("PW_BOARD", "unknown")
    default = load()

    assert pi3.site.name == default.site.name
    assert pi3.inference.class_names == default.inference.class_names
    assert pi3.inference.conf_threshold == default.inference.conf_threshold
    assert pi3.severity.as_dict() == default.severity.as_dict()


def test_environment_still_beats_the_profile(monkeypatch) -> None:
    """Layering order: default -> profile -> local.toml -> environment."""
    monkeypatch.setenv("PW_BOARD", "pi3")
    monkeypatch.setenv("PW_INFERENCE_INPUT_SIZE", "416")
    assert load().inference.input_size == 416


# -- the memory ceilings ---------------------------------------------------


def memory_max_mb(path: Path) -> float | None:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    raw = parser["Service"].get("MemoryMax")
    if not raw:
        return None
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if raw[-1].upper() in suffixes:
        return float(raw[:-1]) * suffixes[raw[-1].upper()] / 1e6
    return float(raw) / 1e6


def test_pi3_dropins_exist_for_both_services() -> None:
    for unit in ("pepperwatch-worker.service", "pepperwatch-web.service"):
        dropin = SYSTEMD / "pi3" / f"{unit}.d" / "10-pi3.conf"
        assert dropin.exists(), f"missing {dropin.relative_to(ROOT)}"


def test_base_unit_ceilings_would_not_fit_a_pi3() -> None:
    """Documents the defect these drop-ins exist to fix."""
    total = sum(
        memory_max_mb(SYSTEMD / unit) or 0
        for unit in ("pepperwatch-worker.service", "pepperwatch-web.service")
    )
    assert total > PI3_VISIBLE_MB, (
        "the base units now fit in a Pi 3, so the drop-ins may be unnecessary"
    )


def test_pi3_ceilings_fit_in_physical_ram() -> None:
    """The actual fix: caps below RAM, so systemd reclaims before the OOM killer."""
    total = sum(
        memory_max_mb(SYSTEMD / "pi3" / f"{unit}.d" / "10-pi3.conf") or 0
        for unit in ("pepperwatch-worker.service", "pepperwatch-web.service")
    )
    assert total > 0, "the drop-ins set no MemoryMax"
    assert total + OS_RESERVE_MB < PI3_VISIBLE_MB, (
        f"{total:.0f}M of limits plus {OS_RESERVE_MB}M for the OS "
        f"exceeds the ~{PI3_VISIBLE_MB}M a Pi 3 actually has"
    )


def test_pi3_reclaims_before_it_kills() -> None:
    """MemoryHigh throttles under pressure; MemoryMax is the hard stop."""
    for unit in ("pepperwatch-worker.service", "pepperwatch-web.service"):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read(SYSTEMD / "pi3" / f"{unit}.d" / "10-pi3.conf")
        service = parser["Service"]

        assert "MemoryHigh" in service, f"{unit} drop-in sets no MemoryHigh"
        high = float(service["MemoryHigh"].rstrip("M"))
        hard = float(service["MemoryMax"].rstrip("M"))
        assert high < hard, f"{unit}: MemoryHigh {high} is not below MemoryMax {hard}"


def test_pi3_keeps_the_dashboard_above_the_worker() -> None:
    """An unresponsive dashboard looks like a crashed system.

    The worker will saturate all four A53 cores given the chance, so the web
    process needs the heavier CPU weight.
    """
    def weight(unit: str) -> int:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read(SYSTEMD / "pi3" / f"{unit}.d" / "10-pi3.conf")
        return int(parser["Service"]["CPUWeight"])

    assert weight("pepperwatch-web.service") > weight("pepperwatch-worker.service")


def test_pi3_web_dropin_replaces_rather_than_appends_execstart() -> None:
    """systemd appends ExecStart= unless it is first cleared with an empty one.

    Without the blank line the unit would try to run gunicorn twice.
    """
    body = (SYSTEMD / "pi3" / "pepperwatch-web.service.d" / "10-pi3.conf").read_text()
    assert "\nExecStart=\n" in body, "ExecStart is not cleared before being redefined"


# -- installer -------------------------------------------------------------


def test_installer_refuses_a_32_bit_userland() -> None:
    """ONNX Runtime publishes no 32-bit ARM wheels, so inference cannot run."""
    body = (ROOT / "deploy" / "install.sh").read_text()
    assert "aarch64" in body
    assert "64-bit" in body


def test_installer_applies_board_dropins() -> None:
    body = (ROOT / "deploy" / "install.sh").read_text()
    assert "DROPIN_SRC" in body
    assert "service.d" in body


def test_installer_warns_when_a_small_board_has_no_dropins() -> None:
    body = (ROOT / "deploy" / "install.sh").read_text()
    assert "OOM killer" in body


def test_installer_enables_zram_on_low_memory_boards() -> None:
    """Compressed swap in RAM, so installing onnxruntime on 1 GB does not OOM.

    zram rather than a swap file because the SD card is the likeliest component
    to fail and swap writes would accelerate that.
    """
    body = (ROOT / "deploy" / "install.sh").read_text()
    assert "zram" in body
    assert "MEM_MB" in body
