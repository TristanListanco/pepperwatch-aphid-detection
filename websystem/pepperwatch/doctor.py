"""Preflight diagnostics: ``python -m pepperwatch.doctor``.

Run this after installing on the Pi, and whenever something behaves oddly. It
checks the things that actually go wrong in this deployment, in the order they
tend to bite:

* **The numpy/libcamera ABI trap.** The venv needs ``--system-site-packages``
  to see apt's Picamera2, but that flag also lets a pip-installed numpy shadow
  apt's — and libcamera's C bindings were compiled against apt's. The import
  then fails with something that looks nothing like the actual cause. This is
  the single most common Pi setup failure, so it is checked first.
* **The clock.** No internet means no NTP. A Pi with no RTC battery boots to a
  meaningless date, which silently makes every timestamp in the Historical
  Detection Log worthless.
* Which cameras are attached, and whether an AI HAT+ is usable: on the bus,
  driver loaded, runtime installed, a compiled model present, and the PCIe
  link running at Gen 3.
* Power sources, the session folder, broker, database, disk, and thermals.

Exit code is 0 when everything passes, 1 if anything failed.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def check_python() -> Check:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro} at {sys.executable}"
    if version < (3, 10):
        return Check("python", FAIL, text, "Python 3.10 or newer is required")
    if version >= (3, 14):
        return Check("python", WARN, text, "3.14+ is untested; onnxruntime and cv2 wheels lag")
    return Check("python", OK, text)


def check_venv() -> Check:
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        return Check("venv", WARN, "not running inside a virtual environment",
                     "expected .venv; see deploy/install.sh")

    # On a Pi the venv MUST be able to see apt's packages, or Picamera2 is
    # invisible no matter how it was installed.
    marker = Path(sys.prefix) / "pyvenv.cfg"
    system_site = "false"
    try:
        for line in marker.read_text().splitlines():
            if line.startswith("include-system-site-packages"):
                system_site = line.split("=", 1)[1].strip()
    except OSError:
        pass

    if _is_pi() and system_site != "true":
        return Check(
            "venv", FAIL, f"system site packages: {system_site}",
            "recreate with: python3 -m venv --system-site-packages .venv "
            "(Picamera2 is apt-only and cannot be seen otherwise)",
        )
    return Check("venv", OK, f"{sys.prefix} (system site packages: {system_site})")


def check_numpy_abi() -> Check:
    """The trap that --system-site-packages sets, checked explicitly."""
    try:
        import numpy
    except ImportError as exc:
        return Check("numpy", FAIL, str(exc), "pip install numpy")

    location = Path(numpy.__file__).resolve()
    in_venv = str(location).startswith(str(Path(sys.prefix).resolve()))

    if _is_pi() and in_venv:
        return Check(
            "numpy", WARN,
            f"{numpy.__version__} from the venv ({location.parent})",
            "a pip-installed numpy shadows apt's, and libcamera's bindings were "
            "built against apt's. If the camera check below fails, run: "
            "pip uninstall numpy  (and let the system copy be used)",
        )
    return Check("numpy", OK, f"{numpy.__version__} from {location.parent}")


def check_imports() -> list[Check]:
    checks = []
    for module, hint in (
        ("cv2", "pip install opencv-python-headless"),
        ("flask", "pip install flask"),
        ("paho.mqtt.client", "pip install paho-mqtt"),
        ("onnxruntime", "pip install onnxruntime"),
        ("psutil", "pip install psutil"),
        ("PIL", "pip install Pillow"),
    ):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            checks.append(Check(module, FAIL, str(exc), hint))
            continue

        # Read the version from package metadata rather than a __version__
        # attribute: Flask deprecated theirs, and several of these never had one.
        distribution = {
            "cv2": "opencv-python-headless",
            "paho.mqtt.client": "paho-mqtt",
            "PIL": "Pillow",
        }.get(module, module)
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            version = "installed"
        checks.append(Check(module, OK, version))
    return checks


def check_clock() -> Check:
    """No NTP offline; a dead RTC battery makes every timestamp meaningless."""
    now = datetime.now(timezone.utc)
    detail = now.isoformat(timespec="seconds")

    # A Pi with no RTC and no fake-hwclock typically boots to the epoch or to
    # its filesystem build date.
    if now.year < 2025:
        return Check(
            "clock", FAIL, f"{detail} — implausible",
            "fit the Pi 5 RTC battery (J5 connector) and run "
            "`sudo timedatectl set-time '<correct time>'`. Without it every "
            "row in the Historical Detection Log is misdated.",
        )

    rtc_present = Path("/dev/rtc0").exists()
    if _is_pi() and not rtc_present:
        return Check(
            "clock", WARN, f"{detail}, no /dev/rtc0",
            "no hardware clock detected. Offline there is no NTP, so the date "
            "will drift or reset on power loss. Fit the RTC battery (J5).",
        )
    return Check("clock", OK, f"{detail}{', RTC present' if rtc_present else ''}")


def _parse_systemd_bytes(value: str) -> float | None:
    """Parse a systemd memory value to bytes.

    Accepts the suffixed form used in unit files ("460M", "2G"), the plain byte
    count `systemctl show` returns, and "infinity".
    """
    text = str(value).strip()
    if not text or text.lower() == "infinity":
        return None

    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if text[-1].upper() in suffixes:
        try:
            return float(text[:-1]) * suffixes[text[-1].upper()]
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _effective_memory_max(unit: str) -> tuple[float | None, str]:
    """The memory ceiling systemd will actually apply to ``unit``.

    Prefers `systemctl show`, which resolves drop-ins for us. Falls back to
    reading this repository's unit files plus whichever board profile would
    apply, so the check still says something useful before installation.
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MemoryMax", "--value", unit],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_systemd_bytes(result.stdout), "systemd"
    except (OSError, subprocess.SubprocessError):
        pass

    from .board import memory_class
    from .config import PROJECT_ROOT, total_memory_mb

    limit = None
    mem_mb = total_memory_mb()
    mem_class = memory_class(int(mem_mb)) if mem_mb else None
    candidates = [PROJECT_ROOT / "deploy" / "systemd" / unit]
    if mem_class:
        candidates.append(PROJECT_ROOT / "deploy" / "systemd" / mem_class / f"{unit}.d" / "10-memory.conf")
    for candidate in candidates:
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            if line.strip().startswith("MemoryMax="):
                parsed = _parse_systemd_bytes(line.split("=", 1)[1])
                if parsed is not None:
                    limit = parsed  # later file wins, as a drop-in does
    return limit, "unit files"


