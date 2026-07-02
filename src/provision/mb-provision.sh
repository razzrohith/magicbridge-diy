#!/bin/bash
# ============================================================
#  MagicBridge — WiFi Provisioning AP
#
#  Runs as a systemd service (mb-provision.service).
#  Starts a hostapd access point "MagicBridge-Setup" when no
#  WiFi connection exists, serves a captive-portal setup page,
#  then hands off to NetworkManager and exits.
#
#  Credentials: SSID "MagicBridge-Setup"  (no password)
#  Portal: http://192.168.73.1/ (opened by captive detection)
# ============================================================
set -e

FLAG_FILE="/etc/magicbridge/.provisioned"
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

# ── Already provisioned? ──────────────────────────────────────────────────────
if [[ -f "$FLAG_FILE" ]]; then
    echo "[$(date)] Flag found — already provisioned, exiting"
    exit 0
fi

# ── Check for live WiFi ───────────────────────────────────────────────────────
sleep 8   # give NetworkManager time to connect saved networks
CONNECTED=$(nmcli -t -f STATE general 2>/dev/null | grep -c "^connected$" || true)
if [[ "$CONNECTED" -gt 0 ]]; then
    echo "[$(date)] WiFi already connected — marking provisioned"
    touch "$FLAG_FILE"
    exit 0
fi

echo "[$(date)] No WiFi — starting provisioning AP: $AP_SSID"

# ── Dependencies ─────────────────────────────────────────────────────────────
for pkg in hostapd dnsmasq python3 python3-flask; do
    dpkg -s "$pkg" &>/dev/null || apt-get install -y "$pkg"
done

# ── Stop NM on wlan0 temporarily ─────────────────────────────────────────────
nmcli device disconnect "$AP_IFACE" 2>/dev/null || true
sleep 1

# ── Bring up static IP on wlan0 ──────────────────────────────────────────────
ip link set "$AP_IFACE" up
ip addr flush dev "$AP_IFACE" 2>/dev/null || true
ip addr add "${AP_IP}/24" dev "$AP_IFACE"

# ── hostapd config ───────────────────────────────────────────────────────────
cat > /tmp/mb-hostapd.conf <<HOSTCONF
interface=$AP_IFACE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=6
auth_algs=1
wmm_enabled=0
HOSTCONF

# ── dnsmasq (DHCP + captive redirect) ────────────────────────────────────────
cat > /tmp/mb-dnsmasq.conf <<DNSCONF
interface=$AP_IFACE
dhcp-range=192.168.73.10,192.168.73.50,12h
address=/#/$AP_IP
no-resolv
no-hosts
DNSCONF

pkill -f "hostapd /tmp/mb-hostapd" 2>/dev/null || true
pkill -f "dnsmasq.*mb-dnsmasq"     2>/dev/null || true

hostapd -B /tmp/mb-hostapd.conf -P /tmp/mb-hostapd.pid
sleep 1
dnsmasq -C /tmp/mb-dnsmasq.conf --pid-file=/tmp/mb-dnsmasq.pid

# ── iptables redirect all port 80 to portal ───────────────────────────────────
iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 80 \
         -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true
iptables -t nat -A PREROUTING -i "$AP_IFACE" -p tcp --dport 443 \
         -j DNAT --to-destination "${AP_IP}:${PORTAL_PORT}" 2>/dev/null || true

echo "[$(date)] AP up — SSID '$AP_SSID', IP $AP_IP, portal :$PORTAL_PORT"

# ── Run captive portal (blocks until user submits) ────────────────────────────
# Temporarily disable errexit: if the portal script exits non-zero (crash, kill,
# etc.) we still MUST fall through to AP teardown below, or the Pi is stuck
# broadcasting the setup AP with no way to reach it again over normal WiFi.
set +e
python3 "$PORTAL_SCRIPT" "$AP_IP" "$PORTAL_PORT" "$WIFI_FILE" "$TS_KEY_TMP"
PORTAL_EXIT=$?
set -e

echo "[$(date)] Portal exited (code $PORTAL_EXIT)"

# ── Tear down AP ─────────────────────────────────────────────────────────────
pkill -F /tmp/mb-hostapd.pid 2>/dev/null || true
pkill -F /tmp/mb-dnsmasq.pid 2>/dev/null || true
iptables -t nat -F PREROUTING 2>/dev/null || true
ip addr flush dev "$AP_IFACE" 2>/dev/null || true
sleep 1

# ── Connect saved WiFi via NetworkManager ─────────────────────────────────────
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

# ── Tailscale auth key (if provided) ─────────────────────────────────────────
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
