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
        "pepperwatch-ap-watch.service",
        "pepperwatch-ap-watch.timer",
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


@pytest.mark.parametrize("name", ["pepperwatch-worker.service", "pepperwatch-web.service"])
def test_systemd_never_gives_up_restarting(name) -> None:
    """A start limit leaves a field node with no detector until someone reboots it."""
    assert unit(name)["Unit"]["StartLimitIntervalSec"] == "0"
    assert "StartLimitBurst" not in unit(name)["Service"]


def test_target_pulls_in_everything_needed_for_unattended_boot(name="pepperwatch.target") -> None:
    """§3.4.5: one enable, and power-on brings the whole node up."""
    section = unit(name)["Unit"]
    for service in ("pepperwatch-worker", "pepperwatch-web", "pepperwatch-ap"):
        assert service in section["Wants"]
    # Not required: systemd restarts the target, and the access point that is
    # PartOf it, every time a required service restarts itself. A worker
    # crash-looping with no camera dropped the hotspot every few seconds.
    assert "Requires" not in section
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


def test_streams_cannot_take_every_web_thread() -> None:
    """Each open stream holds a thread: with only as many threads as stream
    slots, phones left streaming would freeze the dashboard for everyone."""
    import re

    from pepperwatch.config import load

    threads = int(re.search(r"--threads (\d+)", unit("pepperwatch-web.service")["Service"]["ExecStart"])[1])
    cfg = load(use_env=False)
    streams = int(cfg.web.max_stream_clients) * int(cfg.camera.max_cameras)  # phones x cameras
    assert threads >= streams + int(cfg.web.max_sse_clients) + 8


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


# -- a saved Wi-Fi network in range, or the hotspot ------------------------

FAKE_NMCLI = r"""#!/usr/bin/env bash
echo "nmcli $*" >> "$FAKE_LOG"
case "$*" in
  "device status") printf 'wlan0 wifi disconnected\n' ;;
  "-t --escape no -f TYPE,NAME connection show")
    printf '802-11-wireless:pepperwatch-ap\n802-3-ethernet:Wired connection 1\n'
    [ -n "$FAKE_SAVED" ] && printf '802-11-wireless:%s\n' "$FAKE_SAVED" ;;
  "-g GENERAL.STATE device show wlan0") echo "${FAKE_STATE:-30 (disconnected)}" ;;
  "-g GENERAL.CONNECTION device show wlan0") echo "$FAKE_CURRENT" ;;
  "--wait 30 connection up $FAKE_SAVED ifname wlan0") [ -n "$FAKE_UP_FAILS" ] && exit 4 ;;
  "-g 802-11-wireless.mode connection show pepperwatch-ap") echo ap ;;
  "-g 802-11-wireless.mode connection show $FAKE_SAVED") echo infrastructure ;;
  "-g connection.autoconnect connection show $FAKE_SAVED") echo yes ;;
  "-g 802-11-wireless.ssid connection show $FAKE_SAVED") echo "$FAKE_SAVED" ;;
  "-t -f SSID device wifi list ifname wlan0 --rescan yes")
    scans=$(( $(cat "$FAKE_LOG.scans" 2>/dev/null || echo 0) + 1 ))
    echo "$scans" > "$FAKE_LOG.scans"
    eval "seen=\${FAKE_VISIBLE_$scans-\$FAKE_VISIBLE}"  # FAKE_VISIBLE_2: what the 2nd scan sees
    printf '%s\n' $seen ;;
  "-g NAME connection show") echo "${FAKE_NAMES-pepperwatch-ap}" ;;
  "--wait 20 connection up pepperwatch-ap")
    tries=$(( $(cat "$FAKE_LOG.tries" 2>/dev/null || echo 0) + 1 ))
    echo "$tries" > "$FAKE_LOG.tries"
    [ "$tries" -le "${FAKE_AP_FAILS:-0}" ] && exit 4 ;;
  "radio wifi") echo enabled ;;
  "-g IP4.ADDRESS device show wlan0") echo 10.0.0.5/24 ;;
esac
exit 0
"""


def run_setup_ap(
    tmp_path: Path, saved: str = "", visible: str = "", check: bool = True,
    args: tuple[str, ...] = (), always: bool = False, **fake: str,
) -> str:
    """Run setup-ap.sh against a fake nmcli; return what it was asked."""
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nmcli").write_text(FAKE_NMCLI)
    (bin_dir / "nmcli").chmod(0o755)
    (bin_dir / "sleep").write_text("#!/bin/sh\n")  # the retries need not wait here
    (bin_dir / "sleep").chmod(0o755)
    (bin_dir / "nm-online").write_text('#!/bin/sh\necho "nm-online $*" >> "$FAKE_LOG"\n')
    (bin_dir / "nm-online").chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_SAVED": saved,
        "FAKE_VISIBLE": visible,
        "PW_AP_PSK": "a-test-passphrase",
        "PW_AP_ALWAYS_FLAG": str(tmp_path / "ap.always"),
        **{f"FAKE_{key.upper()}": value for key, value in fake.items()},
    }
    if always:
        (tmp_path / "ap.always").touch()
    subprocess.run(
        ["bash", str(DEPLOY / "network" / "setup-ap.sh"), *args],
        env=env, check=check, capture_output=True, text=True,
    )
    return log.read_text()


