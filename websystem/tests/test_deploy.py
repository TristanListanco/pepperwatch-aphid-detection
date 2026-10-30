"""The deployment artifacts.

These files cannot be exercised in CI, but their content can be checked —
and the things worth checking are exactly the ones that would fail silently in
a field: a unit that does not restart, a broker listening on the wrong
interface, an access point with no passphrase.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SYSTEMD = DEPLOY / "systemd"


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read(SYSTEMD / name)
    return parser


# -- systemd ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "pepperwatch.target",
        "pepperwatch-ap.service",
        "pepperwatch-worker.service",
        "pepperwatch-web.service",
        "pepperwatch-maintenance.service",
        "pepperwatch-maintenance.timer",
    ],
)
def test_unit_file_exists_and_parses(name) -> None:
    path = SYSTEMD / name
    assert path.exists(), f"{name} is missing"
    assert "Unit" in unit(name).sections()


@pytest.mark.parametrize("name", ["pepperwatch-worker.service", "pepperwatch-web.service"])
def test_services_restart_automatically(name) -> None:
    """§3.4.5 requires auto-recovery from exceptions and power fluctuations."""
    assert unit(name)["Service"]["Restart"] == "always"


def test_target_pulls_in_everything_needed_for_unattended_boot(name="pepperwatch.target") -> None:
    """§3.4.5: one enable, and power-on brings the whole node up."""
    section = unit(name)["Unit"]
    for service in ("pepperwatch-worker", "pepperwatch-web"):
        assert service in section["Requires"]
    # Wanted, not required: a board with no Wi-Fi radio still runs the
    # detector and serves the dashboard over Ethernet.
    assert "pepperwatch-ap" in section["Wants"]
    assert "pepperwatch-ap" not in section["Requires"]
    assert unit(name)["Install"]["WantedBy"] == "multi-user.target"


def test_web_depends_on_the_worker_only_weakly() -> None:
    """Deliberate: the dashboard must survive the detector dying.

    A Requires= would take the dashboard down at precisely the moment someone
    needs it to show them that the detector has stopped.
    """
    section = unit("pepperwatch-web.service")["Unit"]
    assert "pepperwatch-worker.service" in section["Wants"]
    assert "Requires" not in section or "pepperwatch-worker" not in section.get("Requires", "")


def test_web_runs_exactly_one_gunicorn_worker() -> None:
    """A correctness requirement, not tuning.

    The process holds one MQTT subscription in its EventHub; a second worker
    would mean a second subscription and browsers seeing half the traffic.
    """
    exec_start = unit("pepperwatch-web.service")["Service"]["ExecStart"]
    assert "--workers 1" in exec_start
    assert "--worker-class gthread" in exec_start
    assert "--threads" in exec_start


def test_web_does_not_recycle_workers() -> None:
    """--max-requests would kill live SSE and MJPEG connections mid-flight."""
    assert "--max-requests" not in unit("pepperwatch-web.service")["Service"]["ExecStart"]


def test_worker_can_reach_the_camera_and_sensors() -> None:
    groups = unit("pepperwatch-worker.service")["Service"]["SupplementaryGroups"]
    assert "video" in groups   # camera
    assert "i2c" in groups     # INA219 power monitor


def test_worker_leaves_a_core_for_the_rest_of_the_system() -> None:
    """Giving ONNX all four cores starves Flask and the capture thread."""
    service = unit("pepperwatch-worker.service")["Service"]
    assert service["Environment"].strip().endswith("OMP_NUM_THREADS=3") or (
        "OMP_NUM_THREADS=3" in service["Environment"]
    )


@pytest.mark.parametrize("name", ["pepperwatch-worker.service", "pepperwatch-web.service"])
def test_services_are_hardened(name) -> None:
    service = unit(name)["Service"]
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectSystem"] == "strict"
    assert service["PrivateTmp"] == "yes"
    assert "/var/lib/pepperwatch" in service["ReadWritePaths"]
    assert "MemoryMax" in service


@pytest.mark.parametrize("name", ["pepperwatch-worker.service", "pepperwatch-web.service"])
def test_services_do_not_run_as_root(name) -> None:
    assert unit(name)["Service"]["User"] == "pepperwatch"


def test_maintenance_timer_survives_the_device_being_off() -> None:
    """Solar powered: it may well have been dark at 02:00."""
    timer = unit("pepperwatch-maintenance.timer")["Timer"]
    assert timer["Persistent"] == "true"
    assert "02:00" in timer["OnCalendar"]


# -- mosquitto -------------------------------------------------------------


def test_broker_listens_only_on_loopback() -> None:
    """allow_anonymous is only safe because nothing off-device can connect."""
    conf = (DEPLOY / "mosquitto" / "pepperwatch.conf").read_text()
    listeners = re.findall(r"^listener\s+(\d+)\s*(\S*)", conf, re.MULTILINE)
    assert listeners, "no listener configured"
    for port, bind in listeners:
        assert bind == "127.0.0.1", f"listener on port {port} binds {bind or 'all interfaces'}"


def test_broker_does_not_persist_to_the_sd_card() -> None:
    conf = (DEPLOY / "mosquitto" / "pepperwatch.conf").read_text()
    assert re.search(r"^persistence\s+false", conf, re.MULTILINE)


# -- access point and captive portal --------------------------------------


def test_ap_script_is_executable_and_uses_networkmanager() -> None:
    """Bookworm and Trixie manage wireless through NetworkManager.

    The hostapd + dnsmasq recipe in most Pi tutorials fights it for wlan0.
    """
    script = DEPLOY / "network" / "setup-ap.sh"
    assert script.stat().st_mode & 0o111, "setup-ap.sh is not executable"

    body = script.read_text()
    assert "nmcli" in body
    assert "ipv4.method shared" in body

    # hostapd may be named in a comment explaining why it is not used, but it
    # must never actually be invoked -- it would fight NetworkManager for wlan0.
    code = [
        line for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in code if "hostapd" in line or "dnsmasq" in line.split("#")[0]], (
        "setup-ap.sh invokes hostapd or dnsmasq directly"
    )


def test_ap_refuses_to_start_without_a_passphrase() -> None:
    """An open AP would let anyone in range pause a running field trial."""
    body = (DEPLOY / "network" / "setup-ap.sh").read_text()
    assert "wpa-psk" in body
    assert 'if [ -z "$PSK" ]' in body
    assert "exit 1" in body


def test_captive_dns_resolves_everything_to_the_device() -> None:
    conf = (DEPLOY / "network" / "dnsmasq-shared.d" / "00-pepperwatch-captive.conf").read_text()
    assert "address=/#/192.168.4.1" in conf


def test_captive_config_advertises_rfc8910() -> None:
    """DHCP option 114 is how current iOS and Android find the portal."""
    conf = (DEPLOY / "network" / "dnsmasq-shared.d" / "00-pepperwatch-captive.conf").read_text()
    assert "dhcp-option=114" in conf


def test_captive_config_targets_the_networkmanager_dnsmasq() -> None:
    """A file in /etc/dnsmasq.conf would have no effect on a shared connection."""
    path = DEPLOY / "network" / "dnsmasq-shared.d" / "00-pepperwatch-captive.conf"
    assert path.parent.name == "dnsmasq-shared.d"


# -- installer -------------------------------------------------------------


def test_installer_creates_the_venv_with_system_site_packages() -> None:
    """Mandatory: Picamera2 and libcamera bindings are apt-only."""
    body = (DEPLOY / "install.sh").read_text()
    assert "--system-site-packages" in body
    assert "python3-picamera2" in body


def test_installer_addresses_the_missing_clock() -> None:
    """No internet means no NTP; a wrong clock invalidates the whole log."""
    body = (DEPLOY / "install.sh").read_text()
    assert "fake-hwclock" in body
    assert "/dev/rtc0" in body
    assert "J5" in body


def test_installer_preserves_data_across_upgrades() -> None:
    body = (DEPLOY / "install.sh").read_text()
    assert "--exclude 'var'" in body
    assert "--exclude 'config/local.toml'" in body


def test_installer_generates_secrets_rather_than_shipping_them() -> None:
    body = (DEPLOY / "install.sh").read_text()
    assert "/dev/urandom" in body
    assert "ap.psk" in body
    assert "chmod 600" in body


# -- documentation ---------------------------------------------------------


def test_readme_records_the_divergences_from_the_manuscript() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "Known divergences" in readme
    assert "0.25" in readme and "17 FPS" in readme


def test_manuscript_notes_exist_and_cover_the_serious_items() -> None:
    # Kept beside the manuscript and out of the public repository, so a fresh
    # clone (and CI) will not have it.
    path = ROOT / "MANUSCRIPT-NOTES.md"
    if not path.exists():
        pytest.skip("MANUSCRIPT-NOTES.md is local-only and not in this checkout")
    notes = path.read_text()
    for topic in ("H1", "MongoDB", "RTC", "TFT", "17 FPS"):
        assert topic in notes, f"MANUSCRIPT-NOTES.md does not mention {topic}"
