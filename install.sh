#!/bin/bash
# =============================================================
#  MagicBridge — Full Installer
#  Raspberry Pi 4 · Pi OS Bookworm (64-bit)
#
#  Run as root:
#    curl -fsSL https://raw.githubusercontent.com/razzrohith/magicbridge/main/install.sh | sudo bash
#  Or from a local clone:
#    sudo bash install.sh
#
#  What this does:
#    1. Checks / installs all prerequisites via apt
#    2. Enables USB OTG (dwc2) via /boot/firmware/config.txt
#    3. Clones or updates razzrohith/magicbridge from GitHub
#    4. Installs all components to /opt/magicbridge/
#    5. Generates self-signed TLS cert
#    6. Configures nginx, systemd services, firewall
#    7. Optionally installs Tailscale
#    8. Sets hostname to "magicbridge", enables SSH password login
# =============================================================
set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${BLUE}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
die()  { echo -e "${RED}✗ FATAL:${NC} $*"; exit 1; }

echo -e "${BOLD}"
cat <<'BANNER'
  __  __             _      ____       _     _
 |  \/  | __ _  __ _(_) ___|  _ \_ __(_) __| | __ _  ___
 | |\/| |/ _` |/ _` | |/ __| |_) | '__| |/ _` |/ _` |/ _ \
 | |  | | (_| | (_| | | (__|  _ <| |  | | (_| | (_| |  __/
 |_|  |_|\__,_|\__, |_|\___|_| \_\_|  |_|\__,_|\__, |\___|
                |___/                            |___/
BANNER
echo -e "${NC}"
info "MagicBridge installer — Raspberry Pi 4, Pi OS Bookworm"
echo ""

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"

# ── Architecture check ────────────────────────────────────────────────────────
ARCH=$(uname -m)
[[ "$ARCH" == "aarch64" || "$ARCH" == "armv7l" ]] || \
    warn "Architecture '$ARCH' — intended for Raspberry Pi 4"

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_URL="https://github.com/razzrohith/magicbridge"
BRANCH="main"
INSTALL_DIR="/opt/magicbridge"
CONFIG_DIR="/etc/magicbridge"
LOG_DIR="/var/log"
HOSTNAME_NEW="magicbridge"
SSH_PORT=22
MB_USER="${SUDO_USER:-pi}"     # non-root user who ran sudo

# ══════════════════════════════════════════════════════════════════════════════
# 1. PREREQUISITES
# ══════════════════════════════════════════════════════════════════════════════
info "Updating apt lists…"
apt-get update -qq

APT_PKGS=(
    # Python / web
    python3 python3-pip python3-aiohttp python3-flask python3-bcrypt
    # Git
    git
    # Video
    v4l-utils
    # USB gadget
    # (libcomposite is a kernel module — no apt package needed)
    # MJPEG streaming
    ustreamer ffmpeg
    # nginx
    nginx
    # SSL
    openssl
    # WiFi / network
    NetworkManager wireless-tools wpasupplicant
    # Provisioning AP
    hostapd dnsmasq
    # mDNS
    avahi-daemon libnss-mdns
    # Misc
    curl jq
)

info "Installing packages…"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${APT_PKGS[@]}" 2>&1 | grep -E "^(Setting up|E:|W:)" || true

# Python packages not in apt
pip3 install --break-system-packages --quiet \
    aiohttp flask bcrypt 2>/dev/null || true

ok "Prerequisites installed"

# ══════════════════════════════════════════════════════════════════════════════
# 2. USB OTG (dwc2)
# ══════════════════════════════════════════════════════════════════════════════
CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

info "Configuring USB OTG in $CONFIG_TXT…"
if ! grep -q "^dtoverlay=dwc2" "$CONFIG_TXT"; then
    echo "dtoverlay=dwc2" >> "$CONFIG_TXT"
    ok "dtoverlay=dwc2 added"
else
    ok "dtoverlay=dwc2 already present"
fi

# Load module now (for immediate use without reboot)
modprobe libcomposite 2>/dev/null || warn "libcomposite not loadable now — needs reboot"

# ══════════════════════════════════════════════════════════════════════════════
# 3. CLONE / UPDATE REPO
# ══════════════════════════════════════════════════════════════════════════════
SRC_DIR="/tmp/magicbridge-src"

if [[ -d "/opt/magicbridge-repo/.git" ]]; then
    info "Updating existing clone…"
    git -C /opt/magicbridge-repo pull origin "$BRANCH" --ff-only || true
    SRC_DIR="/opt/magicbridge-repo"
elif [[ -d "$(dirname "$0")/src" ]]; then
    # Running from a local clone
    SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
    info "Using local source at $SRC_DIR"
else
    info "Cloning $REPO_URL…"
    rm -rf "$SRC_DIR"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
    ok "Cloned to $SRC_DIR"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 4. INSTALL FILES
