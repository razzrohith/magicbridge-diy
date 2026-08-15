#!/bin/bash
# ============================================================
#  MagicBridge WiFi Provisioning AP
#
#  Runs as a systemd service (mb-provision.service) on every boot.
#  Starts a hostapd access point "Setup-XXXX" (per-unit, see AP_SSID) whenever the Pi
#  has no working network connection (first-time setup, or later if
#  it's moved somewhere its saved WiFi isn't in range), serves a
#  captive-portal setup page, then hands off to NetworkManager and
#  exits. Not a one-time thing: it re-checks connectivity on every
#  boot rather than gating itself off permanently after first success,
#  so moving this to a new location just means going through the same
#  setup-hotspot flow again instead of losing wireless access to it.
#
#  Credentials: SSID "Setup-XXXX"  (no password; exact name shown on the OLED)
#  Portal: http://192.168.73.1/ (opened by captive detection)
# ============================================================
set -e

FLAG_FILE="/etc/magicbridge/.provisioned"   # informational timestamp only, doesn't gate anything
WIFI_FILE="/etc/magicbridge/.provision-wifi"
# RAM-only, like mb-hdmi-init.log. This log records the WiFi SSIDs the unit set
# up on, which is location-identifying, and the anonymity model says nothing
# usage/location-identifying may persist on the SD card. The tmpfs is mounted
# via fstab at local-fs.target, long before this service (After=NetworkManager)
# runs, so it is always available here. Falls back to the card path only if the
# tmpfs somehow is not mounted, so a diagnostic still exists in that rare case.
if findmnt -rno TARGET /var/log/magicbridge-ram >/dev/null 2>&1; then
    LOG="/var/log/magicbridge-ram/mb-provision.log"
else
    LOG="/var/log/magicbridge-provision.log"
fi
# Generic, per-unit setup SSID (B3): the old "MagicBridge-Setup" beaconed the
# product name over the air (on the real Pi MAC), a branded leak bypassing every
# USB/HDMI/LAN precaution. A stable "Setup-XXXX" suffix from this unit's
# machine-id reads like an ordinary router setup AP; the exact name is shown on
# the OLED so the owner still knows which network to join.
_ap_sfx="$(cat /etc/machine-id 2>/dev/null | tr -dc 'a-f0-9' | tail -c 4)"
[ -n "$_ap_sfx" ] || _ap_sfx="$(hostname 2>/dev/null | tr -dc 'A-Za-z0-9' | tail -c 4)"
[ -n "$_ap_sfx" ] || _ap_sfx="0000"
AP_SSID="Setup-${_ap_sfx}"
AP_IP="192.168.73.1"
AP_IFACE="wlan0"
PORTAL_PORT=80
PORTAL_SCRIPT="/opt/magicbridge/provision/mb-setup-ui.py"
TS_KEY_TMP="/tmp/mb-ts-key"

exec >> "$LOG" 2>&1
echo "[$(date)] mb-provision.sh starting"

# OLED guidance for a headless user (picked up by oled.py's status override).
mkdir -p /run/magicbridge 2>/dev/null || true
oled()       { printf '%s\n' "$@" > /run/magicbridge/oled-status 2>/dev/null || true; }
clear_oled() { rm -f /run/magicbridge/oled-status 2>/dev/null || true; }

