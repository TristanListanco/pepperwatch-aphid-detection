#!/usr/bin/env bash
# Join a saved Wi-Fi network if one is in range; otherwise bring up the Pepper
# Watch access point. Runs at every boot (pepperwatch-ap.service), and
# `sudo systemctl restart pepperwatch-ap` re-decides without a reboot.
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

log() { printf '[pepperwatch-ap] %s\n' "$*"; }

if ! command -v nmcli >/dev/null 2>&1; then
  log "ERROR: nmcli not found. This script targets Raspberry Pi OS Bookworm or"
  log "       newer, where NetworkManager manages wireless."
  exit 1
fi

if ! nmcli device status | grep -E "^${IFACE}\b" >/dev/null; then
  # Not every board names its radio wlan0 (Orange Pi images often use wlan0,
  # but USB and SDIO radios elsewhere can be wlx... or wlp...). Use the first
  # Wi-Fi device NetworkManager knows about.
  FOUND="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "wifi" && !found { print $1; found = 1 }')"
  if [ -z "$FOUND" ]; then
    log "ERROR: no Wi-Fi interface found. Available:"
    nmcli device status | sed 's/^/          /'
    exit 1
  fi
  log "no ${IFACE}; using Wi-Fi interface ${FOUND}"
  IFACE="$FOUND"
fi

# -- a saved network, or the hotspot -------------------------------------------
# One radio, one job. If a saved Wi-Fi network is in range (added from the
# desktop Wi-Fi menu, `nmcli device wifi connect`, or Raspberry Pi Imager), join
# it and leave the hotspot off: the dashboard is then on that network. If none
# is, start the hotspot. This is the only place the hotspot is started, which
# is why its connection never autoconnects by itself. A network set to not
# connect automatically is skipped.
nmcli connection down "$CON_NAME" >/dev/null 2>&1 || true  # frees the radio to scan

saved=()
while IFS=: read -r name kind; do
  if [ "$kind" != "802-11-wireless" ] || [ "$name" = "$CON_NAME" ]; then continue; fi
  if [ "$(nmcli -g 802-11-wireless.mode connection show "$name")" = "ap" ]; then continue; fi
  if [ "$(nmcli -g connection.autoconnect connection show "$name")" = "no" ]; then continue; fi
  saved+=("$name")
done < <(nmcli -t -f NAME,TYPE connection show)

joined=""
current="$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null || true)"
if [ "${#saved[@]}" -gt 0 ]; then
  for name in "${saved[@]}"; do
    if [ "$name" = "$current" ]; then joined="$name"; fi
  done
fi

if [ -z "$joined" ] && [ "${#saved[@]}" -gt 0 ]; then
  visible=""
  for _ in 1 2 3; do  # the radio can still be settling right after boot
    visible="$(nmcli -t -f SSID device wifi list ifname "$IFACE" --rescan yes 2>/dev/null || true)"
    if [ -n "$visible" ]; then break; fi
    sleep 3
  done
  for name in "${saved[@]}"; do
    ssid="$(nmcli -g 802-11-wireless.ssid connection show "$name")"
    if printf '%s\n' "$visible" | grep -Fx -- "$ssid" >/dev/null &&
       nmcli --wait 30 connection up "$name" ifname "$IFACE" >/dev/null 2>&1; then
      joined="$name"
      break
    fi
  done
fi

if [ -n "$joined" ]; then
  joined_addr="$(nmcli -g IP4.ADDRESS device show "$IFACE" 2>/dev/null | sed -n 1p)"
  log "joined saved network '${joined}'; the hotspot stays off"
  log "dashboard at http://${joined_addr%/*}:8000/ (or http://$(hostname).local:8000/)"
  exit 0
fi
log "no saved Wi-Fi network in range: starting the hotspot"

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
    connection.autoconnect no
else
  log "creating connection ${CON_NAME} (SSID ${SSID} on ${IFACE})"
  nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$CON_NAME" \
    autoconnect no \
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