def check_board() -> Check:
    """Report the board, its tier, and which settings profiles apply."""
    from .config import detect_board_info, load

    info = detect_board_info()
    memory = f"{info.mem_mb / 1024:.1f} GB RAM" if info.mem_mb else "unknown RAM"
    detail = f"{info.model or 'unknown'} ({info.family}; {info.cpu}; {memory}) -> {info.tier} tier"

    if not info.supported:
        return Check("board", FAIL, detail, info.reason)
    applied = load().board_profile
    if applied:
        return Check("board", OK, f"{detail}, profile {applied}")
    if info.tier == "high":
        return Check("board", OK, f"{detail}, default settings")
    return Check(
        "board", WARN, f"{detail}, but no config/profiles/{info.tier}.toml",
        "the defaults are sized for a Pi 5 and will run slowly here",
    )


def check_memory_budget() -> Check:
    """Do the service memory ceilings actually fit in this machine's RAM?

    This is the check that catches shipping Pi 5 limits to a Pi 3. A MemoryMax
    above physical memory is worse than none at all: it never engages, so the
    kernel's OOM killer fires instead and chooses its victim by heuristic
    rather than by our intent.
    """
    from .config import total_memory_mb

    total_mb = total_memory_mb()
    if total_mb is None:
        return Check("memory budget", WARN, "cannot read total memory")

    units = ("pepperwatch-worker.service", "pepperwatch-web.service")
    limits: dict[str, float | None] = {}
    source = "unit files"
    for unit in units:
        limit, source = _effective_memory_max(unit)
        limits[unit] = limit

    if all(limit is None for limit in limits.values()):
        return Check("memory budget", WARN, "no MemoryMax set on either service",
                     "a runaway process could take the whole device down")

    capped_mb = sum((limit or 0) / 1e6 for limit in limits.values())
    breakdown = ", ".join(
        f"{unit.split('-')[1].split('.')[0]} {(limit / 1e6):.0f}M" if limit else
        f"{unit.split('-')[1].split('.')[0]} unlimited"
        for unit, limit in limits.items()
    )
    detail = f"{breakdown} = {capped_mb:.0f}M of {total_mb:.0f}M ({source})"

    # Leave room for the OS, NetworkManager, mosquitto and sshd.
    reserve_mb = 150.0
    if capped_mb + reserve_mb > total_mb:
        return Check(
            "memory budget", FAIL, detail,
            f"the service ceilings exceed physical RAM once ~{reserve_mb:.0f}M is "
            "reserved for the OS, so they will never engage and the OOM killer "
            "will fire instead. Re-run sudo ./deploy/install.sh, which applies "
            "the deploy/systemd/mem-*/ limits for this much RAM",
        )
    if capped_mb + reserve_mb > total_mb * 0.92:
        return Check("memory budget", WARN, detail, "very little headroom left for the OS")
    return Check("memory budget", OK, detail)


