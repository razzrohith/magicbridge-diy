#!/bin/bash
# =============================================================
#  MagicBridge Full Installer
#  Raspberry Pi 4, Pi OS Bookworm (64-bit)
#
#  Run as root:
#    curl -fsSL https://raw.githubusercontent.com/razzrohith/MagicBridge/main/install.sh | sudo bash
#  Or from a local clone:
#    sudo bash install.sh
#
#  What this does:
#    1. Checks / installs all prerequisites via apt
#    2. Enables USB OTG (dwc2) via /boot/firmware/config.txt
#    3. Clones or updates razzrohith/MagicBridge from GitHub
#    4. Installs all components to /opt/magicbridge/
#    5. Generates self-signed TLS cert
#    6. Configures nginx, systemd services, firewall
#    7. Optionally installs Tailscale
#    8. Sets hostname to "magicbridge", enables SSH password login
#
#  Assumes the Pi user account (raj/lol, or whatever username was set
#  during Raspberry Pi Imager flashing) already has sudo + SSH access.
#  This script does not create or modify any Linux user accounts.
# =============================================================
set -euo pipefail

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
info "MagicBridge installer, Raspberry Pi 4, Pi OS Bookworm"
echo ""

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"

ARCH=$(uname -m)
[[ "$ARCH" == "aarch64" || "$ARCH" == "armv7l" ]] || \
    warn "Architecture '$ARCH', this installer targets Raspberry Pi 4"

# Configuration
REPO_URL="https://github.com/razzrohith/MagicBridge"
BRANCH="main"
INSTALL_DIR="/opt/magicbridge"
CONFIG_DIR="/etc/magicbridge"

# ══════════════════════════════════════════════════════════════════════════════
# 1. PREREQUISITES
# ══════════════════════════════════════════════════════════════════════════════
info "Updating apt lists..."
apt-get update -qq

# Install git first so clone works even if the rest fails
apt-get install -y git 2>&1 | grep -E "^(Setting up|E:|W:)" || true

APT_PKGS=(
    # Python / web
    python3 python3-pip python3-aiohttp python3-flask python3-bcrypt
    # Git
    git
    # Video
    v4l-utils
    # MJPEG streaming
    ustreamer ffmpeg
    # nginx
    nginx
    # SSL
    openssl
    # WiFi / network
    network-manager wireless-tools wpasupplicant
    # Provisioning AP
    hostapd dnsmasq
    # mDNS
    avahi-daemon libnss-mdns
    # Firewall
    iptables
    # Misc
    curl jq
)

info "Installing packages..."
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

info "Configuring USB OTG (peripheral mode) in $CONFIG_TXT..."
# Remove any existing dtoverlay=dwc2 line from any section
sed -i '/^dtoverlay=dwc2/d' "$CONFIG_TXT"
# Ensure [all] section exists; Pi 4 only applies settings outside [cm4]/[cm5]/[pi4] blocks
grep -q '^\[all\]' "$CONFIG_TXT" || echo '[all]' >> "$CONFIG_TXT"
# Insert immediately after [all] so it applies to all Pi models including Pi 4
sed -i '/^\[all\]/a dtoverlay=dwc2,dr_mode=peripheral' "$CONFIG_TXT"
ok "dtoverlay=dwc2,dr_mode=peripheral added to [all] section (Pi 4 / Pi 5 compatible)"

# Load module now, for immediate use without reboot
modprobe libcomposite 2>/dev/null || warn "libcomposite not loadable now, needs reboot"

# ══════════════════════════════════════════════════════════════════════════════
# 3. CLONE / UPDATE REPO
# ══════════════════════════════════════════════════════════════════════════════
SRC_DIR="/tmp/magicbridge-src"

if [[ -d "/opt/magicbridge-repo/.git" ]]; then
    info "Updating existing clone..."
    git -C /opt/magicbridge-repo pull origin "$BRANCH" --ff-only || true
    SRC_DIR="/opt/magicbridge-repo"
