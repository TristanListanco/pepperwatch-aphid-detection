"""Board detection, tiers, and per-tier tuning.

`config/default.toml` is sized for a Raspberry Pi 5. Older Pis and other
single-board computers (Orange Pi, Banana Pi, ...) have slower cores and often
a gigabyte or less of RAM, so shipping the Pi 5 settings there leaves the
detector crawling and the service memory ceilings *above* physical memory,
where they never engage and the kernel's OOM killer acts instead.

These tests cover detection from real device-tree and cpuinfo strings, the
profile layering, and the arithmetic that keeps the ceilings under RAM.
"""

from __future__ import annotations

import ast
import configparser
import subprocess
import sys
from pathlib import Path

import pytest

from pepperwatch import config as config_module
from pepperwatch.board import BoardInfo, cpu_cores, detect, memory_class
from pepperwatch.config import PROFILE_DIR, TIER_PROFILES, detect_board, load, profile_path

ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = ROOT / "deploy" / "systemd"


def cpuinfo(*parts: str) -> str:
    """A /proc/cpuinfo in the arm64 kernel's format, one block per core."""
    return "\n\n".join(
        f"processor\t: {index}\nBogoMIPS\t: 48.00\nCPU implementer\t: 0x41\n"
        f"CPU architecture: 8\nCPU variant\t: 0x0\nCPU part\t: {part}\nCPU revision\t: 1"
        for index, part in enumerate(parts)
    )


def board(model: str, compatible: list[str] | None = None, cores: str = "", mem_mb: int | None = 4096,
          machine: str = "aarch64", environ: dict | None = None) -> BoardInfo:
    return detect(model=model, compatible=compatible or [], cpuinfo=cores, mem_mb=mem_mb,
                  machine=machine, environ=environ or {})


# -- Raspberry Pi -------------------------------------------------------------


@pytest.mark.parametrize(
    "model,mem_mb,key,tier",
    [
        ("Raspberry Pi 5 Model B Rev 1.0", 8192, "pi5", "high"),
        ("Raspberry Pi 5 Model B Rev 1.1", 2048, "pi5", "high"),
        ("Raspberry Pi 500 Rev 1.0", 8192, "pi5", "high"),
        ("Raspberry Pi 4 Model B Rev 1.4", 4096, "pi4", "mid"),
        ("Raspberry Pi 4 Model B Rev 1.5", 1024, "pi4", "low"),   # 1 GB caps the tier
        ("Raspberry Pi 400 Rev 1.0", 4096, "pi4", "mid"),
        ("Raspberry Pi Compute Module 4 Rev 1.1", 4096, "pi4", "mid"),
        ("Raspberry Pi 3 Model B Plus Rev 1.3", 1024, "pi3", "low"),
        ("Raspberry Pi 3 Model B Rev 1.2", 1024, "pi3", "low"),
        ("Raspberry Pi Zero 2 W Rev 1.0", 512, "pizero2", "tiny"),
    ],
)
def test_raspberry_pi_detection(model, mem_mb, key, tier) -> None:
    info = board(model, mem_mb=mem_mb)
    assert (info.key, info.family, info.tier) == (key, "Raspberry Pi", tier)
    assert info.supported and info.is_raspberry_pi


def test_zero_2_is_not_read_as_a_pi_2() -> None:
    """'Raspberry Pi Zero 2 W' contains a digit that must not be a model number."""
    assert board("Raspberry Pi Zero 2 W Rev 1.0", mem_mb=512).key == "pizero2"
    assert board("Raspberry Pi Zero W Rev 1.1", mem_mb=512).key == "pizero"


@pytest.mark.parametrize("model", ["Raspberry Pi 2 Model B Rev 1.1", "Raspberry Pi Zero W Rev 1.1",
                                   "Raspberry Pi Model B Plus Rev 1.2"])
def test_32_bit_pis_are_refused(model) -> None:
    info = board(model, mem_mb=512)
    assert not info.supported
    assert "64-bit" in info.reason


# -- other boards ---------------------------------------------------------------


def test_orange_pi_zero_3() -> None:
    info = board("OrangePi Zero3", ["xunlong,orangepi-zero3", "allwinner,sun50i-h618"],
                 cpuinfo("0xd03", "0xd03", "0xd03", "0xd03"), mem_mb=1024)
    assert info.family == "Orange Pi"
    assert info.key == "orangepi-zero3"
    assert info.soc == "allwinner,sun50i-h618"
    assert info.cpu == "4x Cortex-A53"
    assert (info.cpu_tier, info.tier, info.mem_class) == ("low", "low", "mem-1g")
    assert info.supported and not info.is_raspberry_pi


