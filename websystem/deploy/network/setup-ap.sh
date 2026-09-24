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

if ! nmcli device status | grep -qE "^${IFACE}\b"; then
  log "ERROR: interface ${IFACE} not found. Available:"
  nmcli device status | sed 's/^/          /'
  exit 1
fi

# 2.4 GHz by default. 5 GHz is faster but its range is much shorter, and this
# is a pole-mounted unit in an open field where reach matters more than
# throughput — the dashboard defaults to ~0.4 Mbit/s polling for that reason.
if nmcli -g NAME connection show | grep -qx "$CON_NAME"; then
  log "updating existing connection ${CON_NAME}"
  nmcli connection modify "$CON_NAME" \
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
