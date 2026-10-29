"""AI HAT+ detection and the per-session choice between it and the CPU."""

from __future__ import annotations

from pathlib import Path

import pytest

from pepperwatch.accel import (
    HailoProbe,
    choose_accelerator,
    hef_candidates,
    parse_identify,
    probe_hailo,
)
from pepperwatch.config import load


def _pci(root: Path, address: str, *, vendor: str, device: str, speed: str = "8.0 GT/s PCIe") -> None:
    node = root / address
    node.mkdir(parents=True)
    (node / "vendor").write_text(vendor + "\n")
    (node / "device").write_text(device + "\n")
    (node / "current_link_speed").write_text(speed + "\n")
    (node / "current_link_width").write_text("1\n")


@pytest.fixture
def cfg(tmp_path: Path):
    return load(
        {
            "accelerator": {"hef_path": str(tmp_path / "models" / "pepperwatch.hef"), "mode": "auto"},
            "db": {"path": str(tmp_path / "t.db")},
        },
        use_env=False,
    )


def test_no_hailo_on_the_bus(tmp_path: Path) -> None:
    _pci(tmp_path / "pci", "0000:01:00.0", vendor="0x1de4", device="0x0001")  # the RP1
    probe = probe_hailo(pci_root=tmp_path / "pci", dev_root=tmp_path / "dev",
                        hat_root=tmp_path / "hat", identify=False, import_runtime=False)
    assert probe.present is False


def test_hailo8_family_found_with_link_speed(tmp_path: Path) -> None:
    _pci(tmp_path / "pci", "0001:01:00.0", vendor="0x1e60", device="0x2864")
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "hailo0").write_text("")
    probe = probe_hailo(pci_root=tmp_path / "pci", dev_root=tmp_path / "dev",
                        hat_root=tmp_path / "hat", identify=False, import_runtime=False)
    assert probe.present and probe.arch == "hailo8"
    # 8 and 8L share a device ID; without hailortcli the chip is not confirmed.
    assert probe.arch_confirmed is False and probe.name == "Hailo-8/8L"
    assert probe.pcie_link == "8.0 GT/s PCIe x1"
    assert probe.device_path.endswith("hailo0") and probe.device_accessible


def test_hailo10h_is_identified_by_its_device_id(tmp_path: Path) -> None:
    _pci(tmp_path / "pci", "0001:01:00.0", vendor="0x1e60", device="0x45c4")
    probe = probe_hailo(pci_root=tmp_path / "pci", dev_root=tmp_path / "dev",
                        hat_root=tmp_path / "hat", identify=False, import_runtime=False)
    assert probe.arch == "hailo10h" and probe.arch_confirmed and probe.name == "Hailo-10H"


@pytest.mark.parametrize(
    ("text", "arch"),
    [
        ("Board Name: Hailo-8\nDevice Architecture: HAILO8L\n", "hailo8l"),
        ("Device Architecture: HAILO8\n", "hailo8"),
        ("Device Architecture: HAILO10H\n", "hailo10h"),
        ("no such device\n", None),
    ],
)
def test_identify_output_is_parsed(text: str, arch: str | None) -> None:
    assert parse_identify(text) == arch


def test_unconfirmed_chip_tries_the_8l_build_first(cfg) -> None:
    """An 8L HEF runs on either chip; an 8 HEF only on the Hailo-8."""
    names = [p.name for p in hef_candidates(cfg, "hailo8", confirmed=False)]
    assert names == ["pepperwatch_hailo8l.hef", "pepperwatch_hailo8.hef", "pepperwatch.hef"]
    names = [p.name for p in hef_candidates(cfg, "hailo8l")]
    assert names == ["pepperwatch_hailo8l.hef", "pepperwatch.hef"]


READY = HailoProbe(
    present=True, arch="hailo8l", arch_confirmed=True, pcie_link="8.0 GT/s PCIe x1",
    device_path="/dev/hailo0", device_accessible=True, runtime_version="4.20.0",
)


def test_a_usable_hat_with_a_hef_is_chosen(cfg, tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "pepperwatch_hailo8l.hef").write_bytes(b"hef")
    choice = choose_accelerator(cfg, READY)
    assert choice.kind == "hailo" and choice.profile == "hailo"
    assert choice.name == "Hailo-8L"
    assert choice.hef_path.name == "pepperwatch_hailo8l.hef"


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (HailoProbe(), "no AI HAT+"),
        (HailoProbe(present=True, arch="hailo8l", arch_confirmed=True), "driver is not loaded"),
        (HailoProbe(present=True, arch="hailo8l", arch_confirmed=True, device_path="/dev/hailo0"),
         "permission"),
        (HailoProbe(present=True, arch="hailo10h", arch_confirmed=True, device_path="/dev/hailo0",
                    device_accessible=True, runtime_error="ImportError: no module"), "hailo-h10-all"),
        (READY, "no compiled model"),
    ],
)
def test_each_reason_to_fall_back_is_reported(cfg, probe: HailoProbe, reason: str) -> None:
    choice = choose_accelerator(cfg, probe)
    assert choice.kind == "cpu" and choice.profile == "cpu"
    assert reason in choice.reason


def test_mode_cpu_never_probes(cfg, monkeypatch) -> None:
    import pepperwatch.accel as accel

    monkeypatch.setattr(accel, "probe_hailo", lambda **_: pytest.fail("probed"))
    cfg.accelerator._data["mode"] = "cpu"  # noqa: SLF001
    assert choose_accelerator(cfg).kind == "cpu"


def test_mode_hailo_still_falls_back_rather_than_refusing_to_run(cfg) -> None:
    cfg.accelerator._data["mode"] = "hailo"  # noqa: SLF001
    choice = choose_accelerator(cfg, HailoProbe())
    assert choice.kind == "cpu"
    assert choice.reason.startswith("requested the AI HAT+")


def test_profiles_adjust_the_session_settings(cfg) -> None:
    hailo, cpu = cfg.with_profile("hailo"), cfg.with_profile("cpu")
    assert hailo.camera.preview_fps > cpu.camera.preview_fps
    assert cpu.inference.intra_op_threads == 3
    assert hailo.get("session_profile") == "hailo"
    # The base config is untouched.
    assert cfg.get("session_profile") is None
