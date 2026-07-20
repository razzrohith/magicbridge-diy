#!/bin/bash
# ============================================================
#  MagicBridge first-boot setup (mb-firstboot.service)
#
#  Runs ONCE on the first boot of a freshly-flashed image, then disables
#  itself. Drives the OLED panel so a headless user knows what's happening:
#    1. "First setup, please wait..."   while it works
#    2. finalizes the install:
#         - net-install image (stock Pi OS): clone repo + run install.sh
#         - pre-installed image (pi-gen/clone): regenerate per-unit secrets
#    3. hands off to WiFi provisioning: "Join MagicBridge-Setup"
#
#  Idempotent + self-disabling via the .firstboot-done flag.
# ============================================================
set -uo pipefail

DONE_FLAG="/etc/magicbridge/.firstboot-done"
REPO_URL="https://github.com/razzrohith/magicbridge-diy"
BRANCH="main"
REPO_DIR="/opt/magicbridge-repo"
LOG="/var/log/magicbridge-firstboot.log"
exec >>"$LOG" 2>&1
echo "[$(date)] mb-firstboot starting"

mkdir -p /run/magicbridge /etc/magicbridge
oled() { printf '%s\n' "$@" > /run/magicbridge/oled-status 2>/dev/null || true; }
clear_oled() { rm -f /run/magicbridge/oled-status 2>/dev/null || true; }

# Already done? Nothing to do (self-disable defensively too).
if [[ -f "$DONE_FLAG" ]]; then
    systemctl disable mb-firstboot.service 2>/dev/null || true
    exit 0
fi

oled "MagicBridge" "First setup" "Please wait..."

if [[ ! -f /opt/magicbridge/core/magicbridge.py ]]; then
    # NET-INSTALL image: MagicBridge isn't installed yet. Clone + install.
    # Everything (TLS cert, auth, USB serial) is generated fresh here, so this
    # path needs no secret reset - each unit is unique by construction.
    oled "MagicBridge" "Installing..." "(a few minutes)"
    echo "[$(date)] not installed - running installer from $REPO_URL"
    if [[ ! -d "$REPO_DIR/.git" ]]; then
        git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$REPO_DIR" || {
            oled "Setup FAILED" "No internet?" "Retries next boot"
            echo "[$(date)] clone failed - leaving flag unset to retry next boot"
            exit 1
        }
    fi
    bash "$REPO_DIR/install.sh" || {
        oled "Setup FAILED" "see firstboot.log" "Retries next boot"
        echo "[$(date)] install.sh failed - will retry next boot"
        exit 1
    }
else
    # PRE-INSTALLED image (pi-gen / clone): the software is baked in, so the
    # ONLY thing to do is regenerate the per-unit secrets that must never be
    # shared across flashed units (SSH host keys, TLS cert, machine-id, auth,
    # USB serial, saved WiFi, Tailscale state).
    oled "MagicBridge" "Personalizing" "this device..."
    echo "[$(date)] pre-installed image - running secret reset"
    # SAFETY NET: secret-reset deletes saved WiFi (correct when arming an image).
    # If this ever re-runs on a unit the user ALREADY provisioned, that wipe would
    # drop it back to the setup hotspot - and if the marker below also failed to
    # write, it would loop forever (the exact failure the PiKVM sibling hit). A
    # freshly-armed image has no saved profiles, so this never fires there.
    if ls /etc/NetworkManager/system-connections/*.nmconnection >/dev/null 2>&1; then
        echo "[$(date)] WARNING: saved WiFi present on a first-boot run - keeping it (refusing to strand this unit)"
        export MB_KEEP_WIFI=1
    fi
    /usr/local/bin/mb-secret-reset.sh || echo "[$(date)] secret-reset had errors (non-fatal)"
fi

# Write the done-marker and PROVE it landed. A silent failure here (read-only or
# full rootfs) means first-boot re-runs every boot, re-wiping WiFi -> endless
# "join hotspot" loop. Verify + sync, and disable the unit either way.
mkdir -p "$(dirname "$DONE_FLAG")" 2>/dev/null
date > "$DONE_FLAG" 2>/dev/null
sync
if [ -s "$DONE_FLAG" ]; then
    echo "[$(date)] done-marker written: $DONE_FLAG"
else
    echo "[$(date)] ERROR: could not write $DONE_FLAG (read-only/full rootfs?) - disabling the unit anyway so first-boot cannot re-run"
fi
systemctl disable mb-firstboot.service 2>/dev/null || true
echo "[$(date)] first-boot finalize complete"

# Hand off. If there's no network yet, the provisioning hotspot comes up and
# mb-provision.sh sets its own OLED message ("Join MagicBridge-Setup"). If we
# already have a network, drop back to the normal status display.
sleep 6
if nmcli -t -f STATE general 2>/dev/null | grep -q '^connected$'; then
    clear_oled
    echo "[$(date)] network present - normal operation"
else
    oled "WiFi setup:" "Join hotspot" "MagicBridge-Setup" "(open network)"
    echo "[$(date)] no network - showing hotspot prompt"
fi
exit 0