# ══════════════════════════════════════════════════════════════════════════════
info "Installing MagicBridge to $INSTALL_DIR…"

mkdir -p "$INSTALL_DIR"/{core,web,dashboard,provision}
mkdir -p "$CONFIG_DIR"/ssl

# Core KVM server
cp "$SRC_DIR/src/core/magicbridge.py"  "$INSTALL_DIR/core/"
cp "$SRC_DIR/src/core/hid.py"         "$INSTALL_DIR/core/"
cp "$SRC_DIR/src/core/video.py"       "$INSTALL_DIR/core/"

# Web UI
cp "$SRC_DIR/src/web/index.html"      "$INSTALL_DIR/web/"
[[ -d "$SRC_DIR/src/web/static" ]] && \
    cp -r "$SRC_DIR/src/web/static"   "$INSTALL_DIR/web/"

# Stealth dashboard
cp "$SRC_DIR/src/dashboard/stealth-dashboard.py" "$INSTALL_DIR/dashboard/"

# Provisioning
cp "$SRC_DIR/src/provision/mb-setup-ui.py"       "$INSTALL_DIR/provision/"

# Scripts
cp "$SRC_DIR/src/core/mb-gadget.sh"    /usr/local/bin/mb-gadget.sh
cp "$SRC_DIR/src/provision/mb-provision.sh" /usr/local/bin/mb-provision.sh
chmod +x /usr/local/bin/mb-gadget.sh /usr/local/bin/mb-provision.sh

ok "Files installed"

# ══════════════════════════════════════════════════════════════════════════════
# 5. INITIAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════
info "Writing initial config.json…"
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    cat > "$CONFIG_DIR/config.json" <<'CONF'
{
  "usb": {
    "manufacturer": "Logitech",
    "product":      "USB Keyboard K120",
    "serial":       "12AB34CD",
    "idVendor":     "0x046d",
    "idProduct":    "0xc31c"
  },
  "video": {
    "device":     "",
    "resolution": "1920x1080",
    "fps":        30,
    "quality":    80,
    "mode":       "mjpeg"
  },
  "mac_persist": {},
  "duckdns":     {}
}
CONF
    # Password hash is set by the dashboard on first boot
    ok "config.json created"
else
    ok "config.json already exists — skipping"
fi

chmod 600 "$CONFIG_DIR/config.json"

# ══════════════════════════════════════════════════════════════════════════════
# 6. TLS CERTIFICATE
# ══════════════════════════════════════════════════════════════════════════════
CERT="$CONFIG_DIR/ssl/cert.pem"
KEY="$CONFIG_DIR/ssl/key.pem"

if [[ ! -f "$CERT" ]]; then
    info "Generating self-signed TLS certificate…"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=magicbridge.local" \
        -addext "subjectAltName=DNS:magicbridge.local,IP:127.0.0.1" \
        2>/dev/null
    chmod 600 "$KEY"
    ok "TLS cert generated (10-year, self-signed)"
else
    ok "TLS cert already exists — skipping"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 7. NGINX
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring nginx…"
rm -f /etc/nginx/sites-enabled/default

cp "$SRC_DIR/src/nginx/magicbridge.conf" \
   /etc/nginx/sites-available/magicbridge

ln -sf /etc/nginx/sites-available/magicbridge \
        /etc/nginx/sites-enabled/magicbridge

# nginx log files referenced by stealth dashboard
touch /var/log/nginx/magicbridge-access.log \
      /var/log/nginx/magicbridge-error.log   || true

nginx -t 2>&1 | grep -v "^$" || die "nginx config test failed"
systemctl enable nginx
systemctl restart nginx
ok "nginx configured and restarted"

# ══════════════════════════════════════════════════════════════════════════════
# 8. SYSTEMD SERVICES
# ══════════════════════════════════════════════════════════════════════════════
info "Installing systemd services…"

cp "$SRC_DIR/src/core/mb-gadget.service"         /etc/systemd/system/
cp "$SRC_DIR/src/core/magicbridge.service"        /etc/systemd/system/
cp "$SRC_DIR/src/dashboard/stealth-dashboard.service" /etc/systemd/system/
cp "$SRC_DIR/src/provision/mb-provision.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable mb-gadget
systemctl enable magicbridge
systemctl enable stealth-dashboard
systemctl enable mb-provision

