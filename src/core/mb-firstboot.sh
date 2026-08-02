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

oled "@SETUP" "MagicBridge" "First-time setup"

if [[ ! -f /opt/magicbridge/core/magicbridge.py ]]; then
    # NET-INSTALL image: MagicBridge isn't installed yet. Clone + install.
    # Everything (TLS cert, auth, USB serial) is generated fresh here, so this
    # path needs no secret reset - each unit is unique by construction.
    oled "@SETUP" "Installing MagicBridge" "(a few minutes)"
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
    # B4: install.sh leaves auth unset, so both backends would otherwise
    # bootstrap the shared public defaults (magicbridge/stealthbridge) on a
    # LAN-reachable panel. Give this net-install unit the SAME per-unit random
    # web password for both panels that the pre-installed path gets, and drop it
    # on the boot partition for the headless owner.
    CFG=/etc/magicbridge/config.json
    if command -v python3 >/dev/null; then
        NEWPW="$(python3 - "$CFG" <<'PY'
import json,sys,secrets,hashlib
p=sys.argv[1]
try: c=json.load(open(p))
except Exception: c={}
ab="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
pw="".join(secrets.choice(ab) for _ in range(12))
try:
    import bcrypt; h=bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
except Exception:
    h="sha256:"+hashlib.sha256(pw.encode()).hexdigest()
c["auth"]={"main_password_hash":h,"main_secret_key":secrets.token_hex(32),
           "password_hash":h,"secret_key":secrets.token_hex(32)}
json.dump(c,open(p,"w"),indent=2)
print(pw)
PY
)"
        chmod 600 "$CFG" 2>/dev/null || true
        if [ -n "$NEWPW" ]; then
            BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
            [ -d "$BOOT" ] && { printf 'MagicBridge web login\nPassword: %s\nChange it after first login; delete this file.\n' "$NEWPW" > "$BOOT/magicbridge-password.txt" 2>/dev/null; sync; }
            echo "[$(date)] net-install: per-unit web password set (on boot partition)"
        else
            echo "[$(date)] WARNING: net-install password generation failed - default may be active"
        fi
    fi
else
    # PRE-INSTALLED image (pi-gen / clone): the software is baked in, so the
    # ONLY thing to do is regenerate the per-unit secrets that must never be
    # shared across flashed units (SSH host keys, TLS cert, machine-id, auth,
    # USB serial, saved WiFi, Tailscale state).
    oled "@PERSONALIZE" "Creating identity" "keys / MAC / name"
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
    # FAIL-CLOSED (B2): secret-reset now returns non-zero if any CRITICAL per-unit
    # secret (SSH host keys, machine-id, TLS cert, hostname) failed to regenerate.
    # In that case DO NOT stamp .firstboot-done - leave it unset so first-boot
    # retries next boot rather than shipping a shared/baked identity. (The
    # MB_KEEP_WIFI guard above already prevents the retry from stranding an
    # already-provisioned unit by re-wiping its saved WiFi.)
    if ! /usr/local/bin/mb-secret-reset.sh; then
        oled "Setup FAILED" "identity not unique" "Retries next boot"
        echo "[$(date)] secret-reset reported CRITICAL failure - leaving flag unset to retry next boot"
        exit 1
    fi
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
    oled "@WIFI" "Join WiFi hotspot:" "MagicBridge-Setup"
    echo "[$(date)] no network - showing hotspot prompt"
fi
exit 0