# --- mDNS self-heal (2026-07-09) -----------------------------------------
# avahi-daemon was found masked+inactive on a live unit, and the hostname
# had been reset to a bogus "DESKTOP-XXXXXXX"-style placeholder (an
# SD-card-imaging-tool leftover, not anything MagicBridge set - install.sh
# has always set hostname="magicbridge" correctly at install time). Both
# silently break magicbridge.local / <hostname>.local with no visible error
# until someone tries to browse there. Checked and self-healed every boot,
# not just at first-provision time, so a future regression of either kind
# doesn't require another manual SSH session to notice and fix.
ensure_mdns_healthy() {
    # avahi-daemon.socket can be masked independently of avahi-daemon.service
    # (systemd treats them as separate units) - both have to be unmasked or
    # the service refuses to start with "Unit avahi-daemon.socket is masked."
    # even though avahi-daemon.service itself looks fine. Found this the hard
    # way: unmasking just the .service left restart failing silently.
    for u in avahi-daemon avahi-daemon.socket; do
        if systemctl is-enabled "$u" 2>/dev/null | grep -q masked; then
            echo "[$(date)] $u was masked - unmasking"
            systemctl unmask "$u"
        fi
    done
    systemctl enable avahi-daemon --now 2>/dev/null || systemctl restart avahi-daemon || true

    # STEALTH: a realistic "DESKTOP-XXXXXXX"/"WIN-*" hostname is exactly the
    # identity we want on the LAN (see install.sh) - keep it. Only replace an
    # actual product/vendor TELL (magicbridge / raspberrypi / bare localhost)
    # with a fresh realistic name, so provisioning never re-brands the device.
    CUR_HOST=$(hostname)
    if [[ "$CUR_HOST" == "magicbridge" || "$CUR_HOST" == "raspberrypi" || "$CUR_HOST" == "localhost" || -z "$CUR_HOST" ]]; then
        NEWHN="DESKTOP-$(tr -dc 'A-Z0-9' </dev/urandom 2>/dev/null | head -c 7 || true)"
        echo "[$(date)] Hostname '$CUR_HOST' is a tell - setting realistic '$NEWHN'"
        hostnamectl set-hostname "$NEWHN"
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEWHN.local $NEWHN/" /etc/hosts
        systemctl restart avahi-daemon || true
    fi

    # Optional friendly mDNS alias is opt-in (config "mdns_alias", off by
    # default); the service no-ops when unset. avahi still auto-publishes
    # <hostname>.local, so the unit stays reachable without advertising a name.
    systemctl enable mb-mdns-alias.service --now 2>/dev/null || true
}
# Disable errexit for EVERYTHING from here on. ensure_mdns_healthy runs
# hostnamectl / systemctl unmask / sed / systemctl restart - any of which can
# return non-zero (busy dbus, masked unit) and, under set -e, would abort the
# WHOLE script BEFORE the setup hotspot is ever raised: no network, no hotspot,
# no OLED escape = a bricked headless unit. Provisioning is best-effort with
# explicit checks + the /boot escape-hatch report, so it must never hard-exit.
set +e
ensure_mdns_healthy

# Check for live network (WiFi, Ethernet, or otherwise) via NetworkManager's
# overall state, not just wlan0 specifically - this fires the setup hotspot
# only when the Pi genuinely has no way onto any network, every boot.
# Poll up to ~40s for connectivity (B7). Two fixes over the old single 8s +
# exact "^connected$" check, which tore GOOD units into the setup hotspot
# (disconnecting wlan0 and DELETING the working profile):
#   1. Match the "connected" PREFIX, so "connected (site only)" - a normal
#      isolated-LAN / no-internet KVM state - counts as connected.
#   2. Poll instead of sampling once, because WPA + DHCP on a slow AP routinely
#      takes well over 8s, during which nmcli still reports "connecting".
CONNECTED=0
for _i in $(seq 1 20); do
    if nmcli -t -f STATE general 2>/dev/null | grep -qE '^connected'; then CONNECTED=1; break; fi
    sleep 2
done
if [[ "$CONNECTED" == "1" ]]; then
    echo "[$(date)] Already connected, nothing to do"
    clear_oled                      # normal status display
    touch "$FLAG_FILE"
    exit 0
fi

echo "[$(date)] No WiFi, starting provisioning AP: $AP_SSID"

# B4: the ENTIRE AP phase (dep install, wlan0 bring-up, hostapd/dnsmasq launch,
# captive portal, teardown) runs under `set +e`. Previously errexit was active
# here, so any failure of apt/ip/hostapd/dnsmasq exited immediately and SKIPPED
# the teardown + /boot escape-hatch report + OLED error below - leaving a
# no-network unit silently broadcasting a dead AP with zero on-card diagnostic.
# Every step here is best-effort with explicit checks; do not re-enable errexit
# until after the teardown/report block.
set +e

