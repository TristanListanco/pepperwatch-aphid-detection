#!/usr/bin/env bash
# Bring up the Pepper Watch access point.
#
# Raspberry Pi OS Bookworm and Trixie manage wireless through NetworkManager,
# which means the hostapd + dnsmasq recipe in most Raspberry Pi AP tutorials is
# the wrong approach on current images: NetworkManager owns wlan0 and will
# fight anything else that tries to configure it.
#
# `ipv4.method shared` is the key. It makes NetworkManager run its own dnsmasq
# instance for DHCP and DNS on the interface, which is what we then configure
# for the captive portal via /etc/NetworkManager/dnsmasq-shared.d/.
#
# Idempotent: safe to re-run, and safe as a systemd oneshot on every boot.
set -euo pipefail
# Under pipefail, never end a pipeline with a reader that quits early (grep -q,
# head, awk exit): the writer dies of SIGPIPE and the whole test reads false.

CON_NAME="${PW_AP_CON_NAME:-pepperwatch-ap}"
SSID="${PW_AP_SSID:-PepperWatch}"
PSK="${PW_AP_PSK:-}"
IFACE="${PW_AP_IFACE:-wlan0}"
ADDRESS="${PW_AP_ADDRESS:-192.168.4.1/24}"
BAND="${PW_AP_BAND:-bg}"
CHANNEL="${PW_AP_CHANNEL:-6}"
UPLINK_CON="${PW_UPLINK_CON_NAME:-pepperwatch-uplink}"
AP_VIRTUAL="${PW_AP_VIRTUAL_IFACE:-ap0}"

log() { printf '[pepperwatch-ap] %s\n' "$*"; }

if ! command -v nmcli >/dev/null 2>&1; then
  log "ERROR: nmcli not found. This script targets Raspberry Pi OS Bookworm or"
  log "       newer, where NetworkManager manages wireless."
  exit 1
fi

# Read the passphrase from a root-only file rather than baking it into a unit
# file or the process table, where `ps` would expose it to any local user.
if [ -z "$PSK" ] && [ -r /etc/pepperwatch/ap.psk ]; then
  PSK="$(tr -d '\n' < /etc/pepperwatch/ap.psk)"
fi

if [ -z "$PSK" ]; then
  log "ERROR: no passphrase. Write one to /etc/pepperwatch/ap.psk (chmod 600)"
  log "       or set PW_AP_PSK. An open access point would let anyone in range"
  log "       pause the detector or rewrite its configuration."
  exit 1
fi

if [ "${#PSK}" -lt 8 ]; then
  log "ERROR: WPA2 requires a passphrase of at least 8 characters."
  exit 1
fi

