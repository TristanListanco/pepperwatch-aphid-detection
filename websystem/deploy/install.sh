#!/usr/bin/env bash
# Install Pepper Watch on a Raspberry Pi, or another 64-bit ARM board.
#
#   sudo ./deploy/install.sh
#
# Targets Raspberry Pi OS 64-bit (Bookworm or Trixie) first, and also Debian 12+
# and Ubuntu 22.04+ based images for other boards: Armbian, Orange Pi OS,
# Banana Pi images. Idempotent: re-running it upgrades an existing install
# without losing the database or captured images.
#
# The one thing worth understanding before running this: the virtualenv is
# created with --system-site-packages. That is mandatory, because Picamera2 and
# its libcamera bindings come from apt and cannot be pip-installed. It also has
# a sharp edge -- a pip-installed numpy will shadow apt's, and libcamera's C
# bindings were compiled against apt's -- so numpy is deliberately left to the
# system, and `pepperwatch doctor` checks for the mismatch.
set -euo pipefail

APP_USER="${PW_USER:-pepperwatch}"
APP_DIR="${PW_DIR:-/opt/pepperwatch}"
DATA_DIR="${PW_DATA_DIR:-/var/lib/pepperwatch}"
CONFIG_DIR=/etc/pepperwatch
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# -- the board ---------------------------------------------------------------

# Python 3.10 is the floor: Ubuntu 22.04, which many Orange Pi and Banana Pi
# images are built on, ships it. config.py falls back to tomli there.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  note "ERROR: Python 3.10 or newer is required; found $(python3 --version 2>&1)."
  note "Use a Debian 12+ or Ubuntu 22.04+ based image."
  exit 1
fi

# Identify the board, so its tier profile and service memory limits apply.
# pepperwatch.board uses the standard library only, so it runs before anything
# is installed. Without this a 1 GB board inherits limits sized for a Pi 5,
# which sit above its physical RAM and therefore never engage.
eval "$(PYTHONPATH="$SOURCE_DIR" python3 -m pepperwatch.board --shell)"
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"

log "Detected ${BOARD_MODEL:-an unknown board} ($BOARD_FAMILY)"
note "cpu: $BOARD_CPU; ${MEM_MB} MB RAM"
note "tier: $BOARD_TIER (config/profiles/$BOARD_TIER.toml, if any)"

if [ "$BOARD_SUPPORTED" != "1" ]; then
  note "ERROR: $BOARD_REASON."
  note "Pepper Watch needs a 64-bit (aarch64) OS on a 64-bit ARM board."
  exit 1
fi

# -- packages --------------------------------------------------------------

# Installing onnxruntime and opencv on a 1 GB board can exhaust memory during
# pip's resolution step. zram gives compressed swap in RAM rather than on the
# SD card, so it costs no write endurance -- which matters because the card is
# the likeliest component to fail in this deployment.
if [ "$MEM_MB" -lt 2048 ]; then
  log "Low memory (${MEM_MB} MB): enabling zram"
  apt-get install -y --no-install-recommends zram-tools >/dev/null 2>&1 || true
  if [ -f /etc/default/zramswap ]; then
    sed -i 's/^#\?PERCENT=.*/PERCENT=50/' /etc/default/zramswap
    systemctl restart zramswap 2>/dev/null || true
    note "zram enabled at 50% of RAM"
  fi
fi

log "Installing system packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev python3-pip python3-numpy \
  mosquitto mosquitto-clients \
  fake-hwclock \
  i2c-tools \
  v4l-utils \
  qrencode \
  rsync

if [ "$BOARD_IS_RPI" = "1" ]; then
  # Raspberry Pi's own repository: the CSI camera stack. NetworkManager is
  # already the network manager on Bookworm and later.
  apt-get install -y --no-install-recommends \
    python3-picamera2 python3-libcamera rpicam-apps network-manager
  note "python3-picamera2 and python3-numpy come from apt on purpose:"
  note "pip cannot build working libcamera bindings, and a pip numpy would"
  note "shadow the one those bindings were compiled against."