# Dependencies
for pkg in hostapd dnsmasq python3 python3-flask; do
    dpkg -s "$pkg" &>/dev/null || apt-get install -y "$pkg"
done

# Stop NM on wlan0 temporarily
nmcli device disconnect "$AP_IFACE" 2>/dev/null || true
sleep 1

# Bring up static IP on wlan0
ip link set "$AP_IFACE" up
ip addr flush dev "$AP_IFACE" 2>/dev/null || true
ip addr add "${AP_IP}/24" dev "$AP_IFACE"

# hostapd config
cat > /tmp/mb-hostapd.conf <<HOSTCONF
interface=$AP_IFACE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=6
auth_algs=1
wmm_enabled=0
HOSTCONF

# dnsmasq (DHCP + captive redirect)
# bind-dynamic + except-interface=lo: only bind wlan0's address, never the
# lo/wildcard :53 socket. Without this dnsmasq grabs the wildcard 0.0.0.0:53
# and collides with anything already holding :53 (the system dnsmasq.service,
# or systemd-resolved on other setups) -> our AP dnsmasq dies on EADDRINUSE ->
# clients on MagicBridge-Setup get NO DHCP and NO captive-DNS -> the hotspot
# appears but is dead. (Same class as the PiKVM port-53 saga; see
# MAGICBRIDGE_SYSTEM.md.) dhcp-authoritative speeds up address handout; the
# lease file lives in tmpfs.
cat > /tmp/mb-dnsmasq.conf <<DNSCONF
interface=$AP_IFACE
except-interface=lo
bind-dynamic
dhcp-range=192.168.73.10,192.168.73.50,12h
dhcp-authoritative
dhcp-leasefile=/run/mb-dnsmasq.leases
address=/#/$AP_IP
no-resolv
no-hosts
DNSCONF

# Free the WiFi radio and the :53 socket before we launch the AP.
# rfkill unblock: harmless no-op if wifi isn't blocked, but if a fresh/relocated
#   Pi has wlan0 soft-blocked, hostapd would silently fail and the setup hotspot
#   would never appear at all ("no wifi found").
# stop system dnsmasq.service: it's enabled on this image and holds the wildcard
#   :53, which even bind-dynamic can't share -> our AP dnsmasq would fail to
#   bind. Stopped only for the duration of provisioning; restored in teardown.
# stop nginx: it binds 0.0.0.0:80 as default_server, so the captive portal
#   (which binds AP_IP:80) fails with "Address already in use" -> the portal
#   exits immediately -> the teardown below removes the AP -> the user is left
#   staring at an OLED telling them to join a hotspot that no longer exists.
#   This was latent: on a fresh flash nginx used to be DEAD (its TLS cert is
#   stripped from the image), so :80 happened to be free and provisioning
#   worked by accident. Restored in teardown.
rfkill unblock wifi 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
NGINX_WAS_ACTIVE=0
systemctl is-active --quiet nginx && NGINX_WAS_ACTIVE=1
[ "$NGINX_WAS_ACTIVE" = "1" ] && { systemctl stop nginx 2>/dev/null || true; }

pkill -f "hostapd /tmp/mb-hostapd" 2>/dev/null || true
pkill -f "dnsmasq.*mb-dnsmasq"     2>/dev/null || true

hostapd -B /tmp/mb-hostapd.conf -P /tmp/mb-hostapd.pid
sleep 1
dnsmasq -C /tmp/mb-dnsmasq.conf --pid-file=/tmp/mb-dnsmasq.pid
sleep 1

# Confirm the AP actually came up. If hostapd or dnsmasq isn't running there is
# no hotspot to serve the portal on, so skip the (blocking) portal and fall
# straight through to teardown + the /boot escape-hatch report + OLED error.
AP_FAIL=""
pgrep -f "hostapd /tmp/mb-hostapd" >/dev/null 2>&1 || AP_FAIL="hostapd"
pgrep -f "dnsmasq.*mb-dnsmasq"     >/dev/null 2>&1 || AP_FAIL="${AP_FAIL:+$AP_FAIL+}dnsmasq"