# -- uplink mode (off by default; see uplink.sh) -----------------------------
# With a saved uplink connection, the radio's own interface joins that network
# and the hotspot runs on a virtual interface beside it. One radio means one
# channel, so the hotspot takes whatever channel the joined network uses.
if nmcli -g NAME connection show | grep -x "$UPLINK_CON" >/dev/null; then
  command -v iw >/dev/null 2>&1 || { log "ERROR: uplink mode needs iw: sudo apt install iw"; exit 1; }
  STA="$(nmcli -g connection.interface-name connection show "$UPLINK_CON")"
  STA="${STA:-$IFACE}"

  if nmcli -g GENERAL.STATE connection show "$UPLINK_CON" | grep activated >/dev/null; then
    log "uplink already connected on ${STA}"
  elif nmcli --wait 20 connection up "$UPLINK_CON" >/dev/null 2>&1; then
    log "joined the uplink network on ${STA}"
  else
    log "WARNING: could not join the uplink network; starting the hotspot anyway"
  fi

  if ! iw dev "$AP_VIRTUAL" info >/dev/null 2>&1; then
    log "creating virtual interface ${AP_VIRTUAL} on ${STA}"
    iw dev "$STA" interface add "$AP_VIRTUAL" type __ap
    # The radio refuses two interfaces with one MAC: use the same address with
    # the locally-administered bit set.
    mac="$(cat "/sys/class/net/${STA}/address")"
    first=$(( (16#${mac%%:*} | 2) & 254 ))
    ip link set "$AP_VIRTUAL" address "$(printf '%02x' "$first"):${mac#*:}"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      nmcli device status | grep -E "^${AP_VIRTUAL}\b" >/dev/null && break
      sleep 0.5
    done
  fi

  # "channel 36 (5180 MHz), ..." -> channel 36, band a. Not connected: keep
  # the defaults, and re-run (systemctl restart pepperwatch-ap) once it is.
  link="$(iw dev "$STA" info | awk '/channel/ && !seen {print $2, $3; seen = 1}')"
  if [ -n "$link" ]; then
    CHANNEL="${link%% *}"
    freq="${link#*(}"
    if [ "$freq" -lt 3000 ]; then BAND="bg"; else BAND="a"; fi
    if [ "$CHANNEL" -ge 52 ] && [ "$CHANNEL" -le 144 ]; then
      log "WARNING: the uplink is on 5 GHz DFS channel ${CHANNEL}; the hotspot"
      log "         cannot start there. Join a 2.4 GHz network instead."
    fi
  fi
  log "hotspot follows the uplink: channel ${CHANNEL} (${BAND})"
  IFACE="$AP_VIRTUAL"
elif command -v iw >/dev/null 2>&1 && iw dev "$AP_VIRTUAL" info >/dev/null 2>&1; then
  # Back to hotspot only: remove the virtual interface uplink mode left behind.
  nmcli connection down "$CON_NAME" >/dev/null 2>&1 || true
  iw dev "$AP_VIRTUAL" del
  log "uplink off; removed ${AP_VIRTUAL}"
fi

if ! nmcli device status | grep -E "^${IFACE}\b" >/dev/null; then
  # Not every board names its radio wlan0 (Orange Pi images often use wlan0,
  # but USB and SDIO radios elsewhere can be wlx... or wlp...). Use the first
  # Wi-Fi device NetworkManager knows about.
  FOUND="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "wifi" { print $1; exit }')"
  if [ -z "$FOUND" ]; then
    log "ERROR: no Wi-Fi interface found. Available:"
    nmcli device status | sed 's/^/          /'
    exit 1
  fi
  log "no ${IFACE}; using Wi-Fi interface ${FOUND}"
  IFACE="$FOUND"
fi

# 2.4 GHz by default. 5 GHz is faster but its range is much shorter, and this
# is a pole-mounted unit in an open field where reach matters more than
# throughput — the dashboard defaults to ~0.4 Mbit/s polling for that reason.
if nmcli -g NAME connection show | grep -x "$CON_NAME" >/dev/null; then
  log "updating existing connection ${CON_NAME}"
  nmcli connection modify "$CON_NAME" \
    connection.interface-name "$IFACE" \
    wifi.ssid "$SSID" \
    wifi.band "$BAND" \
    wifi.channel "$CHANNEL" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK" \
    ipv4.method shared \
    ipv4.addresses "$ADDRESS" \
    connection.autoconnect yes
else
  log "creating connection ${CON_NAME} (SSID ${SSID} on ${IFACE})"
  nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$CON_NAME" \
    autoconnect yes \
    ssid "$SSID" \
    -- \
    wifi.mode ap \
    wifi.band "$BAND" \
    wifi.channel "$CHANNEL" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK" \
    ipv4.method shared \
    ipv4.addresses "$ADDRESS"
fi

log "bringing up ${CON_NAME}"
nmcli connection up "$CON_NAME"

log "access point is up:"
nmcli -f GENERAL.STATE,IP4.ADDRESS connection show "$CON_NAME" | sed 's/^/          /'
log "SSID ${SSID}, dashboard at http://${ADDRESS%/*}:8000/"
if [ "$IFACE" = "$AP_VIRTUAL" ]; then
  UPLINK_ADDR="$(nmcli -g IP4.ADDRESS device show "$STA" 2>/dev/null | sed -n 1p)"
  log "uplink: also reachable at http://${UPLINK_ADDR%/*}:8000/ on the joined network"
fi