def check_camera(probe: bool) -> Check:
    """List the attached cameras; optionally open the chosen one for a frame."""
    from .config import load
    from .hal.discovery import choose_camera, discover_cameras

    cfg = load()
    found = discover_cameras()
    listed = ", ".join(c.describe() for c in found) or "none"
    chosen = choose_camera(found, kind=str(cfg.camera.kind), device=str(cfg.camera.device or ""))
    if chosen is None:
        return Check(
            "camera", FAIL, f"no camera for kind={cfg.camera.kind!r}; found: {listed}",
            "check the CSI ribbon (a Pi 5 needs the narrow 22-pin cable) or the USB "
            "cable, then `rpicam-hello --list-cameras` and `ls /dev/video*`",
        )
    if not probe:
        return Check("camera", OK, f"will use {chosen.describe()}; found: {listed}")

    try:
        from .hal import open_camera

        camera, _ = open_camera(cfg)
        try:
            frame = None
            deadline = time.monotonic() + 6
            while frame is None and time.monotonic() < deadline:
                frame = camera.read()
            if frame is None:
                return Check("camera", FAIL, f"{chosen.describe()} opened but produced no frame within 6s",
                             "check the cable seating and `rpicam-hello --list-cameras`")
            return Check("camera", OK,
                         f"captured {frame.width}x{frame.height} from {camera.info.description}")
        finally:
            camera.stop()
    except Exception as exc:  # noqa: BLE001
        return Check("camera", FAIL, f"{type(exc).__name__}: {exc}",
                     "if this mentions numpy or libcamera, see the numpy check above")


def check_accelerator() -> Check:
    """Is an AI HAT+ attached, and can this session use it?"""
    from .accel import ARCH_PACKAGES, choose_accelerator, probe_hailo
    from .config import load

    cfg = load()
    probe = probe_hailo()
    if not probe.present:
        return Check("accelerator", OK, "no AI HAT+ found; inference runs on the CPU")

    choice = choose_accelerator(cfg, probe)
    link = probe.pcie_link or "unknown link"
    if choice.kind != "hailo":
        package = ARCH_PACKAGES.get(probe.arch or "hailo8", "hailo-all")
        return Check(
            "accelerator", WARN, f"{probe.name} found ({link}) but not usable: {choice.reason}",
            f"sudo apt install {package} && sudo reboot, then put a HEF compiled for the "
            f"{probe.name} in models/ (see README: Adding the trained model)",
        )
    if link.startswith("5.0") or link.startswith("2.5"):
        return Check(
            "accelerator", WARN, f"{choice.reason}; HEF {choice.hef_path.name}",
            "the PCIe link is below Gen 3, which roughly halves the HAT's bandwidth: "
            "add dtparam=pciex1_gen=3 to /boot/firmware/config.txt and reboot",
        )
    return Check("accelerator", OK, f"{choice.reason}; HEF {choice.hef_path.name}")


