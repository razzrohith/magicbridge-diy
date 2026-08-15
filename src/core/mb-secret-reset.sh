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
# FAIL-CLOSED (B2): track whether every CRITICAL per-unit secret actually
# regenerated. mb-firstboot.sh keys the .firstboot-done marker on our exit code,
# so a transient failure here must surface as non-zero instead of silently
# shipping a shared/baked secret. Cosmetic steps (WiFi/Tailscale/log cleanup)
# do NOT flip this.
HARD_FAIL=0
fail(){ HARD_FAIL=1; info "CRITICAL FAILURE: $*"; }

# 1. SSH host keys — otherwise every unit shares the same host identity.
info "regenerating SSH host keys"
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
ssh-keygen -A 2>/dev/null || dpkg-reconfigure openssh-server 2>/dev/null || true
ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || fail "no SSH host key regenerated"

# 1b. USER SSH material (B1) — if the golden unit ever had key-based login or
#     git-over-SSH set up, /root/.ssh and /home/*/.ssh (id_*, authorized_keys,
#     known_hosts) would ship IDENTICALLY in every clone: a shared private key /
#     authorized_keys is a master key into ALL units, and known_hosts cross-links
#     them. Wipe them; they are never needed on a flashed unit (password login).
info "removing any inherited user SSH keys/known_hosts"
rm -rf /root/.ssh 2>/dev/null || true
for h in /home/*; do [ -d "$h/.ssh" ] && rm -rf "$h/.ssh" 2>/dev/null || true; done

# 2. machine-id — a cross-linkable per-install identifier.
info "regenerating machine-id"
rm -f /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
systemd-machine-id-setup 2>/dev/null || true
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
[ -s /etc/machine-id ] || fail "machine-id not regenerated"

# 2b. Hostname — a realistic per-unit name. A clone must not share the builder's
#     hostname, and must never advertise "magicbridge"/"raspberrypi" on the LAN
#     (broadcast via DHCP + mDNS). DESKTOP-XXXXXXX reads as an ordinary PC.
NEWHN="DESKTOP-$(tr -dc 'A-Z0-9' </dev/urandom 2>/dev/null | head -c 7 || true)"
[ "$NEWHN" = "DESKTOP-" ] && NEWHN="DESKTOP-$(date +%s | tail -c 8)"   # urandom empty fallback
info "regenerating hostname -> $NEWHN"
hostnamectl set-hostname "$NEWHN" 2>/dev/null || true
sed -i "/^127.0.1.1/d" /etc/hosts 2>/dev/null || true
echo "127.0.1.1  $NEWHN.local  $NEWHN" >> /etc/hosts 2>/dev/null || true
CURHN="$(hostname 2>/dev/null || true)"
{ [ -n "$CURHN" ] && [ "$CURHN" != "magicbridge" ] && [ "$CURHN" != "raspberrypi" ] && [ "$CURHN" != "localhost" ]; } \
    || fail "hostname not set to a per-unit value (got '$CURHN')"

# 3. TLS cert/key — self-signed, must be unique per unit. CN/SAN use THIS unit's
#    hostname (B5): a fleet-wide hardcoded CN=magicbridge.local is a branded,
#    pre-auth TLS beacon that outs every clone as MagicBridge and makes every
#    cert identical. magicbridge.local stays as a SAN so browsing to it still
#    validates.
info "regenerating TLS certificate (CN=$CURHN)"
mkdir -p /etc/magicbridge/ssl
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout /etc/magicbridge/ssl/key.pem -out /etc/magicbridge/ssl/cert.pem \
    -subj "/CN=${CURHN:-localhost}" \
    -addext "subjectAltName=DNS:${CURHN:-localhost}.local,DNS:magicbridge.local,IP:127.0.0.1" 2>/dev/null || true
chmod 600 /etc/magicbridge/ssl/key.pem 2>/dev/null || true
[ -s /etc/magicbridge/ssl/cert.pem ] && [ -s /etc/magicbridge/ssl/key.pem ] || fail "TLS cert/key not regenerated"

# 4. config.json — drop every baked secret/identity field, and (B6) set a UNIQUE
#    RANDOM web password per unit instead of re-bootstrapping to the shared public
#    default "magicbridge" (which anyone on the LAN could use on a fresh clone
#    before the owner logs in). The plaintext is written to the FAT boot partition
#    (physical-access-only, readable by pulling the card) so a headless owner can
#    retrieve it; mb-firstboot points the OLED at it. Change it after first login.
PW_OUT=0
if [[ -f "$CFG" ]] && command -v python3 >/dev/null; then
    info "resetting config.json secrets + setting a per-unit random web password"
    python3 - "$CFG" <<'PY'
import json,sys,secrets,hashlib,string,os
p=sys.argv[1]
try: c=json.load(open(p))
except Exception: c={}
# unambiguous charset (no 0/O/1/l/I) so the owner can read it off the OLED/card
alphabet="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
pw="".join(secrets.choice(alphabet) for _ in range(12))
def _mkhash(s):
    try:
        import bcrypt
        return bcrypt.hashpw(s.encode(), bcrypt.gensalt()).decode()
    except Exception:
        return "sha256:"+hashlib.sha256(s.encode()).hexdigest()
h=_mkhash(pw)
# Fresh auth for BOTH panels with the SAME per-unit random password. CRITICAL:
# the stealth admin dashboard (its auth.password_hash/secret_key) is reachable
# on the LAN via nginx's location /stealth/ on :443 - the :7777 firewall drop
# only blocks DIRECT access, not the proxy. If we only reset main and let the
# stealth panel re-bootstrap, every clone would ship the public default
# "stealthbridge" on a LAN-reachable admin panel that can rotate USB/MAC/EDID
# and reveal WiFi PSKs. Setting both to the random pw closes that.
c["auth"]={"main_password_hash":h,"main_secret_key":secrets.token_hex(32),
           "password_hash":h,"secret_key":secrets.token_hex(32)}
c.pop("duckdns",None); c.pop("tailscale",None)
c.pop("mac_persist",None)
if isinstance(c.get("ai"),dict): c["ai"].pop("keys",None)
if isinstance(c.get("usb"),dict): c["usb"]["serial"]=""   # regenerated from MAC
json.dump(c,open(p,"w"),indent=2)
# Stash the plaintext for mb-firstboot to place on the boot partition.
try: open("/run/magicbridge/.new-web-password","w").write(pw)
except Exception: pass
print("MB_NEW_PASSWORD_SET")
PY
    if [[ $? -eq 0 ]]; then PW_OUT=1; else fail "config secret reset / password generation failed"; fi
    chmod 600 "$CFG" 2>/dev/null || true
fi
# FAIL-CLOSED (B4): if the password block was SKIPPED entirely (config.json
# momentarily absent, or python3 broken) PW_OUT stays 0 and nothing above fired
# fail() - so without this the unit would stamp first-boot done and re-bootstrap
# the shared public defaults (magicbridge/stealthbridge) on a LAN-reachable panel.
if [[ "$PW_OUT" != "1" ]]; then fail "web/admin password was not regenerated (config/python missing)"; fi
# Surface the new password where a headless owner can read it: the FAT boot
# partition (pull the card, open in any OS). Root-only perms on the runtime copy.
if [[ "$PW_OUT" == "1" && -s /run/magicbridge/.new-web-password ]]; then
    NEWPW="$(cat /run/magicbridge/.new-web-password 2>/dev/null)"
    BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
    if [ -d "$BOOT" ]; then
        { echo "MagicBridge web login"; echo "URL : https://${CURHN:-this-device}.local/  (or the IP on the OLED)";
          echo "User: (none)"; echo "Password: ${NEWPW}"; echo;
          echo "Change it after first login. Delete this file once you have it."; echo;
          echo "NOTE: your browser will warn the connection is not private. That";
          echo "is expected on a private device with a self-signed certificate:";
          echo "choose Advanced, then Proceed."; } \
          > "$BOOT/magicbridge-password.txt" 2>/dev/null || true
        sync
        info "per-unit web password written to $BOOT/magicbridge-password.txt"
    fi
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
    # NetworkManager runtime state also records the builder's WiFi environment:
    # seen-bssids/timestamps = which access points this unit has seen (the
    # builder's LOCATION), and internal DHCP leases cross-link clones. Purge them.
    rm -rf /var/lib/NetworkManager/seen-bssids \
           /var/lib/NetworkManager/timestamps \
           /var/lib/NetworkManager/*.lease \
           /var/lib/NetworkManager/*-internal.lease \
           /var/lib/NetworkManager/dhclient*.lease 2>/dev/null || true
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

# FAIL-CLOSED (B2): exit non-zero if any CRITICAL per-unit secret failed to
# regenerate, so mb-firstboot does NOT stamp .firstboot-done and the unit
# retries next boot instead of silently shipping a shared/baked identity.
if [[ "$HARD_FAIL" -ne 0 ]]; then
    info "done WITH CRITICAL FAILURES - signaling first-boot to retry"
    exit 1
fi
info "done - all per-unit secrets regenerated"
exit 0