else
  # Picamera2 and libcamera's Pi pipeline exist only for Raspberry Pi boards.
  # Elsewhere the camera is a USB (UVC) webcam, opened through V4L2.
  note "not a Raspberry Pi: the camera must be a USB webcam (CSI needs Picamera2)"
fi

# -- AI HAT+ ---------------------------------------------------------------

# Hailo's PCI vendor ID is 0x1e60. The Hailo-8 and Hailo-8L (AI HAT+ 26 and
# 13 TOPS) share device 0x2864 and the hailo-all stack; the Hailo-10H (AI HAT+
# 2) is 0x45c4 and needs hailo-h10-all. Both build a kernel driver with DKMS,
# so a first install needs a reboot before /dev/hailo0 exists.
HAILO_PACKAGE=""
HAILO_FOUND=0
REBOOT_NEEDED=0
for pci in /sys/bus/pci/devices/*; do
  [ "$(cat "$pci/vendor" 2>/dev/null)" = "0x1e60" ] || continue
  case "$(cat "$pci/device" 2>/dev/null)" in
    0x45c4) HAILO_PACKAGE=hailo-h10-all ;;
    *)      HAILO_PACKAGE=hailo-all ;;
  esac
  HAILO_FOUND=1
  LINK="$(cat "$pci/current_link_speed" 2>/dev/null || echo unknown)"
  log "Hailo accelerator detected at $(basename "$pci") (PCIe $LINK)"
  case "$LINK" in
    8.0*|16.0*) ;;
    *) note "The link is below Gen 3. For full speed add dtparam=pciex1_gen=3"
       note "to /boot/firmware/config.txt and reboot." ;;
  esac
  break
done

if [ "$HAILO_FOUND" = "1" ] && [ "$BOARD_IS_RPI" != "1" ]; then
  # hailo-all and hailo-h10-all come from Raspberry Pi's repository. Other
  # boards need HailoRT and its PCIe driver from the Hailo Developer Zone.
  note "Hailo packages are only packaged for Raspberry Pi OS. On this board, install"
  note "HailoRT and its PCIe driver from hailo.ai, then re-run this installer."
  HAILO_PACKAGE=""
fi

if [ -n "$HAILO_PACKAGE" ]; then
  if apt-get install -y dkms "$HAILO_PACKAGE"; then
    note "installed $HAILO_PACKAGE"
    [ -e /dev/hailo0 ] || REBOOT_NEEDED=1
  else
    note "WARNING: $HAILO_PACKAGE did not install; inference will run on the CPU."
  fi
elif [ "$HAILO_FOUND" != "1" ]; then
  note "no AI HAT+ on the PCIe bus; inference will run on the CPU"
fi

# -- the clock -------------------------------------------------------------

log "Timekeeping"
# Offline there is no NTP. A Pi without an RTC battery boots to a meaningless
# date, which would silently misdate every row in the Historical Detection Log
# and undermine the historical-tracking objective entirely.
systemctl enable fake-hwclock >/dev/null 2>&1 || true
if [ -e /dev/rtc0 ]; then
  note "hardware RTC present at /dev/rtc0"
  hwclock --hctosys 2>/dev/null || true
else
  note "NO HARDWARE CLOCK DETECTED."
  note "Fit a coin cell to the Pi 5's J5 connector. fake-hwclock will keep time"
  note "monotonic across reboots, but it cannot keep it correct."
fi

# -- user and directories --------------------------------------------------

log "Creating the service account"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi
# video for the camera, i2c for the INA219 power monitor, gpio for the fan.
for group in video i2c gpio; do
  getent group "$group" >/dev/null && usermod -aG "$group" "$APP_USER"
done

# sessions/ holds the per-session CSV files; hailort/ takes HailoRT's own log,
# which it would otherwise try to write into the read-only app directory.
install -d -o "$APP_USER" -g "$APP_USER" \
  "$DATA_DIR" "$DATA_DIR/media" "$DATA_DIR/db" "$DATA_DIR/sessions" "$DATA_DIR/hailort"
install -d -m 700 "$CONFIG_DIR"

# -- application -----------------------------------------------------------

log "Installing to $APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
# Preserve runtime state across upgrades.
rsync -a --delete \
  --exclude '.venv' --exclude 'var' --exclude '.git' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'config/local.toml' \
  "$SOURCE_DIR/" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Building the virtualenv"
if [ ! -d "$APP_DIR/.venv" ]; then
  sudo -u "$APP_USER" python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
# --no-deps on numpy would be fragile; instead the requirements file pins it
# loosely so pip is content with whatever apt already provides.
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
note "pip may print 'dependency conflicts' among the OS's own Python packages, which"
note "it sees through --system-site-packages. They do not affect Pepper Watch."

# Optional: real voltage and current for Chapter IV's power analysis.
if i2cdetect -y 1 2>/dev/null | grep -qi "4[0-5]"; then
  note "I2C device detected; installing the INA219 driver"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet \
    adafruit-circuitpython-ina219 || note "INA219 driver install failed (non-fatal)"
fi

# -- configuration ---------------------------------------------------------

log "Configuration"
if [ ! -f "$APP_DIR/config/local.toml" ]; then
  cat > "$APP_DIR/config/local.toml" <<TOML
# Local overrides. Not in version control.
# Everything here can also be set as a PW_* environment variable.

[site]
name = "Naawan"

[db]
path = "$DATA_DIR/db/pepperwatch.db"

[media]
root = "$DATA_DIR/media"

[web]
# Gates snapshot, pause and threshold changes. Reading never needs it.
# Leave empty to disable, but note that anyone within Wi-Fi range can then
# pause a running field trial.
control_pin = ""
secret_key = "$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
TOML
  chown "$APP_USER:$APP_USER" "$APP_DIR/config/local.toml"
  chmod 640 "$APP_DIR/config/local.toml"
  note "wrote config/local.toml with a generated secret key"
else
  note "config/local.toml exists; left untouched"
fi

if [ ! -f "$CONFIG_DIR/ap.psk" ]; then
  head -c 12 /dev/urandom | base64 | tr -d '/+=' | head -c 12 > "$CONFIG_DIR/ap.psk"
  chmod 600 "$CONFIG_DIR/ap.psk"
  note "generated a Wi-Fi passphrase: $(cat "$CONFIG_DIR/ap.psk")"
  note "WRITE THIS DOWN. Change it by editing $CONFIG_DIR/ap.psk."
fi

# -- services --------------------------------------------------------------

log "Installing services"
install -m 644 "$APP_DIR/deploy/mosquitto/pepperwatch.conf" /etc/mosquitto/conf.d/
install -d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$APP_DIR/deploy/network/dnsmasq-shared.d/00-pepperwatch-captive.conf" \
  /etc/NetworkManager/dnsmasq-shared.d/
install -m 644 "$APP_DIR"/deploy/systemd/pepperwatch*.service /etc/systemd/system/
install -m 644 "$APP_DIR"/deploy/systemd/pepperwatch*.timer /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/pepperwatch.target" /etc/systemd/system/

# Memory limits by RAM, whatever the board. The base units cap the worker at
# 2 GB and the dashboard at 1 GB; under 3 GB of RAM those caps sit above
# physical memory and never engage, so a smaller set replaces them. Old
# board-named drop-ins from earlier installs are cleared first.
rm -f /etc/systemd/system/pepperwatch-*.service.d/10-pi3.conf \
      /etc/systemd/system/pepperwatch-*.service.d/10-memory.conf
if [ -n "$BOARD_MEM_CLASS" ] && [ -d "$APP_DIR/deploy/systemd/$BOARD_MEM_CLASS" ]; then
  DROPIN_SRC="$APP_DIR/deploy/systemd/$BOARD_MEM_CLASS"
  note "applying $BOARD_MEM_CLASS memory limits for ${MEM_MB} MB of RAM"
  for unit_dir in "$DROPIN_SRC"/*.service.d; do
    [ -d "$unit_dir" ] || continue
    target="/etc/systemd/system/$(basename "$unit_dir")"
    install -d "$target"
    install -m 644 "$unit_dir"/*.conf "$target/"
    note "  $(basename "$unit_dir")"
  done
else
  note "${MEM_MB} MB of RAM: the default memory limits fit"
fi

# The access point needs a Wi-Fi radio that NetworkManager manages. Boards
# without one (Banana Pi M5, some Orange Pi 5 variants) or images that run
# another network stack serve the dashboard over Ethernet instead. The
# installer does not switch an image to NetworkManager itself: doing that
# over SSH can cut the very connection it is running on.
AP_FLAG="$CONFIG_DIR/ap.disabled"
if ! systemctl is-active --quiet NetworkManager; then
  : > "$AP_FLAG"
  AP_NOTE="NetworkManager is not running, so the PepperWatch access point is off."
elif ! nmcli -t -f TYPE device 2>/dev/null | grep -qx wifi; then
  : > "$AP_FLAG"
  AP_NOTE="No Wi-Fi radio found, so the PepperWatch access point is off."
else
  rm -f "$AP_FLAG"
  AP_NOTE=""
fi
[ -z "$AP_NOTE" ] || note "$AP_NOTE Use the board's Ethernet address instead."

systemctl daemon-reload
systemctl enable mosquitto >/dev/null
systemctl restart mosquitto
systemctl enable pepperwatch.target >/dev/null
systemctl enable pepperwatch-maintenance.timer >/dev/null

# -- verify ----------------------------------------------------------------

log "Running diagnostics"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pepperwatch.doctor --camera-probe || true

log "Installed"
AP_PSK="$(cat "$CONFIG_DIR/ap.psk" 2>/dev/null || true)"
if [ -z "$AP_NOTE" ]; then
  AP_SUMMARY="Wi-Fi network:  PepperWatch
    Password:       $AP_PSK
    Join it from a phone or tablet; the dashboard should open by itself.
    If it does not, browse to http://192.168.4.1:8000/"
else
  AP_SUMMARY="$AP_NOTE"
fi
cat <<EOF

    Start everything:   sudo systemctl start pepperwatch.target
    Watch the logs:     journalctl -fu pepperwatch-worker -u pepperwatch-web
    Re-check health:    sudo -u $APP_USER $APP_DIR/.venv/bin/python -m pepperwatch.doctor

    $AP_SUMMARY
    On the wired network: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000/

    Session CSV files (inference, power, fps) are written to
    $DATA_DIR/sessions and can be downloaded from the dashboard.

    Before any field trial, confirm:
      - the RTC battery is fitted, or timestamps will be wrong
      - models/pepperwatch.onnx exists (CPU), and models/pepperwatch_<chip>.hef
        if an AI HAT+ is fitted, or the stub detector runs instead
      - an agriculturist has signed off config/recommendations.json

EOF

# A Wi-Fi QR code: a phone camera joins the network without typing the
# password. Special characters are escaped as the WIFI: format requires.
if [ -z "$AP_NOTE" ] && [ -n "$AP_PSK" ] && command -v qrencode >/dev/null; then
  QR_PSK="$(printf '%s' "$AP_PSK" | sed 's/[\\;,:"]/\\&/g')"
  echo "    Scan with a phone camera to join PepperWatch:"
  qrencode -t ANSIUTF8 -m 2 "WIFI:T:WPA;S:PepperWatch;P:$QR_PSK;;"
fi

if [ "$REBOOT_NEEDED" -eq 1 ]; then
  log "Reboot required"
  note "The AI HAT+ driver was just installed and loads at boot: sudo reboot"
fi