if [[ -n "$AP_FAIL" ]]; then
    echo "[$(date)] AP bring-up FAILED ($AP_FAIL) - skipping portal, going to teardown/report"
    PORTAL_EXIT=98
else
    # iptables redirect all port 80/443 to portal
    iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 80 \
             -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true
    iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 443 \
             -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true

    # Let the AP's captive DNS actually answer. The shipped rules.v4 is
    # default-deny INPUT and allows UDP 5353 + 67 but NOT 53, so on a flashed
    # unit every DNS query from the phone was DROPped: dnsmasq's
    # "address=/#/$AP_IP" never reached anyone, the OS captive-detection probe
    # could not resolve, the sign-in sheet never appeared, and the phone just
    # said "connected, no internet". Scoped to the AP interface and removed in
    # teardown below, so nothing is left open once provisioning ends.
    iptables -I INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT 2>/dev/null || true
    iptables -I INPUT -i "$AP_IFACE" -p tcp --dport 53 -j ACCEPT 2>/dev/null || true

    echo "[$(date)] AP up, SSID '$AP_SSID', IP $AP_IP, portal :$PORTAL_PORT"
    # Second line is the manual escape hatch: if the captive sheet still does
    # not pop (some Android builds, a phone with private DNS forced on), this
    # is the only thing on the whole device that tells the user where to
    # browse. oled.py's @WIFI screen renders 2 lines truncated to 17 chars, so
    # both strings are sized to fit: "Join: Setup-1a2b" is 16, "Open
    # 192.168.73.1" is exactly 17. Do not lengthen either without widening
    # _draw_phase_anim's @WIFI branch.
    oled "@WIFI" "Join: $AP_SSID" "Open $AP_IP"

    # Arm teardown BEFORE the portal blocks. The portal waits indefinitely, so a
    # systemd TimeoutStartSec kill (or any signal) while nobody is completing
    # setup used to bypass teardown entirely: nginx left stopped, system dnsmasq
    # stopped, wlan0 still pinned at 192.168.73.1 with the :80/:443 DNAT rules
    # in place, and the OLED still pointing at a hotspot that is gone. Reads as a
    # bricked unit until a power cycle. ap_teardown is once-only (AP_TORNDOWN), so
    # the trap plus the inline call after the portal cannot double-run it.
    trap ap_teardown EXIT INT TERM
    # Run captive portal (blocks until user submits). errexit is already off for
    # the whole AP phase (set +e above), so a portal crash still falls through
    # to teardown - the Pi must never be left broadcasting an unreachable AP.
    python3 "$PORTAL_SCRIPT" "$AP_IP" "$PORTAL_PORT" "$WIFI_FILE" "$TS_KEY_TMP"
    PORTAL_EXIT=$?
fi

echo "[$(date)] Portal exited (code $PORTAL_EXIT)"