def check_power() -> Check:
    """Which power readings the session CSV will contain."""
    from .hal.power import Vcgencmd, parse_pmic_adc

    sources = []
    text = Vcgencmd().run("pmic_read_adc")
    reading = parse_pmic_adc(text) if text else None
    if reading and reading.rails_w is not None:
        supply = f", supply {reading.input_voltage_v} V" if reading.input_voltage_v else ""
        sources.append(f"PMIC rails {reading.rails_w:.2f} W{supply}")
    try:
        import board  # type: ignore[import-not-found]  # noqa: F401
        from adafruit_ina219 import INA219  # type: ignore[import-not-found]

        sensor = INA219(board.I2C())
        sources.append(f"INA219 {float(sensor.power):.2f} W")
    except Exception:  # noqa: BLE001
        pass

    if not sources:
        from .config import detect_board

        hint = (
            "vcgencmd pmic_read_adc should work on any Pi 5; check the user is in group video"
            if detect_board() == "pi5"
            else "only the Pi 5 measures its own power; fit an INA219 on I2C for power and "
            "energy figures, especially when comparing boards"
        )
        return Check("power", WARN, "no power readings available", hint)
    if len(sources) == 1:
        return Check("power", OK, sources[0] + " (no INA219: whole-device draw is not measured)")
    return Check("power", OK, "; ".join(sources))


def check_sessions() -> Check:
    """Can the worker write its session CSV files?"""
    from .config import load

    root = load().sessions_root
    probe_dir = root if root.exists() else root.parent
    if not os.access(probe_dir, os.W_OK):
        return Check("sessions", FAIL, f"{root} is not writable",
                     "the systemd units set PW_SESSIONS_ROOT=/var/lib/pepperwatch/sessions; "
                     "re-run sudo ./deploy/install.sh")
    count = len(list(root.glob("*/session.json"))) if root.exists() else 0
    return Check("sessions", OK, f"{root} ({count} recorded)")


def check_broker() -> Check:
    from .config import load

    cfg = load()
    try:
        with socket.create_connection((cfg.mqtt.host, cfg.mqtt.port), timeout=2):
            return Check("mqtt", OK, f"broker reachable at {cfg.mqtt.host}:{cfg.mqtt.port}")
    except OSError as exc:
        return Check(
            "mqtt", FAIL, f"{cfg.mqtt.host}:{cfg.mqtt.port} unreachable ({exc})",
            "sudo systemctl start mosquitto  (telemetry and controls need it; "
            "detection and logging continue without it)",
        )


def check_database() -> Check:
    from .config import load
    from .db import Database

    cfg = load()
    try:
        database = Database(cfg)
        database.initialise(writer="doctor")
        stats = database.stats()
        database.close()
    except Exception as exc:  # noqa: BLE001
        return Check("database", FAIL, str(exc), f"check permissions on {cfg.db_path.parent}")

    if stats["journal_mode"] != "wal":
        return Check("database", FAIL, f"journal_mode is {stats['journal_mode']}",
                     "WAL is required for concurrent read/write (§3.3.2.1)")

    counts = stats["counts"]
    return Check(
        "database", OK,
        f"WAL, {counts['detection_event']} events, {counts['bounding_box']} boxes, "
        f"{stats['size_mb']} MB (+{stats['wal_mb']} MB wal)",
    )


def check_model() -> Check:
    """Load the model this board would use, and run it once on a blank frame."""
    import numpy as np

    from .accel import choose_accelerator
    from .config import load
    from .detect import get_detector

    base = load()
    choice = choose_accelerator(base)
    cfg = base.with_profile(choice.profile)
    try:
        detector, final = get_detector(cfg, choice)
    except Exception as exc:  # noqa: BLE001
        return Check("model", FAIL, f"would not load: {type(exc).__name__}: {exc}",
                     "re-export it; see README: Adding the trained model")

    frame = np.zeros((cfg.camera.preview_height, cfg.camera.preview_width, 3), np.uint8)
    try:
        detector.detect(frame)  # warm-up: the first call allocates
        began = time.perf_counter()
        detector.detect(frame)
        ms = (time.perf_counter() - began) * 1000
        info = detector.info
    except Exception as exc:  # noqa: BLE001 — e.g. a head with the wrong class count
        return Check("model", FAIL, f"loaded but inference failed: {exc}",
                     f"check the model was trained for {list(cfg.inference.class_names)}")
    finally:
        detector.close()

    name = Path(info.detail.get("hef_path") or info.detail.get("model_path") or "").name
    detail = (f"{name or info.model_version} on {final.name}, {info.input_size} px, "
              f"classes {', '.join(info.class_names)}, {ms:.0f} ms per frame")
    if info.synthetic:
        return Check(
            "model", WARN, f"no model file; {detail}",
            "the heuristic stub runs instead. Its output is NOT model output "
            "and every event it writes is flagged synthetic — do not report it.",
        )
    if final.kind != choice.kind:
        return Check("model", WARN, f"{detail} (the AI HAT+ failed: {final.reason})",
                     "if the worker is running it holds the HAT: "
                     "sudo systemctl stop pepperwatch-worker, then re-run doctor")
    if info.input_size != cfg.inference.input_size:
        return Check("model", WARN, detail,
                     f"this board's tier expects {cfg.inference.input_size} px; export "
                     f"models/pepperwatch_{cfg.inference.input_size}.onnx for full speed")
    return Check("model", OK, detail)


