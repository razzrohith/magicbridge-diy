#!/bin/bash
# ============================================================
#  MagicBridge WiFi Provisioning AP
#
#  Runs as a systemd service (mb-provision.service) on every boot.
#  Starts a hostapd access point "MagicBridge-Setup" whenever the Pi
#  has no working network connection (first-time setup, or later if
#  it's moved somewhere its saved WiFi isn't in range), serves a
#  captive-portal setup page, then hands off to NetworkManager and
#  exits. Not a one-time thing: it re-checks connectivity on every
#  boot rather than gating itself off permanently after first success,
#  so moving this to a new location just means going through the same
#  setup-hotspot flow again instead of losing wireless access to it.
#
#  Credentials: SSID "MagicBridge-Setup"  (no password)
#  Portal: http://192.168.73.1/ (opened by captive detection)
# ============================================================
set -e

FLAG_FILE="/etc/magicbridge/.provisioned"   # informational timestamp only, doesn't gate anything
WIFI_FILE="/etc/magicbridge/.provision-wifi"
LOG="/var/log/magicbridge-provision.log"
AP_SSID="MagicBridge-Setup"
AP_IP="192.168.73.1"
AP_IFACE="wlan0"
PORTAL_PORT=80
PORTAL_SCRIPT="/opt/magicbridge/provision/mb-setup-ui.py"
TS_KEY_TMP="/tmp/mb-ts-key"

exec >> "$LOG" 2>&1
echo "[$(date)] mb-provision.sh starting"

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

    # Only resets an obviously-bogus placeholder hostname - never touches a
    # hostname someone actually chose on purpose.
    CUR_HOST=$(hostname)
    if [[ "$CUR_HOST" =~ ^DESKTOP-.* || "$CUR_HOST" =~ ^WIN-.* || "$CUR_HOST" == "localhost" ]]; then
        echo "[$(date)] Hostname '$CUR_HOST' looks like an imaging-tool default, resetting to 'magicbridge'"
        hostnamectl set-hostname magicbridge
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tmagicbridge/" /etc/hosts
        systemctl restart avahi-daemon || true
    fi

    # magicbridge.local is published as its own standing alias (see
    # mb-mdns-alias.service) specifically so it keeps working even if the
    # box's own hostname drifts again for some other reason in the future.
    systemctl enable mb-mdns-alias.service --now 2>/dev/null || true
}
ensure_mdns_healthy

# Check for live network (WiFi, Ethernet, or otherwise) via NetworkManager's
# overall state, not just wlan0 specifically - this fires the setup hotspot
# only when the Pi genuinely has no way onto any network, every boot.
sleep 8   # give NetworkManager time to connect saved networks
CONNECTED=$(nmcli -t -f STATE general 2>/dev/null | grep -c "^connected$" || true)
if [[ "$CONNECTED" -gt 0 ]]; then
    echo "[$(date)] Already connected, nothing to do"
    touch "$FLAG_FILE"
    exit 0
fi

echo "[$(date)] No WiFi, starting provisioning AP: $AP_SSID"

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
rfkill unblock wifi 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

pkill -f "hostapd /tmp/mb-hostapd" 2>/dev/null || true
pkill -f "dnsmasq.*mb-dnsmasq"     2>/dev/null || true

hostapd -B /tmp/mb-hostapd.conf -P /tmp/mb-hostapd.pid
sleep 1
dnsmasq -C /tmp/mb-dnsmasq.conf --pid-file=/tmp/mb-dnsmasq.pid

# iptables redirect all port 80 to portal
iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 80 \
         -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true
iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 443 \
         -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true

echo "[$(date)] AP up, SSID '$AP_SSID', IP $AP_IP, portal :$PORTAL_PORT"

# Run captive portal (blocks until user submits)
# Temporarily disable errexit: if the portal script exits non-zero (crash, kill,
# etc.) we still MUST fall through to AP teardown below, or the Pi is stuck
# broadcasting the setup AP with no way to reach it again over normal WiFi.
set +e
python3 "$PORTAL_SCRIPT" "$AP_IP" "$PORTAL_PORT" "$WIFI_FILE" "$TS_KEY_TMP"
PORTAL_EXIT=$?
set -e

echo "[$(date)] Portal exited (code $PORTAL_EXIT)"

# Tear down AP
pkill -F /tmp/mb-hostapd.pid 2>/dev/null || true
pkill -F /tmp/mb-dnsmasq.pid 2>/dev/null || true
iptables -t nat -F PREROUTING 2>/dev/null || true
ip addr flush dev "$AP_IFACE" 2>/dev/null || true
# Restore the system dnsmasq we stopped above so the box's normal DNS/DHCP
# state matches what it was before provisioning (no-op if it wasn't enabled).
systemctl start dnsmasq 2>/dev/null || true
sleep 1

# Connect saved WiFi via NetworkManager
if [[ -f "$WIFI_FILE" ]]; then
    SSID=$(sed -n '1p' "$WIFI_FILE")
    PASS=$(sed -n '2p' "$WIFI_FILE")
    echo "[$(date)] Connecting to '$SSID'…"
    nmcli connection delete "$SSID" 2>/dev/null || true
    if [[ -z "$PASS" ]]; then
        nmcli connection add type wifi con-name "$SSID" ssid "$SSID" \
              connection.autoconnect yes
    else
        nmcli connection add type wifi con-name "$SSID" ssid "$SSID" \
              wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
              connection.autoconnect yes
    fi
    nmcli connection up "$SSID" || true
    rm -f "$WIFI_FILE"
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
