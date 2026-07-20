#!/bin/bash
# ============================================================
#  MagicBridge per-unit secret reset
#
#  For PRE-BAKED images only (pi-gen / SD-card clone). Regenerates every
#  secret that must be unique per physical unit, so a shared image never
#  ships shared credentials/keys (which would let units impersonate each
#  other and be cross-linked - a hard break of the anonymity model).
#
#  Run once by mb-firstboot.sh on the first boot of a flashed unit.
#  Safe to run again; each run just re-randomizes.
# ============================================================
set -uo pipefail
CFG=/etc/magicbridge/config.json
info(){ echo "[$(date)] secret-reset: $*"; }

# 1. SSH host keys — otherwise every unit shares the same host identity.
info "regenerating SSH host keys"
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
ssh-keygen -A 2>/dev/null || dpkg-reconfigure openssh-server 2>/dev/null || true

# 2. machine-id — a cross-linkable per-install identifier.
info "regenerating machine-id"
rm -f /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
systemd-machine-id-setup 2>/dev/null || true
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

# 2b. Hostname — a realistic per-unit name. A clone must not share the builder's
#     hostname, and must never advertise "magicbridge"/"raspberrypi" on the LAN
#     (broadcast via DHCP + mDNS). DESKTOP-XXXXXXX reads as an ordinary PC.
NEWHN="DESKTOP-$(tr -dc 'A-Z0-9' </dev/urandom 2>/dev/null | head -c 7 || true)"
info "regenerating hostname -> $NEWHN"
hostnamectl set-hostname "$NEWHN" 2>/dev/null || true
sed -i "/^127.0.1.1/d" /etc/hosts 2>/dev/null || true
echo "127.0.1.1  $NEWHN.local  $NEWHN" >> /etc/hosts 2>/dev/null || true

# 3. TLS cert/key — self-signed, must be unique per unit.
info "regenerating TLS certificate"
mkdir -p /etc/magicbridge/ssl
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout /etc/magicbridge/ssl/key.pem -out /etc/magicbridge/ssl/cert.pem \
    -subj "/CN=magicbridge.local" \
    -addext "subjectAltName=DNS:magicbridge.local,IP:127.0.0.1" 2>/dev/null || true
chmod 600 /etc/magicbridge/ssl/key.pem 2>/dev/null || true

# 4. config.json — drop auth (re-bootstraps to the default passwords) and every
#    secret/identity field, so no baked WiFi/Tailscale/DuckDNS/AI keys leak and
#    the USB serial regenerates from THIS Pi's MAC.
if [[ -f "$CFG" ]] && command -v python3 >/dev/null; then
    info "resetting config.json secrets"
    python3 - "$CFG" <<'PY' || true
import json,sys
p=sys.argv[1]
try: c=json.load(open(p))
except Exception: c={}
c.pop("auth",None)                 # -> re-bootstrap to default passwords on next start
c.pop("duckdns",None); c.pop("tailscale",None)
c.pop("mac_persist",None)
if isinstance(c.get("ai"),dict): c["ai"].pop("keys",None)
if isinstance(c.get("usb"),dict): c["usb"]["serial"]=""   # regenerated from MAC
json.dump(c,open(p,"w"),indent=2)
PY
    chmod 600 "$CFG" 2>/dev/null || true
fi

# 5. Saved WiFi — the unit must provision fresh, not join the builder's network.
#    MB_KEEP_WIFI=1 (set by mb-firstboot when it detects an already-provisioned
#    unit) skips this, so a stray re-run can never strand a working unit on its
#    setup hotspot. Arming an image always runs with no saved profiles present.
if [[ "${MB_KEEP_WIFI:-0}" == "1" ]]; then
    info "MB_KEEP_WIFI=1 - keeping saved WiFi (not stranding an in-service unit)"
else
    info "clearing saved WiFi connections"
    rm -f /etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null || true
    nmcli -t -f UUID,TYPE connection show 2>/dev/null | awk -F: '$2 ~ /wireless/ {print $1}' \
        | xargs -r -n1 nmcli connection delete 2>/dev/null || true
fi

# 6. Tailscale — don't inherit the builder's node identity.
info "clearing Tailscale state"
tailscale logout 2>/dev/null || true
systemctl stop tailscaled 2>/dev/null || true
rm -f /var/lib/tailscale/tailscaled.state 2>/dev/null || true

# 7. DuckDNS cron + MAC-persist unit (baked from the builder).
rm -f /etc/cron.d/mb-duckdns 2>/dev/null || true
systemctl disable mb-mac.service 2>/dev/null || true
rm -f /etc/systemd/system/mb-mac.service 2>/dev/null || true
# Drop the NM-layer MAC override too, so a fresh unit picks a NEW random
# vendor MAC on first boot (via the dashboard) instead of the builder's.
rm -f /etc/NetworkManager/conf.d/00-mb-macspoof.conf 2>/dev/null || true

# 8. Clear any provisioning/first-boot leftovers + RAM logs.
rm -f /etc/magicbridge/.provision-wifi /tmp/mb-ts-key 2>/dev/null || true
rm -f /var/log/magicbridge-ram/* 2>/dev/null || true

# 9. Restart ONLY the EARLY services whose per-unit secrets we just regenerated.
#    On a FRESH FLASH the image ships with NO ssh host keys and NO TLS cert (both
#    stripped when arming), so sshd + nginx - which start EARLY, before this
#    first-boot script - fail and stay down: the unit boots "up" (OLED shows its
#    IP) but with no SSH and no web UI. Restarting them here fixes that; neither
#    is ordered after mb-firstboot, so the restart returns immediately.
#
#    Do NOT restart magicbridge / stealth-dashboard here. They are ordered AFTER
#    mb-firstboot (this very script runs inside mb-firstboot, which declares
#    Before=magicbridge.service), so `systemctl restart` on them BLOCKS waiting
#    for mb-firstboot to finish - which is waiting on this line - a first-boot
#    DEADLOCK that hangs before WiFi provisioning (no hotspot, no OLED progress).
#    They have not started yet on a fresh flash, so they come up cleanly on their
#    own once mb-firstboot exits - no restart is needed or wanted.
info "restarting the early services (ssh, nginx) with the regenerated keys/cert"
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
systemctl restart nginx 2>/dev/null || true

info "done"
exit 0