def check_storage() -> Check:
    from .config import load

    cfg = load()
    try:
        usage = shutil.disk_usage(cfg.db_path.parent)
    except OSError as exc:
        return Check("storage", FAIL, str(exc))

    free_gb = usage.free / 1e9
    percent_free = 100.0 * usage.free / usage.total

    if free_gb < 1.0:
        return Check("storage", FAIL, f"{free_gb:.1f} GB free ({percent_free:.0f}%)",
                     "captured images will start failing")
    if free_gb < 4.0:
        return Check("storage", WARN, f"{free_gb:.1f} GB free ({percent_free:.0f}%)",
                     "an 8-hour cycle writes images continuously")
    return Check("storage", OK, f"{free_gb:.1f} GB free ({percent_free:.0f}%)")


def check_thermal() -> Check:
    try:
        celsius = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        return Check("thermal", WARN, "cannot read thermal zone")

    if celsius >= 80:
        return Check("thermal", FAIL, f"{celsius:.1f} °C",
                     "at or past the soft throttle point; check the fan and enclosure venting")
    if celsius >= 70:
        return Check("thermal", WARN, f"{celsius:.1f} °C",
                     "the governor begins backing off inference at 75 °C")
    return Check("thermal", OK, f"{celsius:.1f} °C")


def check_advisory() -> Check:
    from .advisory import load_advisory
    from .config import load

    advisory = load_advisory(load())
    if advisory.validated:
        return Check("advisory", OK, "content signed off")
    return Check(
        "advisory", WARN, "treatment guidance is an unvalidated draft",
        "have an agriculturist review config/recommendations.json and set "
        "meta.status to 'validated' before this reaches farmers",
    )


def _is_pi() -> bool:
    from .config import is_raspberry_pi

    return is_raspberry_pi()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera-probe", action="store_true",
                        help="Open the camera and capture a test frame")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR)

    from .config import detect_board_info

    info = detect_board_info()
    print("Pepper Watch diagnostics")
    print(f"  host     : {platform.node()} ({platform.machine()})")
    print(f"  board    : {info.model or 'unknown'} [{info.key}, {info.tier} tier]")
    print(f"  cwd      : {os.getcwd()}")
    print()

    checks = [check_board(), check_python(), check_venv(), check_numpy_abi()]
    checks += check_imports()
    checks += [
        check_memory_budget(),
        check_clock(),
        check_camera(args.camera_probe),
        check_accelerator(),
        check_model(),
        check_power(),
        check_sessions(),
        check_broker(),
        check_database(),
        check_storage(),
        check_thermal(),
        check_advisory(),
    ]

    width = max(len(check.name) for check in checks)
    failures = warnings = 0
    for check in checks:
        if check.status == FAIL:
            failures += 1
        elif check.status == WARN:
            warnings += 1
        print(f"  [{check.status:>4}] {check.name:<{width}}  {check.detail}")
        if check.hint and check.status != OK:
            for line in _wrap(check.hint, 88 - width):
                print(f"         {'':<{width}}  -> {line}")

    print()
    if failures:
        print(f"{failures} failed, {warnings} warnings")
        return 1
    print(f"all checks passed{f' ({warnings} warnings)' if warnings else ''}")
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, max(width, 40))


if __name__ == "__main__":
    raise SystemExit(main())