# Start gadget immediately if we have a UDC
if ls /sys/class/udc/* &>/dev/null; then
    systemctl restart mb-gadget && ok "USB gadget started" || warn "USB gadget start failed — may need reboot"
else
    warn "No UDC found — USB gadget will start after reboot (needs dtoverlay=dwc2)"
fi

systemctl restart magicbridge       && ok "magicbridge started"       || warn "magicbridge start failed"
systemctl restart stealth-dashboard && ok "stealth-dashboard started" || warn "stealth-dashboard start failed"

# ══════════════════════════════════════════════════════════════════════════════
# 9. FIREWALL
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring firewall (iptables)…"

# Default-deny inbound; allow established, SSH, HTTP, HTTPS
iptables -F INPUT
iptables -P INPUT DROP
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -p tcp --dport "$SSH_PORT" -j ACCEPT
iptables -A INPUT -p tcp --dport 80  -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
iptables -A INPUT -p udp --dport 5353 -j ACCEPT   # mDNS
iptables -A INPUT -p udp --dport 67 -j ACCEPT     # DHCP (for AP mode)
iptables -A INPUT -p tcp --dport 7777 -j DROP     # stealth dashboard: internal only
iptables -A INPUT -p tcp --dport 8080 -j DROP     # aiohttp: internal only
iptables -A INPUT -p tcp --dport 8081 -j DROP     # ustreamer: internal only

# Persist rules across reboots
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iptables-persistent 2>&1 | grep -E "^(Setting|E:|W:)" || true
iptables-save > /etc/iptables/rules.v4
ok "Firewall configured"

# ══════════════════════════════════════════════════════════════════════════════
# 10. SSH HARDENING (keep password login, set port)
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring SSH…"
SSHD="/etc/ssh/sshd_config"
sed -i "s/^#*Port .*/Port $SSH_PORT/" "$SSHD"
# Ensure password auth on (required for admin / lol)
grep -q "^PasswordAuthentication" "$SSHD" && \
    sed -i "s/^PasswordAuthentication.*/PasswordAuthentication yes/" "$SSHD" || \
    echo "PasswordAuthentication yes" >> "$SSHD"
systemctl restart ssh
ok "SSH configured on port $SSH_PORT"

# ══════════════════════════════════════════════════════════════════════════════
# 11. HOSTNAME + mDNS
# ══════════════════════════════════════════════════════════════════════════════
info "Setting hostname to '$HOSTNAME_NEW'…"
hostnamectl set-hostname "$HOSTNAME_NEW"
if ! grep -q "^127.0.1.1.*$HOSTNAME_NEW" /etc/hosts; then
    sed -i "/^127.0.1.1/d" /etc/hosts
    echo "127.0.1.1  $HOSTNAME_NEW.local  $HOSTNAME_NEW" >> /etc/hosts
fi
systemctl enable avahi-daemon
systemctl restart avahi-daemon
ok "Hostname '$HOSTNAME_NEW.local' active"

# ══════════════════════════════════════════════════════════════════════════════
# 12. USER (admin / lol)
# ══════════════════════════════════════════════════════════════════════════════
info "Ensuring user 'admin'…"
if ! id -u admin &>/dev/null; then
    useradd -m -s /bin/bash admin
    ok "User 'admin' created"
fi
echo "admin:lol" | chpasswd
usermod -aG sudo,video,input,dialout admin 2>/dev/null || true
ok "User 'admin' configured (password: lol)"

# ══════════════════════════════════════════════════════════════════════════════
# 13. TAILSCALE (optional)
# ══════════════════════════════════════════════════════════════════════════════
if ! command -v tailscale &>/dev/null; then
    echo ""
    read -r -t 10 -p "Install Tailscale for remote access? [Y/n]: " TS_ANS || TS_ANS="Y"
    if [[ "${TS_ANS:-Y}" =~ ^[Yy]$ ]]; then
        info "Installing Tailscale…"
        curl -fsSL https://tailscale.com/install.sh | bash 2>&1 | tail -5
        systemctl enable tailscaled
        systemctl start tailscaled
        ok "Tailscale installed — run 'tailscale up' to authenticate"
    fi
else
    ok "Tailscale already installed"
    systemctl enable tailscaled 2>/dev/null || true
fi

# ══════════════════════════════════════════════════════════════════════════════
# 14. LOG FILES
# ══════════════════════════════════════════════════════════════════════════════
touch /var/log/magicbridge-auth.log \
      /var/log/magicbridge-sessions.log \
      /var/log/magicbridge-provision.log \
      /var/log/magicbridge.log
chmod 640 /var/log/magicbridge-*.log

# ══════════════════════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║         MagicBridge installed successfully!              ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}KVM interface${NC}     https://magicbridge.local/"
echo -e "  ${BOLD}Admin panel${NC}       https://magicbridge.local/stealth/"
echo -e "  ${BOLD}SSH${NC}               ssh admin@magicbridge.local  (password: lol)"
echo -e "  ${BOLD}Panel password${NC}    lol  (change in panel → System)"
echo ""
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "?")
echo -e "  ${BOLD}Local IP${NC}          $LOCAL_IP"
echo ""
warn "A reboot is required to activate USB OTG (dwc2 overlay)."
echo -e "  ${BOLD}sudo reboot${NC}"
echo ""
echo -e "  After reboot, connect the Pi's USB-C port to the target computer."
echo -e "  Open ${BOLD}https://magicbridge.local/${NC} on any device on the same network."
echo ""
