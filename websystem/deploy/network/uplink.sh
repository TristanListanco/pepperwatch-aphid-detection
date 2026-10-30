#!/usr/bin/env bash
# Join another Wi-Fi network while keeping the PepperWatch hotspot, or stop.
#
#   sudo pepperwatch-uplink enable "FarmWiFi"   # asks for the password
#   sudo pepperwatch-uplink disable              # hotspot only (the default)
#   sudo pepperwatch-uplink status
#
# Both run on the Pi's one radio: wlan0 joins the network, and the hotspot
# moves to a virtual interface (ap0) on the same channel. The radio can only be
# on one channel, so the hotspot follows the joined network's. Treat this as
# experimental: if that network drops or changes channel, the hotspot can drop
# with it, and `sudo systemctl restart pepperwatch-ap` brings it back.
#
# Hotspot clients still get no internet (every name resolves to the Pi), which
# is what keeps the captive portal working. The Pi itself gets internet: its
# clock syncs and update.sh works without Ethernet.
set -euo pipefail

# Run over the hotspot, the SSH session drops while the hotspot moves to ap0.
# Ignore the hangup so the change still finishes; reconnect and run `status`.
trap '' HUP

CON="${PW_UPLINK_CON_NAME:-pepperwatch-uplink}"
IFACE="${PW_UPLINK_IFACE:-wlan0}"

log() { printf '[pepperwatch-uplink] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }
usage() {
  sed -n '4,6p' "$0" | sed 's/^# *//'
  exit 2
}

[ "$(id -u)" -eq 0 ] || die "run with sudo"
command -v nmcli >/dev/null 2>&1 || die "nmcli not found; NetworkManager is required"

restart_ap() {
  if [ -e /etc/pepperwatch/ap.disabled ]; then
    log "the PepperWatch hotspot is disabled on this board; nothing to move"
  else
    systemctl restart pepperwatch-ap
  fi
}

case "${1:-}" in
  enable)
    ssid="${2:-}"
    [ -n "$ssid" ] || usage
    command -v iw >/dev/null 2>&1 || die "iw is not installed: sudo apt install iw"

    # From the environment for scripts, otherwise asked for and never echoed
    # or put on the command line of this script.
    if [ -n "${PW_UPLINK_PSK+set}" ]; then
      psk="$PW_UPLINK_PSK"
    else
      read -rsp "Password for ${ssid} (leave empty for an open network): " psk
      echo
    fi

    nmcli connection delete "$CON" >/dev/null 2>&1 || true
    args=(type wifi ifname "$IFACE" con-name "$CON" autoconnect yes ssid "$ssid"
          -- ipv4.method auto wifi.powersave 2)
    if [ -n "$psk" ]; then
      [ "${#psk}" -ge 8 ] || die "a WPA password has at least 8 characters"
      args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk")
    fi
    nmcli connection add "${args[@]}" >/dev/null
    log "saved ${ssid}; moving the hotspot onto its channel"
    restart_ap
    log "WARNING: anyone on ${ssid} can now open the dashboard and use its"
    log "         controls (no login). Only join a network you trust."
    "$0" status
    ;;

  disable)
    nmcli connection delete "$CON" >/dev/null 2>&1 || log "uplink was not enabled"
    log "back to hotspot only"
    restart_ap
    "$0" status
    ;;

  status)
    if nmcli -g NAME connection show | grep -x "$CON" >/dev/null; then
      ssid="$(nmcli -g 802-11-wireless.ssid connection show "$CON")"
      address="$(nmcli -g IP4.ADDRESS device show "$IFACE" 2>/dev/null | sed -n 1p)"
      channel="$(iw dev "$IFACE" info 2>/dev/null | awk '/channel/ && !seen {print $2; seen = 1}')"
      log "uplink: ON, ${IFACE} -> ${ssid} (${address:-not connected}, channel ${channel:-none})"
    else
      log "uplink: off (hotspot only, the default)"
    fi
    nmcli -f GENERAL.DEVICES,GENERAL.STATE connection show pepperwatch-ap 2>/dev/null \
      | sed 's/^/                     /' || log "hotspot connection not found"
    ;;

  *)
    usage
    ;;
esac