def test_more_ram_does_not_promote_slow_cores() -> None:
    info = board("OrangePi Zero3", ["xunlong,orangepi-zero3", "allwinner,sun50i-h618"],
                 cpuinfo(*["0xd03"] * 4), mem_mb=4096)
    assert info.tier == "low" and info.mem_class is None


def test_orange_pi_5_big_little_is_classed_by_its_big_cores() -> None:
    """RK3588S: 4x A55 + 4x A76. Inference threads land on the A76s."""
    info = board("Orange Pi 5", ["rockchip,rk3588s-orangepi-5", "rockchip,rk3588s"],
                 cpuinfo(*["0xd05"] * 4, *["0xd0b"] * 4), mem_mb=8192)
    assert info.family == "Orange Pi"
    assert info.cpu == "4x Cortex-A76 + 4x Cortex-A55"
    assert info.tier == "high"


def test_banana_pi_boards() -> None:
    m5 = board("Banana Pi BPI-M5", ["bananapi,bpi-m5", "amlogic,meson-sm1"],
               cpuinfo(*["0xd05"] * 4), mem_mb=4096)
    assert (m5.family, m5.key, m5.tier) == ("Banana Pi", "bpi-m5", "low")

    berry = board("BananaPi BPI-M4-Berry", ["sinovoip,bpi-m4-berry", "allwinner,sun50i-h618"],
                  cpuinfo(*["0xd03"] * 4), mem_mb=2048)
    assert berry.family == "Banana Pi"
    assert (berry.tier, berry.mem_class) == ("low", "mem-2g")


def test_soc_is_the_fallback_when_cpuinfo_names_no_cores() -> None:
    info = board("Radxa ROCK Pi 4", ["radxa,rockpi4", "rockchip,rk3399"], cores="", mem_mb=4096)
    assert (info.family, info.tier) == ("Radxa", "mid")


def test_unknown_cores_and_soc_get_the_default_settings() -> None:
    assert board("Mystery Board", ["acme,mystery", "acme,soc9000"], mem_mb=4096).tier == "high"


def test_32_bit_userland_is_refused_on_any_board() -> None:
    info = board("OrangePi Zero3", ["xunlong,orangepi-zero3"], machine="armv7l", mem_mb=1024)
    assert not info.supported
    assert "32-bit" in info.reason


def test_a_desktop_is_not_mistaken_for_a_board() -> None:
    info = board("", [], machine="x86_64", mem_mb=16384)
    assert info.key == "unknown"
    assert not info.supported


def test_env_overrides_detection() -> None:
    """How the tests, and a developer, exercise a profile without the hardware."""
    info = board("Raspberry Pi 5 Model B Rev 1.0", environ={"PW_BOARD": "pi3"})
    assert (info.key, info.tier) == ("pi3", "low")
    assert board("Raspberry Pi 5 Model B Rev 1.0", environ={"PW_TIER": "mid"}).tier == "mid"


def test_cpuinfo_parsing_ignores_non_arm_implementers() -> None:
    text = cpuinfo("0xd0b", "0xd05") + "\n\nprocessor\t: 2\nCPU implementer\t: 0x51\nCPU part\t: 0x801"
    assert cpu_cores(text) == ["0xd0b", "0xd05"]


@pytest.mark.parametrize(
    "mem_mb,expected",
    [(430, "mem-512m"), (767, "mem-512m"), (940, "mem-1g"), (1535, "mem-1g"),
     (1900, "mem-2g"), (3071, "mem-2g"), (3800, None), (16000, None), (None, None)],
)
def test_memory_class_boundaries(mem_mb, expected) -> None:
    assert memory_class(mem_mb) == expected


def test_board_module_is_standard_library_only() -> None:
    """install.sh runs it with the system Python before anything is installed."""
    tree = ast.parse((ROOT / "pepperwatch" / "board.py").read_text())
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    imported.discard("__future__")
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_shell_output_is_safe_to_eval() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pepperwatch.board", "--shell"],
        capture_output=True, text=True, cwd=ROOT, check=True,
    )
    names = [line.split("=", 1)[0] for line in result.stdout.splitlines()]
    assert {"BOARD_KEY", "BOARD_TIER", "BOARD_MEM_CLASS", "BOARD_IS_RPI", "BOARD_SUPPORTED"} <= set(names)


# -- profile layering -------------------------------------------------------------


def test_tier_profiles_ship() -> None:
    for tier in TIER_PROFILES:
        assert profile_path(tier) is not None, f"config/profiles/{tier}.toml is missing"


def test_profile_files_are_tiers_or_board_keys() -> None:
    for candidate in PROFILE_DIR.glob("*.toml"):
        assert candidate.stem in TIER_PROFILES or candidate.stem == candidate.stem.lower(), candidate.name


def test_the_high_tier_runs_the_defaults(monkeypatch) -> None:
    monkeypatch.setenv("PW_BOARD", "unknown")
    monkeypatch.setenv("PW_TIER", "high")
    cfg = load()
    assert cfg.board_profile is None
    assert cfg.board_tier == "high"
    assert cfg.inference.input_size == 640