# Tear down AP. Runs from the EXIT/INT/TERM trap armed below as well as inline,
# so a systemd timeout or a signal mid-portal can never leave the unit stranded
# on a half-configured hotspot. Once-only via AP_TORNDOWN.
AP_TORNDOWN=0
ap_teardown() {
    [ "$AP_TORNDOWN" = "1" ] && return 0
    AP_TORNDOWN=1
    pkill -F /tmp/mb-hostapd.pid 2>/dev/null || true
    pkill -F /tmp/mb-dnsmasq.pid 2>/dev/null || true
    iptables -t nat -F PREROUTING 2>/dev/null || true
    # Drop the captive-DNS allows we added for the AP window. Looped because a
    # retried provisioning run can insert them more than once, and they must not
    # survive into normal operation.
    while iptables -D INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT 2>/dev/null; do :; done
    while iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 53 -j ACCEPT 2>/dev/null; do :; done
    ip addr flush dev "$AP_IFACE" 2>/dev/null || true
    # Put back what we stopped to free :80 and :53, or the unit comes up with no
    # web UI and no name resolution and looks bricked.
    [ "${NGINX_WAS_ACTIVE:-0}" = "1" ] && systemctl start nginx 2>/dev/null || true
    systemctl start dnsmasq 2>/dev/null || true
}
ap_teardown
# Mirror a human-readable report onto the FAT boot partition. THIS IS THE
# ESCAPE HATCH: a unit that has neither WiFi nor a working hotspot is otherwise
# unreachable, and its ext4 logs can't be read on Windows/macOS (wsl --mount
# refuses removable SD readers). /boot/firmware is FAT32, so anyone can pull the
# card, open it in Explorer/Finder, and read this file.
mb_boot_report() {
    local B=/boot/firmware; [ -d "$B" ] || B=/boot; [ -d "$B" ] || return 0
    {
      echo "MagicBridge setup report - $(date)"
      echo "hostname   : $(hostname)"
      echo "ip         : $(hostname -I 2>/dev/null)"
      echo "portal_exit: ${PORTAL_EXIT:-n/a}    nginx_was_active: ${NGINX_WAS_ACTIVE:-n/a}"
      echo "ap_ssid    : $AP_SSID on $AP_IFACE ($AP_IP)"
      echo "saved wifi : $(ls /etc/NetworkManager/system-connections/ 2>/dev/null | wc -l) profile(s)"
      echo
      echo "===== who is listening on :80 (portal needs it) ====="
      ss -ltnp 2>/dev/null | grep -E ':80\b' || echo "(nothing on :80)"
      echo
      echo "===== hostapd / dnsmasq running? ====="
      pgrep -a hostapd 2>/dev/null || echo "(hostapd NOT running)"
      pgrep -a dnsmasq 2>/dev/null || echo "(dnsmasq NOT running)"
      echo
      # REDACTED. This file lands on the FAT boot partition, which is readable
      # by anyone who pulls the card, and it is never deleted on a live unit.
      # The provisioning log records the network being joined ("Connecting to
      # 'HOME-5G'"), and one mistyped password is enough to get a failure logged
      # and the script re-run, so the SSID ends up here. A network name is
      # location-identifying, and persisting it on the card is exactly what the
      # anonymity model forbids. This report only needs to prove which services
      # were alive, so strip anything quoted before it is written.
      echo "===== last 60 lines: provisioning (network names redacted) ====="
      tail -60 "$LOG" 2>/dev/null | sed "s/'[^']*'/'<redacted>'/g"
      echo
      echo "===== last 40 lines: first boot (network names redacted) ====="
      tail -40 /var/log/magicbridge-firstboot.log 2>/dev/null | sed "s/'[^']*'/'<redacted>'/g"
    } > "$B/magicbridge-setup-report.txt" 2>/dev/null || true
    sync
}
mb_boot_report

# Restore the system dnsmasq we stopped above so the box's normal DNS/DHCP
# state matches what it was before provisioning (no-op if it wasn't enabled).
systemctl start dnsmasq 2>/dev/null || true
# Restore nginx (stopped so the portal could own :80). Do this even on the
# failure path - the web UI must come back regardless of how provisioning ended.
[ "${NGINX_WAS_ACTIVE:-0}" = "1" ] && { systemctl start nginx 2>/dev/null || true; }
# If the portal never got a WiFi submission, don't leave the OLED telling the
# user to join a hotspot we just tore down - say what actually happened.
if [[ -n "${AP_FAIL:-}" ]]; then
    echo "[$(date)] hotspot bring-up failed ($AP_FAIL) - flagging on OLED + boot report"
    oled "Hotspot FAILED" "$AP_FAIL" "see card report"
elif [[ "${PORTAL_EXIT:-1}" -ne 0 && ! -f "$WIFI_FILE" ]]; then
    echo "[$(date)] portal exited $PORTAL_EXIT with no WiFi submitted - flagging on OLED"
    oled "Setup problem" "portal failed" "see provision.log"
fi
sleep 1