def test_a_saved_network_in_range_is_joined_and_the_hotspot_stays_off(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, saved="FarmWiFi", visible="Neighbour FarmWiFi")
    assert "connection up FarmWiFi ifname wlan0" in calls
    assert "connection up pepperwatch-ap" not in calls


def test_with_no_saved_network_the_hotspot_starts(tmp_path) -> None:
    calls = run_setup_ap(tmp_path)
    modify = next(line for line in calls.splitlines() if "connection modify" in line)
    assert "connection.autoconnect no" in modify  # only setup-ap.sh starts it
    assert "connection up pepperwatch-ap" in calls


@pytest.mark.parametrize("names", ["pepperwatch-ap", ""], ids=["update", "create"])
def test_the_hotspot_is_wpa2_aes_with_pmf_off(tmp_path, names) -> None:
    """The Pi's radio can't do PMF or WPA3 as an access point; left on, phones
    fail to join or never see the network."""
    calls = run_setup_ap(tmp_path, names=names)
    command = next(line for line in calls.splitlines() if "wifi-sec.key-mgmt" in line)
    for setting in ("wifi-sec.proto rsn", "wifi-sec.pairwise ccmp",
                    "wifi-sec.group ccmp", "wifi-sec.pmf disable"):
        assert setting in command


def test_a_hotspot_that_fails_to_start_at_boot_is_retried(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, ap_fails="2")
    assert calls.count("connection up pepperwatch-ap") == 3


def test_a_hotspot_that_never_starts_fails_the_service(tmp_path) -> None:
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        run_setup_ap(tmp_path, ap_fails="9")


def test_a_saved_network_out_of_range_falls_back_to_the_hotspot(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, saved="FarmWiFi", visible="Neighbour")
    assert "connection up FarmWiFi" not in calls
    assert "connection up pepperwatch-ap" in calls


def test_already_connected_keeps_the_network_and_turns_hotspot_autoconnect_off(tmp_path) -> None:
    """Older installs saved the hotspot with autoconnect on. A Pi that always
    finds its home network must still have it turned off, or a blip in that
    network hands the radio to the hotspot."""
    calls = run_setup_ap(
        tmp_path, saved="FarmWiFi", current="FarmWiFi", state="100 (connected)",
    )
    assert "connection modify pepperwatch-ap connection.autoconnect no" in calls
    assert "device wifi list" not in calls  # no needless rescan
    assert "connection up pepperwatch-ap" not in calls


def test_a_join_still_in_progress_that_fails_starts_the_hotspot(tmp_path) -> None:
    """NetworkManager names the network while still joining it; a wrong
    password then fails the join, and the Pi must not end up with nothing."""
    calls = run_setup_ap(
        tmp_path, saved="FarmWiFi", visible="FarmWiFi", current="FarmWiFi",
        state="70 (connecting (getting IP configuration))", up_fails="1",
    )
    assert "connection up pepperwatch-ap" in calls


def test_a_network_with_a_colon_in_its_name_is_joined(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, saved="Cafe:5G", visible="Cafe:5G")
    assert "connection up Cafe:5G ifname wlan0" in calls
    assert "connection up pepperwatch-ap" not in calls


def test_deploy_scripts_never_pipe_into_an_early_exit_reader() -> None:
    """Under pipefail, `nmcli ... | grep -q` reads false whenever nmcli is still
    writing when grep quits, so setup-ap.sh would re-create a connection that
    exists. Readers here must consume all their input."""
    network = list((DEPLOY / "network").glob("*.sh"))
    for path in (DEPLOY / "install.sh", DEPLOY / "update.sh", *network):
        # The network scripts read nmcli and iw, so no early-quitting reader at
        # all; elsewhere only grep -q, which is what reads a real match as none.
        pattern = r"\|\s*(grep -q|head\b)|exit\s*\}'" if path in network else r"\|\s*grep -q"
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.lstrip().startswith("#"):
                assert not re.search(pattern, line), f"{path.name}:{number}: {line.strip()}"


def test_captive_dns_resolves_everything_to_the_device() -> None:
    conf = (DEPLOY / "network" / "dnsmasq-shared.d" / "00-pepperwatch-captive.conf").read_text()
    assert "address=/#/192.168.4.1" in conf


def test_captive_config_has_no_plain_http_rfc8910_option() -> None:
    """Phones only accept option 114 over HTTPS; an http:// one does nothing."""
    conf = (DEPLOY / "network" / "dnsmasq-shared.d" / "00-pepperwatch-captive.conf").read_text()
    assert not any(line.startswith("dhcp-option=114") for line in conf.splitlines())