def _tier(monkeypatch, tier: str):
    monkeypatch.setenv("PW_BOARD", "unknown")
    monkeypatch.setenv("PW_TIER", tier)
    return load()


def test_tiers_scale_the_expensive_settings_down_in_order(monkeypatch) -> None:
    configs = {tier: _tier(monkeypatch, tier) for tier in ("high", "mid", "low", "tiny")}
    for field in ("input_size",):
        values = [configs[t].inference[field] for t in ("high", "mid", "low", "tiny")]
        assert values == sorted(values, reverse=True), (field, values)
    previews = [configs[t].camera.preview_fps for t in ("high", "mid", "low", "tiny")]
    assert previews == sorted(previews, reverse=True)
    assert configs["low"].frames.slot_bytes < configs["high"].frames.slot_bytes
    assert configs["low"].web.max_stream_clients < configs["high"].web.max_stream_clients
    assert configs["low"].board_profile == "low"


def test_a_pi3_gets_the_low_tier(monkeypatch) -> None:
    monkeypatch.delenv("PW_TIER", raising=False)
    monkeypatch.setenv("PW_BOARD", "pi3")
    cfg = load()
    assert cfg.board_tier == "low"
    assert cfg.inference.input_size == 320


@pytest.mark.parametrize("tier", ["mid", "low", "tiny"])
def test_no_tier_lowers_the_fps_requirement(monkeypatch, tier) -> None:
    """§3.2's 17 FPS is the requirement, not a tuning knob.

    Lowering it per board would hide the shortfall the dashboard should be
    reporting honestly. A low-tier board showing "below the 17 fps target" is
    correct.
    """
    assert _tier(monkeypatch, tier).inference.target_fps == 17


def test_profiles_do_not_touch_deployment_identity(monkeypatch) -> None:
    """A profile tunes for hardware; it must not rewrite what the site is."""
    low, high = _tier(monkeypatch, "low"), _tier(monkeypatch, "high")
    assert low.site.name == high.site.name
    assert low.inference.class_names == high.inference.class_names
    assert low.inference.conf_threshold == high.inference.conf_threshold
    assert low.severity.as_dict() == high.severity.as_dict()


def test_environment_still_beats_the_profile(monkeypatch) -> None:
    """Layering order: default -> tier -> board -> local.toml -> environment."""
    monkeypatch.setenv("PW_INFERENCE_INPUT_SIZE", "416")
    assert _tier(monkeypatch, "low").inference.input_size == 416


def test_a_cpu_session_keeps_the_tier_settings(monkeypatch) -> None:
    """The CPU session profile must not undo the tier's lower capture rate."""
    low = _tier(monkeypatch, "low")
    assert low.with_profile("cpu").camera.preview_fps == low.camera.preview_fps == 15
    assert low.with_profile("cpu").inference.intra_op_threads == 2
    assert low.with_profile("hailo").camera.preview_fps > 15


