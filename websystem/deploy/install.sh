#!/usr/bin/env bash
# Install Pepper Watch on a Raspberry Pi 5.
#
#   sudo ./deploy/install.sh
#
# Targets Raspberry Pi OS 64-bit, Bookworm or Trixie. Idempotent: re-running it
# upgrades an existing install without losing the database or captured images.
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

if ! grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  note "WARNING: this does not look like a Raspberry Pi."
  read -rp "    Continue anyway? [y/N] " reply
  [ "$reply" = "y" ] || exit 1
fi

# Identify the board so its tuning profile and unit drop-ins can be applied.
# Without this a Pi 3 inherits the Pi 5's memory ceilings, which sit above its
# physical RAM and therefore never engage.
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
case "$MODEL" in
  *"Pi 5"*)      BOARD=pi5 ;;
  *"Pi 4"*|*"Compute Module 4"*) BOARD=pi4 ;;
  *"Pi 3"*)      BOARD=pi3 ;;
  *"Zero 2"*)    BOARD=pizero2 ;;
  *)             BOARD=unknown ;;
esac

log "Detected $MODEL"
note "board: $BOARD, ${MEM_MB} MB RAM"

if [ "$(uname -m)" != "aarch64" ]; then
  note "ERROR: this is a $(uname -m) userland, not aarch64."
  note "ONNX Runtime publishes no 32-bit ARM wheels, so inference cannot run."
  note "Reflash with Raspberry Pi OS Lite (64-bit)."
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
  python3 python3-venv python3-dev python3-pip \
  python3-picamera2 python3-libcamera python3-numpy \
  rpicam-apps \
  mosquitto mosquitto-clients \
  network-manager \
  fake-hwclock \
  i2c-tools \
  rsync

note "python3-picamera2 and python3-numpy come from apt on purpose:"
note "pip cannot build working libcamera bindings, and a pip numpy would"
note "shadow the one those bindings were compiled against."

# -- AI HAT+ ---------------------------------------------------------------

# Hailo's PCI vendor ID is 0x1e60. The Hailo-8 and Hailo-8L (AI HAT+ 26 and
# 13 TOPS) share device 0x2864 and the hailo-all stack; the Hailo-10H (AI HAT+
# 2) is 0x45c4 and needs hailo-h10-all. Both build a kernel driver with DKMS,
# so a first install needs a reboot before /dev/hailo0 exists.
HAILO_PACKAGE=""
REBOOT_NEEDED=0
for pci in /sys/bus/pci/devices/*; do
  [ "$(cat "$pci/vendor" 2>/dev/null)" = "0x1e60" ] || continue
  case "$(cat "$pci/device" 2>/dev/null)" in
    0x45c4) HAILO_PACKAGE=hailo-h10-all ;;
    *)      HAILO_PACKAGE=hailo-all ;;
  esac
  LINK="$(cat "$pci/current_link_speed" 2>/dev/null || echo unknown)"
  log "AI HAT+ detected at $(basename "$pci") (PCIe $LINK)"
  case "$LINK" in
    8.0*|16.0*) ;;
    *) note "The link is below Gen 3. For full speed add dtparam=pciex1_gen=3"
       note "to /boot/firmware/config.txt and reboot." ;;
  esac
  break
done

if [ -n "$HAILO_PACKAGE" ]; then
  if apt-get install -y dkms "$HAILO_PACKAGE"; then
    note "installed $HAILO_PACKAGE"
    [ -e /dev/hailo0 ] || REBOOT_NEEDED=1
  else
    note "WARNING: $HAILO_PACKAGE did not install; inference will run on the CPU."
  fi
else
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

# Board-specific drop-ins. The base units are sized for a Pi 5; these override
# the memory ceilings, thread counts and CPU weights for smaller boards.
DROPIN_SRC="$APP_DIR/deploy/systemd/$BOARD"
if [ -d "$DROPIN_SRC" ]; then
  note "applying $BOARD service overrides"
  for unit_dir in "$DROPIN_SRC"/*.service.d; do
    [ -d "$unit_dir" ] || continue
    target="/etc/systemd/system/$(basename "$unit_dir")"
    install -d "$target"
    install -m 644 "$unit_dir"/*.conf "$target/"
    note "  $(basename "$unit_dir")"
  done
else
  note "no service overrides for $BOARD; using the Pi 5 defaults"
  if [ "$MEM_MB" -lt 4096 ]; then
    note "  WARNING: ${MEM_MB} MB RAM with Pi 5 memory ceilings."
    note "  The limits will not engage and the OOM killer may fire instead."
    note "  Check with: python -m pepperwatch.doctor"
  fi
fi

systemctl daemon-reload
systemctl enable mosquitto >/dev/null
systemctl restart mosquitto
systemctl enable pepperwatch.target >/dev/null
systemctl enable pepperwatch-maintenance.timer >/dev/null

# -- verify ----------------------------------------------------------------

log "Running diagnostics"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pepperwatch.doctor --camera-probe || true

log "Installed"
cat <<EOF

    Start everything:   sudo systemctl start pepperwatch.target
    Watch the logs:     journalctl -fu pepperwatch-worker -u pepperwatch-web
    Re-check health:    sudo -u $APP_USER $APP_DIR/.venv/bin/python -m pepperwatch.doctor

    The SSID is PepperWatch and the passphrase is in $CONFIG_DIR/ap.psk.
    Joining it should open the dashboard automatically; if it does not,
    browse to http://192.168.4.1:8000/

    Session CSV files (inference, power, fps) are written to
    $DATA_DIR/sessions and can be downloaded from the dashboard.

    Before any field trial, confirm:
      - the RTC battery is fitted, or timestamps will be wrong
      - models/pepperwatch.onnx exists (CPU), and models/pepperwatch_<chip>.hef
        if an AI HAT+ is fitted, or the stub detector runs instead
      - an agriculturist has signed off config/recommendations.json

EOF

if [ "$REBOOT_NEEDED" -eq 1 ]; then
  log "Reboot required"
  note "The AI HAT+ driver was just installed and loads at boot: sudo reboot"
fi