@pytest.mark.parametrize(
    "path",
    [
        "pepperwatch-web.service",
        "mem-512m/pepperwatch-web.service.d/10-memory.conf",
        "mem-1g/pepperwatch-web.service.d/10-memory.conf",
    ],
)
def test_web_listens_on_port_80_for_the_captive_portal(path) -> None:
    """Every OS probes for a portal on port 80; on 8000 alone none ever opens.
    The memory drop-ins replace ExecStart, so each must keep the bind."""
    text = (SYSTEMD / path).read_text()
    assert "--bind 0.0.0.0:80 " in text and "--bind 0.0.0.0:8000" in text


def test_web_may_bind_port_80_without_running_as_root() -> None:
    service = unit("pepperwatch-web.service")["Service"]
    assert service["User"] == "pepperwatch"
    assert service["AmbientCapabilities"] == "CAP_NET_BIND_SERVICE"
    assert service["CapabilityBoundingSet"] == "CAP_NET_BIND_SERVICE"


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


def test_the_periodic_check_leaves_a_connected_wifi_alone(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, args=("--if-offline",), saved="FarmWiFi", current="FarmWiFi",
                         state="100 (connected)")
    assert "connection up" not in calls and "connection down" not in calls


def test_the_periodic_check_brings_the_hotspot_back_when_the_network_went_away(tmp_path) -> None:
    """Joined a phone's hotspot at boot; the phone left: unreachable until reboot."""
    calls = run_setup_ap(tmp_path, args=("--if-offline",), saved="PhoneHotspot", visible="Neighbour")
    assert "connection up pepperwatch-ap" in calls


def test_field_mode_starts_the_hotspot_even_with_a_saved_network_in_range(tmp_path) -> None:
    """Campus Wi-Fi in range but keeping phones from reaching the Pi."""
    calls = run_setup_ap(tmp_path, saved="CampusWiFi", visible="CampusWiFi", always=True)
    assert "connection up CampusWiFi" not in calls
    assert "connection up pepperwatch-ap" in calls


def test_field_mode_check_leaves_a_saved_network_for_the_hotspot(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, args=("--if-offline",), saved="CampusWiFi", visible="CampusWiFi",
                         current="CampusWiFi", state="100 (connected)", always=True)
    assert "connection up pepperwatch-ap" in calls


def test_the_wifi_check_runs_every_two_minutes_and_is_enabled() -> None:
    timer = unit("pepperwatch-ap-watch.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "2min"
    assert "--if-offline" in unit("pepperwatch-ap-watch.service")["Service"]["ExecStart"]
    assert "systemctl enable pepperwatch-ap-watch.timer" in (DEPLOY / "install.sh").read_text()


def test_install_generates_an_admin_pin_the_dashboard_can_read() -> None:
    script = (DEPLOY / "install.sh").read_text()
    assert 'install -d -m 750 -o root -g "$APP_USER" "$CONFIG_DIR"' in script
    assert '"$CONFIG_DIR/admin.pin"' in script and "chmod 640" in script
    assert 'chmod 600 "$CONFIG_DIR/ap.psk"' in script  # the Wi-Fi password stays root-only


def test_the_wifi_radio_is_switched_on_before_the_hotspot(tmp_path) -> None:
    calls = run_setup_ap(tmp_path)
    assert calls.index("nmcli radio wifi on") < calls.index("connection up pepperwatch-ap")


def test_install_sets_a_wifi_country_when_none_is_set() -> None:
    """Without one Raspberry Pi OS keeps the radio blocked: no hotspot at all."""
    script = (DEPLOY / "install.sh").read_text()
    assert "raspi-config nonint get_wifi_country" in script
    assert 'raspi-config nonint do_wifi_country "${PW_WIFI_COUNTRY:-PH}"' in script


def test_at_boot_networkmanager_finishes_its_own_join_before_any_scan(tmp_path) -> None:
    """A scan while NetworkManager is still joining can abort the join; a busy
    boot (a USB camera and the model loading) made the hotspot win that race."""
    calls = run_setup_ap(tmp_path, saved="FarmWiFi", visible="FarmWiFi")
    assert calls.index("nm-online -s") < calls.index("device wifi list")
    (tmp_path / "check").mkdir()
    assert "nm-online" not in run_setup_ap(tmp_path / "check", args=("--if-offline",), state="100 (connected)")


def test_a_saved_network_missed_by_the_first_scan_is_still_joined(tmp_path) -> None:
    calls = run_setup_ap(tmp_path, saved="FarmWiFi", visible_1="Neighbour", visible="Neighbour FarmWiFi")
    assert calls.count("device wifi list") == 2
    assert "connection up FarmWiFi ifname wlan0" in calls
    assert "connection up pepperwatch-ap" not in calls


def test_the_periodic_check_leaves_a_connection_being_made_alone(tmp_path) -> None:
    """Someone picking a network from the desktop's Wi-Fi menu: no hotspot over it."""
    calls = run_setup_ap(tmp_path, args=("--if-offline",), saved="FarmWiFi",
                         state="70 (connecting (getting IP configuration))")
    assert "connection up" not in calls and "connection down" not in calls


def test_the_detector_starts_after_the_wifi_decision_and_yields_the_cpu() -> None:
    section = unit("pepperwatch-worker.service")
    assert "pepperwatch-ap.service" in section["Unit"]["After"]
    assert int(section["Service"]["Nice"]) > 0
