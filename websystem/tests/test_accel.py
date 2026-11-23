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
                        hat_root=tmp_path / "hat", identify=False, import_runtime=False, settle_s=0)
    assert probe.arch == "hailo10h" and probe.arch_confirmed and probe.name == "Hailo-10H"


def test_just_after_boot_the_probe_waits_for_the_driver(tmp_path: Path) -> None:
    """The driver can still be loading when the worker starts; wait rather than
    run the whole first session on the CPU."""
    import threading

    _pci(tmp_path / "pci", "0001:01:00.0", vendor="0x1e60", device="0x2864")
    (tmp_path / "dev").mkdir()
    threading.Timer(0.3, (tmp_path / "dev" / "hailo0").write_text, args=("",)).start()
    probe = probe_hailo(pci_root=tmp_path / "pci", dev_root=tmp_path / "dev", hat_root=tmp_path / "hat",
                        identify=False, import_runtime=False, settle_s=3)
    assert probe.device_path and probe.device_path.endswith("hailo0")


def test_long_after_boot_a_missing_driver_is_reported_at_once(tmp_path: Path, monkeypatch) -> None:
    import time

    import pepperwatch.accel as accel

    monkeypatch.setattr(accel, "_uptime_s", lambda: 3600.0)
    _pci(tmp_path / "pci", "0001:01:00.0", vendor="0x1e60", device="0x2864")
    began = time.monotonic()
    probe = probe_hailo(pci_root=tmp_path / "pci", dev_root=tmp_path / "dev", hat_root=tmp_path / "hat",
                        identify=False, import_runtime=False)
    assert probe.device_path is None and time.monotonic() - began < 1.0


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


def test_a_missing_model_names_the_folder_it_looked_in(cfg) -> None:
    """On the Pi that is /opt/pepperwatch/models: a HEF left only in the checkout is not there."""
    reason = choose_accelerator(cfg, READY).reason
    assert f"pepperwatch.hef in {cfg.hef_path.parent})" in reason


def test_mode_cpu_only_reads_sysfs_to_record_a_fitted_hat(cfg, monkeypatch) -> None:
    """No hailortcli, no runtime import, no waiting: only whether a HAT is there,
    since a fitted one idles and draws power in a CPU baseline."""
    import pepperwatch.accel as accel

    calls = []
    fitted = HailoProbe(present=True, arch="hailo8")
    monkeypatch.setattr(accel, "probe_hailo", lambda **kwargs: calls.append(kwargs) or fitted)
    cfg.accelerator._data["mode"] = "cpu"  # noqa: SLF001
    choice = choose_accelerator(cfg)
    assert (choice.kind, choice.deliberate, choice.probe) == ("cpu", True, fitted)
    assert calls == [{"identify": False, "import_runtime": False, "settle_s": 0}]
    assert choice.as_dict()["deliberate"] is True  # the dashboard's banner stays quiet


def test_mode_hailo_still_falls_back_rather_than_refusing_to_run(cfg) -> None:
    cfg.accelerator._data["mode"] = "hailo"  # noqa: SLF001
    choice = choose_accelerator(cfg, HailoProbe())
    assert choice.kind == "cpu"
    assert choice.reason.startswith("requested the AI HAT+")


def test_profiles_adjust_the_session_settings(cfg) -> None:
    hailo, cpu = cfg.with_profile("hailo"), cfg.with_profile("cpu")
    # Same capture rate, so CPU and HAT sessions differ only in what runs the model.
    assert hailo.camera.preview_fps == cpu.camera.preview_fps
    assert cpu.inference.intra_op_threads == 3
    assert hailo.get("session_profile") == "hailo"
    # The base config is untouched.
    assert cfg.get("session_profile") is None