# Connect saved WiFi via NetworkManager
if [[ -f "$WIFI_FILE" ]]; then
    SSID=$(sed -n '1p' "$WIFI_FILE")
    PASS=$(sed -n '2p' "$WIFI_FILE")
    echo "[$(date)] Connecting to '$SSID'…"
    oled "@CONNECTING" "Connecting to WiFi:" "$SSID"
    nmcli connection delete "$SSID" 2>/dev/null || true
    # 802-11-wireless.hidden yes: without it a hidden SSID never associates,
    # because NM only probes for networks it saw in a scan. Harmless on a normal
    # broadcast network, which still connects exactly as before.
    #
    # key-mgmt is deliberately NOT pinned to wpa-psk any more. Pinning it makes a
    # WPA3/SAE-only router fail association even with the CORRECT password, and
    # the buyer sees "WiFi setup failed", i.e. we blame their password for a
    # setting we chose. Left unset, NetworkManager negotiates PSK or SAE from
    # what the AP actually advertises, covering WPA2, WPA3 and mixed mode.
    if [[ -z "$PASS" ]]; then
        nmcli connection add type wifi con-name "$SSID" ssid "$SSID" \
              802-11-wireless.hidden yes \
              connection.autoconnect yes
    else
        nmcli connection add type wifi con-name "$SSID" ssid "$SSID" \
              802-11-wireless.hidden yes \
              wifi-sec.psk "$PASS" \
              connection.autoconnect yes
    fi
    nmcli connection up "$SSID" || true
    rm -f "$WIFI_FILE"
    # VERIFY it actually connected. A wrong password (or an out-of-range SSID)
    # makes `nmcli connection up` fail - and the old code still announced
    # "Connected!", left the AP torn down and exited, stranding the unit with NO
    # WiFi and NO hotspot until someone power-cycled it. Now: confirm first, and
    # on failure drop the bad profile and re-raise the setup hotspot so the user
    # can simply try again with the right password.
    WIFI_OK=0
    for _ in $(seq 1 12); do
        # Match the "connected" PREFIX, same as the initial check at the top of
        # this script. An air-gapped / no-internet LAN (a very plausible home for
        # this device) associates fine but NM reports "connected (site only)",
        # which the old anchored "^connected$" rejected: the loop would time out,
        # DELETE the just-created good profile, and re-raise the setup hotspot,
        # stranding a unit that has the correct password. "connecting" doesn't
        # start with "connected", so the prefix can't accept a still-connecting state.
        if nmcli -t -f STATE general 2>/dev/null | grep -qE '^connected'; then WIFI_OK=1; break; fi
        sleep 2
    done
    if [[ "$WIFI_OK" != "1" ]]; then
        echo "[$(date)] WiFi '$SSID' did NOT connect (wrong password / out of range)"
        nmcli connection delete "$SSID" 2>/dev/null || true   # never keep a bad profile
        MB_PROV_RETRY="${MB_PROV_RETRY:-0}"
        if [[ "$MB_PROV_RETRY" -lt 4 ]]; then
            export MB_PROV_RETRY=$(( MB_PROV_RETRY + 1 ))
            echo "[$(date)] re-raising the setup hotspot (attempt $MB_PROV_RETRY)"
            oled "WiFi failed" "Wrong password?" "Rejoin $AP_SSID"
            sleep 4
            # Re-exec from the top: the connectivity check finds no network and
            # brings the AP back up. Exported retry counter survives the exec so
            # this can't spin forever.
            exec bash "$0"
        fi
        oled "WiFi setup failed" "too many tries" "power-cycle to retry"
        echo "[$(date)] giving up after $MB_PROV_RETRY attempts"
        exit 1
    fi
    # Connected for real - celebrate + show the IP, then normal display.
    for _ in 1 2 3; do
        MB_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
        [[ -n "$MB_IP" ]] && break
        sleep 2
    done
    oled "@READY" "Connected!" "${MB_IP:-getting IP...}"
    sleep 6
    clear_oled                      # connected → back to normal status
fi

# Tailscale auth key (if provided)
if [[ -f "$TS_KEY_TMP" ]]; then
    TS_KEY=$(cat "$TS_KEY_TMP")
    rm -f "$TS_KEY_TMP"
    if [[ -n "$TS_KEY" ]]; then
        echo "[$(date)] Authenticating Tailscale…"
        tailscale up --authkey="$TS_KEY" --accept-routes --reset || true
    fi
fi

touch "$FLAG_FILE"
echo "[$(date)] Provisioning complete"
