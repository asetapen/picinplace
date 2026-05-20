#!/usr/bin/env bash
# ap-mode.sh — toggle the PicInPlace Wi-Fi access point on/off.
#
# Usage:
#   ./ap-mode.sh --enable    # prompt for an AP password, create + bring up the hotspot
#   ./ap-mode.sh --disable   # tear it down and reconnect to the previous Wi-Fi
#
# Requires NetworkManager (`nmcli`), which is the default on Raspberry Pi OS
# Bookworm and newer. While the AP is up the Pi has no Wi-Fi uplink — see
# README.md "Running the Pi as a Wi-Fi access point" for context.

set -euo pipefail

AP_NAME="picinplace-ap"
SSID="PicInPlace"
IFACE="wlan0"

usage() {
    cat >&2 <<USAGE
Usage: $0 --enable | --disable

  --enable    Prompt for a password and bring up the PicInPlace Wi-Fi hotspot.
              Existing $AP_NAME connection is replaced.
  --disable   Take the hotspot down and reconnect to the most recently used
              saved Wi-Fi network.
USAGE
    exit 2
}

require_nmcli() {
    if ! command -v nmcli >/dev/null 2>&1; then
        echo "Error: nmcli not found. NetworkManager is required (Pi OS Bookworm+)." >&2
        exit 1
    fi
}

ap_exists() {
    sudo nmcli -t -f NAME connection show | grep -Fxq "$AP_NAME"
}

enable_ap() {
    require_nmcli

    # Cache sudo creds upfront so the password prompt below isn't sandwiched
    # between sudo prompts.
    sudo -v

    local pass pass2
    while :; do
        read -r -s -p "AP password (min 8 chars, hidden): " pass
        echo
        if [ "${#pass}" -lt 8 ]; then
            echo "Password must be at least 8 characters." >&2
            continue
        fi
        read -r -s -p "Confirm password: " pass2
        echo
        if [ "$pass" != "$pass2" ]; then
            echo "Passwords don't match — try again." >&2
            continue
        fi
        break
    done

    if ap_exists; then
        echo "Replacing existing $AP_NAME connection..."
        sudo nmcli connection delete "$AP_NAME" >/dev/null
    fi

    echo "Creating hotspot $SSID on $IFACE..."
    sudo nmcli connection add type wifi ifname "$IFACE" con-name "$AP_NAME" \
        autoconnect yes ssid "$SSID" >/dev/null
    sudo nmcli connection modify "$AP_NAME" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        ipv4.method shared \
        ipv6.method disabled \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$pass" \
        connection.autoconnect-priority 100

    echo "Bringing $AP_NAME up..."
    sudo nmcli connection up "$AP_NAME" >/dev/null

    cat <<INFO

Hotspot is up.
  SSID:    $SSID
  URL:     http://10.42.0.1:8000   (from devices joined to that SSID)

To disable and reconnect to your usual Wi-Fi:
  $0 --disable
INFO
}

disable_ap() {
    require_nmcli
    sudo -v

    if ap_exists; then
        echo "Bringing $AP_NAME down..."
        sudo nmcli connection down "$AP_NAME" >/dev/null 2>&1 || true
        sudo nmcli connection modify "$AP_NAME" connection.autoconnect no
    else
        echo "$AP_NAME isn't configured — nothing to take down."
    fi

    # Pick the most recently activated Wi-Fi connection that isn't our AP.
    # nmcli -t output is colon-separated; TIMESTAMP is unix epoch (0 if never used).
    local prev
    prev=$(nmcli -t -f NAME,TYPE,TIMESTAMP connection show \
        | awk -F: -v ap="$AP_NAME" '$2 == "802-11-wireless" && $1 != ap { print $3 ":" $1 }' \
        | sort -t: -k1 -n -r \
        | head -n 1 \
        | cut -d: -f2-)

    if [ -z "$prev" ]; then
        echo "No previous Wi-Fi connection found." >&2
        echo "Connect manually with:" >&2
        echo "  sudo nmcli device wifi connect '<SSID>' password '<password>'" >&2
        exit 1
    fi

    echo "Reconnecting to '$prev'..."
    sudo nmcli connection up "$prev" >/dev/null
    echo "Done."
}

case "${1:-}" in
    --enable)  enable_ap ;;
    --disable) disable_ap ;;
    -h|--help) usage ;;
    *)         usage ;;
esac