elif [[ -d "$(dirname "$0")/src" ]]; then
    # Running from a local clone
    SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
    info "Using local source at $SRC_DIR"
else
    info "Cloning $REPO_URL..."
    rm -rf "$SRC_DIR"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
    ok "Cloned to $SRC_DIR"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 4. INSTALL FILES
# ══════════════════════════════════════════════════════════════════════════════
info "Installing MagicBridge to $INSTALL_DIR..."

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
cp "$SRC_DIR/src/core/mb-lockdown.sh"  /usr/local/bin/mb-lockdown.sh
chmod +x /usr/local/bin/mb-gadget.sh /usr/local/bin/mb-provision.sh /usr/local/bin/mb-lockdown.sh

ok "Files installed"

# ══════════════════════════════════════════════════════════════════════════════
# 5. INITIAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════
info "Writing initial config.json..."
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    # Serial is generated below (not hardcoded here) so a fresh install never
    # ships the same obviously-placeholder value on every device. Same
    # format + seed as magicbridge.py's _gen_serial(0), so mb-gadget.sh's
    # very first boot already applies a realistic serial instead of a
    # generic one that would need to be manually replaced later.
    DEFAULT_SERIAL=$(python3 -c "
import hashlib, random
try:
    mac = open('/sys/class/net/wlan0/address').read().strip().replace(':','')
except Exception:
    try:
        mac = open('/sys/class/net/eth0/address').read().strip().replace(':','')
    except Exception:
        mac = 'dca632c49b00'
seed = int(hashlib.md5((mac + '0').encode()).hexdigest()[:8], 16)
rng = random.Random(seed)
yr, mo = rng.randint(19, 23), rng.randint(1, 12)
print('%02d%02dLK%05d' % (yr, mo, rng.randint(10000, 99999)))
" 2>/dev/null || echo "2103LK48291")

    cat > "$CONFIG_DIR/config.json" <<CONF
{
  "usb": {
    "manufacturer": "Logitech",
    "product":      "USB Keyboard K120",
    "serial":       "$DEFAULT_SERIAL",
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
    # auth.main_password_hash / auth.password_hash are bootstrapped on first
    # run by magicbridge.py and stealth-dashboard.py respectively, defaulting
    # to "magicbridge" and "stealthbridge". Not written here.
    ok "config.json created (USB serial: $DEFAULT_SERIAL)"
else
    ok "config.json already exists, skipping"
fi

chmod 600 "$CONFIG_DIR/config.json"

# ══════════════════════════════════════════════════════════════════════════════
# 6. TLS CERTIFICATE
# ══════════════════════════════════════════════════════════════════════════════
CERT="$CONFIG_DIR/ssl/cert.pem"
KEY="$CONFIG_DIR/ssl/key.pem"

if [[ ! -f "$CERT" ]]; then
    info "Generating self-signed TLS certificate..."
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=magicbridge.local" \
        -addext "subjectAltName=DNS:magicbridge.local,IP:127.0.0.1" \
        2>/dev/null
    chmod 600 "$KEY"
    ok "TLS cert generated (10-year, self-signed)"
else
    ok "TLS cert already exists, skipping"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 7. NGINX
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring nginx..."
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
info "Installing systemd services..."

cp "$SRC_DIR/src/core/mb-gadget.service"         /etc/systemd/system/
cp "$SRC_DIR/src/core/magicbridge.service"        /etc/systemd/system/
cp "$SRC_DIR/src/dashboard/stealth-dashboard.service" /etc/systemd/system/
cp "$SRC_DIR/src/provision/mb-provision.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable mb-gadget
systemctl enable magicbridge
systemctl enable stealth-dashboard
systemctl enable mb-provision

# Note: ustreamer.service is intentionally NOT installed or enabled here.
# video.py starts and manages ustreamer itself as a subprocess (and stops
# any systemd-managed instance it finds), so a separately enabled
# ustreamer.service would fight it for the capture device and port 8081.

# Start gadget immediately if we have a UDC
if ls /sys/class/udc/* &>/dev/null; then
    systemctl restart mb-gadget && ok "USB gadget started" || warn "USB gadget start failed, may need reboot"
else
    warn "No UDC found, USB gadget will start after reboot (needs dtoverlay=dwc2)"
fi

systemctl restart magicbridge       && ok "magicbridge started"       || warn "magicbridge start failed"
systemctl restart stealth-dashboard && ok "stealth-dashboard started" || warn "stealth-dashboard start failed"

# ══════════════════════════════════════════════════════════════════════════════
# 9. FIREWALL
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring firewall (iptables)..."

# Default-deny inbound; allow established, SSH, HTTP, HTTPS
iptables -F INPUT
iptables -P INPUT DROP
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -p tcp --dport 22  -j ACCEPT
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
# 10. SSH
# ══════════════════════════════════════════════════════════════════════════════
info "Confirming SSH password login is enabled..."
SSHD="/etc/ssh/sshd_config"
grep -q "^PasswordAuthentication" "$SSHD" && \
    sed -i "s/^PasswordAuthentication.*/PasswordAuthentication yes/" "$SSHD" || \
    echo "PasswordAuthentication yes" >> "$SSHD"
systemctl restart ssh
ok "SSH password login enabled"

# ══════════════════════════════════════════════════════════════════════════════
# 11. HOSTNAME + mDNS
# ══════════════════════════════════════════════════════════════════════════════
HOSTNAME_NEW="magicbridge"
info "Setting hostname to '$HOSTNAME_NEW'..."
hostnamectl set-hostname "$HOSTNAME_NEW"
if ! grep -q "^127.0.1.1.*$HOSTNAME_NEW" /etc/hosts; then
    sed -i "/^127.0.1.1/d" /etc/hosts
    echo "127.0.1.1  $HOSTNAME_NEW.local  $HOSTNAME_NEW" >> /etc/hosts
fi
systemctl enable avahi-daemon
systemctl restart avahi-daemon
ok "Hostname '$HOSTNAME_NEW.local' active"

# ══════════════════════════════════════════════════════════════════════════════
# 12. TAILSCALE (optional)
# ══════════════════════════════════════════════════════════════════════════════
if ! command -v tailscale &>/dev/null; then
    echo ""
    read -r -t 10 -p "Install Tailscale for remote access? [Y/n]: " TS_ANS || TS_ANS="Y"
    if [[ "${TS_ANS:-Y}" =~ ^[Yy]$ ]]; then
        info "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | bash 2>&1 | tail -5
        systemctl enable tailscaled
        systemctl start tailscaled
        ok "Tailscale installed, run 'tailscale up' to authenticate"
    fi
else
    ok "Tailscale already installed"
    systemctl enable tailscaled 2>/dev/null || true
fi

# ══════════════════════════════════════════════════════════════════════════════
# 13. LOG FILES
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
echo -e "  ${BOLD}KVM password${NC}      magicbridge  (change in panel -> System -> Account)"
echo -e "  ${BOLD}Admin password${NC}    stealthbridge  (change inside the stealth panel)"
echo ""
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "?")
echo -e "  ${BOLD}Local IP${NC}          $LOCAL_IP"
echo ""
warn "A reboot is required to activate USB OTG (dwc2 overlay)."
echo -e "  ${BOLD}sudo reboot${NC}"
echo ""
echo -e "  If this Pi has no saved WiFi network yet, it will boot into a"
echo -e "  setup hotspot named ${BOLD}MagicBridge-Setup${NC}. Connect to it and follow"
echo -e "  the on-screen steps to join your WiFi."
echo ""
echo -e "  After reboot, connect the Pi's USB-C port to the target computer."
echo -e "  Open ${BOLD}https://magicbridge.local/${NC} on any device on the same network."
echo -e "  Change both default passwords on first login."
echo ""