def test_a_board_profile_layers_over_its_tier(monkeypatch, tmp_path) -> None:
    (tmp_path / "low.toml").write_text("[camera]\npreview_fps = 15\n[inference]\ninput_size = 320\n")
    (tmp_path / "orangepi-zero3.toml").write_text("[camera]\npreview_fps = 12\n")
    monkeypatch.setattr(config_module, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(
        config_module, "detect_board_info",
        lambda: board("OrangePi Zero3", ["xunlong,orangepi-zero3", "allwinner,sun50i-h618"],
                      cpuinfo(*["0xd03"] * 4), mem_mb=1024),
    )
    cfg = load()
    assert cfg.board_profile == "low+orangepi-zero3"
    assert cfg.camera.preview_fps == 12          # the board file wins
    assert cfg.inference.input_size == 320       # the tier still applies


def test_detect_board_keeps_its_simple_form(monkeypatch) -> None:
    monkeypatch.delenv("PW_BOARD", raising=False)
    assert detect_board("Raspberry Pi 4 Model B Rev 1.4") == "pi4"
    assert detect_board("") == "unknown"


# -- the memory ceilings -----------------------------------------------------------

UNITS = ("pepperwatch-worker.service", "pepperwatch-web.service")
# RAM the OS actually sees (MiB), and what the OS, NetworkManager, mosquitto
# and sshd need, for the smallest board in each class.
BUDGETS = {"mem-512m": (440, 90), "mem-1g": (940, 150), "mem-2g": (1900, 250)}


def service(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser["Service"]


def memory_max_mb(path: Path) -> float:
    """MemoryMax in MiB, the unit systemd's M suffix means."""
    raw = service(path).get("MemoryMax", "")
    scale = {"K": 1 / 1024, "M": 1, "G": 1024}
    return float(raw[:-1]) * scale[raw[-1].upper()] if raw else 0.0


def dropin(mem_class: str, unit: str) -> Path:
    return SYSTEMD / mem_class / f"{unit}.d" / "10-memory.conf"


@pytest.mark.parametrize("mem_class", sorted(BUDGETS))
def test_dropins_exist_for_both_services(mem_class) -> None:
    for unit in UNITS:
        assert dropin(mem_class, unit).exists(), f"missing {dropin(mem_class, unit).relative_to(ROOT)}"


@pytest.mark.parametrize("mem_class", sorted(BUDGETS))
def test_ceilings_fit_in_physical_ram(mem_class) -> None:
    """The fix: caps below RAM, so systemd reclaims before the OOM killer."""
    visible, reserve = BUDGETS[mem_class]
    total = sum(memory_max_mb(dropin(mem_class, unit)) for unit in UNITS)
    assert total > 0
    assert total + reserve < visible, (
        f"{mem_class}: {total:.0f}M of limits plus {reserve}M for the OS exceeds ~{visible}M"
    )


def test_base_ceilings_need_the_dropins_below_3_gb() -> None:
    """Documents the defect the drop-ins fix, and that 4 GB boards need none."""
    total = sum(memory_max_mb(SYSTEMD / unit) for unit in UNITS)
    assert total > 1900, "the base units now fit a 2 GB board; the drop-ins may be unnecessary"
    assert total + 250 < 3800, "the base units no longer fit a 4 GB board"


@pytest.mark.parametrize("mem_class", sorted(BUDGETS))
def test_reclaims_before_it_kills(mem_class) -> None:
    """MemoryHigh throttles under pressure; MemoryMax is the hard stop."""
    for unit in UNITS:
        section = service(dropin(mem_class, unit))
        high = float(section["MemoryHigh"].rstrip("M"))
        hard = float(section["MemoryMax"].rstrip("M"))
        assert high < hard, f"{mem_class} {unit}: MemoryHigh {high} is not below MemoryMax {hard}"


@pytest.mark.parametrize("mem_class", ["mem-512m", "mem-1g"])
def test_small_boards_keep_the_dashboard_above_the_worker(mem_class) -> None:
    """An unresponsive dashboard looks like a crashed system."""
    weight = {unit: int(service(dropin(mem_class, unit))["CPUWeight"]) for unit in UNITS}
    assert weight["pepperwatch-web.service"] > weight["pepperwatch-worker.service"]


@pytest.mark.parametrize("mem_class", sorted(BUDGETS))
def test_web_dropins_replace_rather_than_append_execstart(mem_class) -> None:
    """systemd appends ExecStart= unless it is first cleared with an empty one."""
    body = dropin(mem_class, "pepperwatch-web.service").read_text()
    if "ExecStart=/" in body:
        assert "\nExecStart=\n" in body, "ExecStart is not cleared before being redefined"


# -- installer ----------------------------------------------------------------------

INSTALL = (ROOT / "deploy" / "install.sh").read_text()


def test_installer_identifies_the_board_with_the_shared_module() -> None:
    assert "pepperwatch.board --shell" in INSTALL
    assert "BOARD_SUPPORTED" in INSTALL


def test_installer_refuses_a_32_bit_userland() -> None:
    """ONNX Runtime publishes no 32-bit ARM wheels, so inference cannot run."""
    assert "aarch64" in INSTALL and "64-bit" in INSTALL


def test_installer_applies_memory_dropins_by_ram() -> None:
    assert "BOARD_MEM_CLASS" in INSTALL
    assert "DROPIN_SRC" in INSTALL and "service.d" in INSTALL
    assert "10-pi3.conf" in INSTALL, "drop-ins from older installs are not cleared"


def test_installer_installs_the_pi_camera_stack_only_on_a_pi() -> None:
    """python3-picamera2 and rpicam-apps exist only in Raspberry Pi's repository."""
    guard = INSTALL.index('if [ "$BOARD_IS_RPI" = "1" ]; then')
    assert INSTALL.index("python3-picamera2", guard) > guard
    assert "python3-picamera2" not in INSTALL[:guard]


def test_installer_leaves_the_access_point_off_without_wifi() -> None:
    assert "ap.disabled" in INSTALL
    unit = (SYSTEMD / "pepperwatch-ap.service").read_text()
    assert "ConditionPathExists=!/etc/pepperwatch/ap.disabled" in unit


def test_installer_enables_zram_on_low_memory_boards() -> None:
    """Compressed swap in RAM, so installing onnxruntime on 1 GB does not OOM.

    zram rather than a swap file because the SD card is the likeliest component
    to fail and swap writes would accelerate that.
    """
    assert "zram" in INSTALL and "MEM_MB" in INSTALL
