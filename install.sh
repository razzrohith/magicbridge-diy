#!/bin/bash
# =============================================================
#  MagicBridge Full Installer
#  Raspberry Pi 4, Pi OS Bookworm (64-bit)
#
#  Run as root:
#    curl -fsSL https://raw.githubusercontent.com/razzrohith/magicbridge-diy/main/install.sh | sudo bash
#  Or from a local clone:
#    sudo bash install.sh                 # full functional install (MJPEG video)
#    sudo bash install.sh --with-webrtc   # + build Janus/H.264 WebRTC (~15-30 min)
#    sudo bash install.sh --check         # read-only status report, changes nothing
#
#  What this does:
#    1. Installs all prerequisites via apt (+ luma.oled for the panel)
#    2. Boot overlays: USB OTG (dwc2), C790/TC358743 capture, I2C for OLED
#    3. Clones/updates razzrohith/magicbridge-diy from GitHub
#    4. Installs all components to /opt/magicbridge/ (incl. oled.py + EDID blob)
#    5. Generates a self-signed TLS cert
#    6. Configures nginx, RAM-only (tmpfs) logs, systemd services, firewall
#    7. Enables video capture services (mb-hdmi-init/watch) + OLED (mb-oled)
#    8. Optionally Tailscale and, with --with-webrtc, the Janus WebRTC path
#    9. Sets a realistic per-unit hostname (DESKTOP-XXXXXXX), enables SSH login
#
#  Safe to re-run: every step is idempotent. LUKS at-rest encryption of
#  /etc/magicbridge is an advanced hardening step and is NOT auto-applied
#  (see the closing notes) - everything else the anonymity model needs
#  (RAM-only logs, spoofable identity) is set up here.
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
REPO_URL="https://github.com/razzrohith/magicbridge-diy"
BRANCH="main"
INSTALL_DIR="/opt/magicbridge"
CONFIG_DIR="/etc/magicbridge"
CONFIG_TXT="/boot/firmware/config.txt"; [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

# Flags:
#   --with-webrtc  also build Janus + hardware H.264 WebRTC (LONG: ~15-30 min).
#                  Without it you get the MJPEG video path (WebRTC can be added
#                  later by running src/install_janus_webrtc.sh).
#   --check        read-only status report of every component, then exit. Safe
#                  to run on a live unit; changes nothing.
WITH_WEBRTC=0; DO_CHECK=0
for a in "$@"; do
  case "$a" in
    --with-webrtc) WITH_WEBRTC=1 ;;
    --check|--doctor) DO_CHECK=1 ;;
    *) warn "Unknown argument: $a (valid: --with-webrtc, --check)" ;;
  esac
done

# Idempotent config.txt line setter: drop any prior "<key>" form, then add the
# exact line under [all]. Re-runs never duplicate or leave a stale value.
set_cfgtxt() {   # $1 = ^regex to remove   $2 = exact line to add
  sed -i "\|^$1|d" "$CONFIG_TXT"
  grep -q '^\[all\]' "$CONFIG_TXT" || echo '[all]' >> "$CONFIG_TXT"
  sed -i "/^\[all\]/a $2" "$CONFIG_TXT"
}

# ── Read-only doctor (--check): report state, change nothing ──────────────────
if [[ "$DO_CHECK" == 1 ]]; then
  echo -e "${BOLD}MagicBridge install check — read-only, nothing is modified${NC}\n"
  chk() { if eval "$2" &>/dev/null; then ok "$1"; else warn "$1 — MISSING"; fi; }
  chk "config.txt: dwc2 peripheral (USB HID)"  "grep -q '^dtoverlay=dwc2' '$CONFIG_TXT'"
  chk "config.txt: camera_auto_detect=0"       "grep -q '^camera_auto_detect=0' '$CONFIG_TXT'"
  chk "config.txt: tc358743 (C790 capture)"    "grep -q '^dtoverlay=tc358743' '$CONFIG_TXT'"
  chk "config.txt: i2c_arm (OLED)"             "grep -q '^dtparam=i2c_arm=on' '$CONFIG_TXT'"
  chk "core: magicbridge.py / hid.py / video.py" "test -f $INSTALL_DIR/core/magicbridge.py -a -f $INSTALL_DIR/core/video.py"
  chk "core: oled.py"                          "test -f $INSTALL_DIR/core/oled.py"
  chk "web: index.html"                        "test -f $INSTALL_DIR/web/index.html"
  chk "edid blob (1080p50)"                    "test -f $INSTALL_DIR/edid/mb-edid-1080p50.hex"
  chk "script: mb-hdmi-init.sh"                "test -x /usr/local/bin/mb-hdmi-init.sh"
  chk "RAM-log tmpfs mounted"                  "findmnt -no FSTYPE /var/log/magicbridge-ram | grep -q tmpfs"
  chk "TLS cert"                               "test -f $CONFIG_DIR/ssl/cert.pem"
  chk "nginx site enabled"                     "test -L /etc/nginx/sites-enabled/magicbridge"
  for s in mb-gadget magicbridge stealth-dashboard mb-provision mb-mdns-alias mb-hdmi-init mb-hdmi-watch mb-oled; do
    chk "service enabled: $s" "systemctl is-enabled $s.service"
  done
  chk "WebRTC: janus-webrtc enabled"           "systemctl is-enabled janus-webrtc.service"
  chk "config: /etc/magicbridge on LUKS (optional)" "lsblk -no TYPE \$(findmnt -no SOURCE $CONFIG_DIR 2>/dev/null) 2>/dev/null | grep -q crypt"
  echo ""; info "Check complete — nothing was changed."
  exit 0
fi

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
    avahi-daemon libnss-mdns avahi-utils
    # Firewall
    iptables
    # Misc
    curl jq
)

info "Installing packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${APT_PKGS[@]}" 2>&1 | grep -E "^(Setting up|E:|W:)" || true

# Python packages not in apt (luma.oled drives the SSD1306 status panel)
pip3 install --break-system-packages --quiet \
    aiohttp flask bcrypt luma.oled 2>/dev/null || true

ok "Prerequisites installed"

# ══════════════════════════════════════════════════════════════════════════════
# 2. BOOT OVERLAYS  (USB OTG + C790/TC358743 capture + I2C for OLED)
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring boot overlays in $CONFIG_TXT ..."
# USB OTG peripheral mode = the HID gadget (/dev/hidg*). Idempotent.
set_cfgtxt "dtoverlay=dwc2" "dtoverlay=dwc2,dr_mode=peripheral"
# C790 / TC358743 HDMI->CSI-2 capture. Without these /dev/video0 never appears,
# so there is no video at all. camera_auto_detect MUST be 0 or it fights the
# explicit overlay. tc358743-audio is the (currently upstream-limited) I2S audio
# DAI - harmless to load; see docs/DIY_PROGRESS.md.
set_cfgtxt "camera_auto_detect=" "camera_auto_detect=0"
sed -i '\|^dtoverlay=tc358743|d' "$CONFIG_TXT"     # clears tc358743 AND tc358743-audio
grep -q '^\[all\]' "$CONFIG_TXT" || echo '[all]' >> "$CONFIG_TXT"
sed -i '/^\[all\]/a dtoverlay=tc358743-audio' "$CONFIG_TXT"
sed -i '/^\[all\]/a dtoverlay=tc358743' "$CONFIG_TXT"
# I2C for the SSD1306 OLED status panel.
set_cfgtxt "dtparam=i2c_arm=" "dtparam=i2c_arm=on"
ok "config.txt set: dwc2 + tc358743(+audio) + camera_auto_detect=0 + i2c_arm (idempotent)"

# Load modules now where possible (full effect needs a reboot).
modprobe libcomposite 2>/dev/null || warn "libcomposite not loadable now, needs reboot"
modprobe i2c-dev 2>/dev/null || true

# ══════════════════════════════════════════════════════════════════════════════
# 3. CLONE / UPDATE REPO
# ══════════════════════════════════════════════════════════════════════════════
# Trust /opt/magicbridge-repo for root's git regardless of HOME, so the built-in
# updater's `git pull` (run as root by magicbridge.py) never trips git's
# "dubious ownership" guard. System-wide (/etc/gitconfig), so it always applies.
git config --system --add safe.directory /opt/magicbridge-repo 2>/dev/null || true

SRC_DIR="/tmp/magicbridge-src"

if [[ -d "/opt/magicbridge-repo/.git" ]]; then
    info "Updating existing clone..."
    # This pull can replace THIS SCRIPT underneath the running bash. git swaps
    # the file by rename, so the already-open fd still points at the OLD inode
    # and bash keeps executing the pre-pull text to the end - the newly pulled
    # installer logic never runs. Seen for real: the deployed-commit stamp
    # arrived on disk and was silently skipped, so the run "succeeded" while
    # doing the old thing. Re-exec ourselves if we changed. MB_INSTALL_REEXEC
    # bounds it to exactly one re-exec, so a checksum that keeps differing can
    # never loop.
    _SELF_BEFORE=$(sha256sum "$0" 2>/dev/null | cut -d' ' -f1 || true)
    git -C /opt/magicbridge-repo pull origin "$BRANCH" --ff-only || true
    SRC_DIR="/opt/magicbridge-repo"
    _SELF_AFTER=$(sha256sum "$0" 2>/dev/null | cut -d' ' -f1 || true)
    if [[ -n "$_SELF_BEFORE" && "$_SELF_BEFORE" != "$_SELF_AFTER" && -z "${MB_INSTALL_REEXEC:-}" ]]; then
        info "Installer updated itself - restarting with the new version..."
        export MB_INSTALL_REEXEC=1
        exec bash "$0" "$@"
    fi
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

mkdir -p "$INSTALL_DIR"/{core,web,dashboard,provision,edid}
mkdir -p "$CONFIG_DIR"/ssl

# Core KVM server
cp "$SRC_DIR/src/core/magicbridge.py"  "$INSTALL_DIR/core/"
cp "$SRC_DIR/src/core/hid.py"         "$INSTALL_DIR/core/"
cp "$SRC_DIR/src/core/video.py"       "$INSTALL_DIR/core/"
cp "$SRC_DIR/src/core/oled.py"        "$INSTALL_DIR/core/"   # OLED status panel (mb-oled.service)

# EDID blob: caps ANY source at 1080p50 (the Pi 4B 2-CSI-lane ceiling), applied
# at boot by mb-hdmi-init. Without it the TC358743 has no EDID after reboot and
# the source sends no signal.
cp "$SRC_DIR/src/edid/mb-edid-1080p50.hex" "$INSTALL_DIR/edid/"

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
cp "$SRC_DIR/src/core/mb-mdns-alias.sh" /usr/local/bin/mb-mdns-alias.sh
cp "$SRC_DIR/src/core/mb-hdmi-init.sh" /usr/local/bin/mb-hdmi-init.sh   # C790 EDID/timings at boot + hot-plug
cp "$SRC_DIR/src/core/mb-setup-fan.sh" /usr/local/bin/mb-setup-fan.sh   # optional case-fan helper (run manually with a GPIO pin)
cp "$SRC_DIR/src/core/mb-firstboot.sh"   /usr/local/bin/mb-firstboot.sh   # first-boot install/personalize + OLED guidance
cp "$SRC_DIR/src/core/mb-firstboot-late.sh" /usr/local/bin/mb-firstboot-late.sh  # post-boot part 2: rootfs grow + unique EDID serial
cp "$SRC_DIR/src/core/mb-secret-reset.sh" /usr/local/bin/mb-secret-reset.sh # per-unit secret reset (pre-baked images)
cp "$SRC_DIR/src/core/mb-power-test.sh" /usr/local/bin/mb-power-test.sh # power-path A/B diagnostic (docs/POWER_TESTS.md)
chmod +x /usr/local/bin/mb-gadget.sh /usr/local/bin/mb-provision.sh /usr/local/bin/mb-lockdown.sh \
         /usr/local/bin/mb-mdns-alias.sh /usr/local/bin/mb-hdmi-init.sh /usr/local/bin/mb-setup-fan.sh \
         /usr/local/bin/mb-firstboot.sh /usr/local/bin/mb-firstboot-late.sh /usr/local/bin/mb-secret-reset.sh \
         /usr/local/bin/mb-power-test.sh

# Stage the WebRTC add-on installer so `--with-webrtc` (or a later manual run)
# can build the Janus H.264 path. It is NOT executed here unless --with-webrtc.
cp "$SRC_DIR/src/install_janus_webrtc.sh" "$INSTALL_DIR/install_janus_webrtc.sh" 2>/dev/null || true

ok "Files installed"

# ══════════════════════════════════════════════════════════════════════════════
# 5. INITIAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════
info "Writing initial config.json..."
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    # A real Logitech Unifying Receiver reports no serial number at all
    # (iSerial=0) and exposes a 3rd idle vendor HID interface alongside
    # keyboard+mouse - verified against real device descriptors. So the
    # default identity here matches that: no serial, extra_iface on.
    cat > "$CONFIG_DIR/config.json" <<CONF
{
  "usb": {
    "manufacturer": "Logitech",
    "product":      "USB Receiver",
    "serial":       "",
    "idVendor":     "0x046d",
    "idProduct":    "0xc52b",
    "has_serial":   false,
    "extra_iface":  true
  },
  "video": {
    "device":     "",
    "resolution": "1920x1080",
    "fps":        50,
    "quality":    80,
    "mode":       "auto"
  },
  "mac_persist":   {},
  "mac_autospoof": true,
  "mdns_alias":    "magicbridge",
  "duckdns":       {}
}
CONF
    # auth.main_password_hash / auth.password_hash are bootstrapped on first
    # run by magicbridge.py and stealth-dashboard.py respectively, defaulting
    # to "magicbridge" and "stealthbridge". Not written here.
    ok "config.json created (Logitech Unifying Receiver identity, no serial - matches real hardware)"
else
    # BACKFILL, don't skip. Writing the defaults only when config.json is ABSENT
    # meant a new default key reached fresh flashes ONLY - never an already
    # installed unit. Found live: a unit upgraded through the web UI got every
    # code change but still had no "mdns_alias", so magicbridge.local stayed
    # dead on exactly the headless units the default was added for. Same hole
    # would swallow every future default (video.mode, mac_autospoof, ...).
    #
    # Add MISSING keys only - never overwrite a value that is already there.
    # That is what makes it safe and idempotent: a user who deliberately set
    # mdns_alias:"" for full stealth keeps it (the key exists), auth hashes and
    # saved settings are untouched, and re-running install.sh changes nothing.
    info "config.json exists - backfilling any missing defaults..."
    python3 - "$CONFIG_DIR/config.json" <<'PYBACKFILL' || warn "config backfill skipped (python3 error) - existing config left untouched"
import json, sys

DEFAULTS = {
    "usb": {
        "manufacturer": "Logitech", "product": "USB Receiver", "serial": "",
        "idVendor": "0x046d", "idProduct": "0xc52b",
        "has_serial": False, "extra_iface": True,
    },
    "video": {
        "device": "", "resolution": "1920x1080",
        "fps": 30, "quality": 80, "mode": "auto",
    },
    "mac_persist": {},
    "mac_autospoof": True,
    "mdns_alias": "magicbridge",
    "duckdns": {},
}

path = sys.argv[1]
with open(path) as fh:
    cfg = json.load(fh)

added = []
for key, default in DEFAULTS.items():
    if key not in cfg:
        cfg[key] = default
        added.append(key)
    elif isinstance(default, dict) and isinstance(cfg[key], dict):
        # One level deep is enough for this schema, and keeps the merge
        # predictable - no surprise resurrection of keys a user removed.
        for sub, subdefault in default.items():
            if sub not in cfg[key]:
                cfg[key][sub] = subdefault
                added.append(f"{key}.{sub}")

if added:
    # Write via a temp file + replace so an interrupted upgrade can never leave
    # a truncated config.json behind (that would brick the backend on restart).
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    import os
    os.replace(tmp, path)
    print("  backfilled: " + ", ".join(added))
else:
    print("  nothing missing")
PYBACKFILL
    ok "config.json defaults reconciled"
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
# 6b. RAM-ONLY LOGS  (must exist BEFORE nginx starts — nginx.conf logs here)
# ══════════════════════════════════════════════════════════════════════════════
# MAGICBRIDGE_SYSTEM.md §2: auth/session/nginx logs live in a tmpfs so pulling
# the card exposes no connection IPs/history. nginx.conf and both Python
# backends write here; wiped on reboot/power-loss by design.
info "Setting up RAM-only (tmpfs) log directory..."
# mode=0755, NOT 1777. A world-writable sticky dir holding nginx logs owned by
# www-data trips the kernel's fs.protected_regular (Bookworm default = 2): it
# blocks EVEN ROOT from opening a not-owned file inside a sticky world-writable
# dir, so `nginx -t` fails and a re-install aborts (first install works only
# because the logs don't exist yet). All writers here run as root (magicbridge,
# stealth-dashboard) and nginx's master creates its logs as root before handing
# them to www-data, so 0755 root:root is sufficient - and avoids the trap.
FSTAB_LINE="tmpfs /var/log/magicbridge-ram tmpfs defaults,noatime,mode=0755,size=32m 0 0"
# Normalize any existing entry (e.g. an older mode=1777 line) to the correct one
# so a re-run self-heals instead of leaving the boot-time mode wrong.
if grep -q '[[:space:]]/var/log/magicbridge-ram[[:space:]]' /etc/fstab; then
    sed -i '\|[[:space:]]/var/log/magicbridge-ram[[:space:]]|d' /etc/fstab
fi
echo "$FSTAB_LINE" >> /etc/fstab
ok "tmpfs fstab entry set (mode=0755) for /var/log/magicbridge-ram"
mkdir -p /var/log/magicbridge-ram
mountpoint -q /var/log/magicbridge-ram || mount /var/log/magicbridge-ram 2>/dev/null || true
# A tmpfs remount won't change its mode, but chmod does - and that's what fixes
# the already-mounted (possibly 1777) dir on an update so nginx -t can pass now.
chmod 0755 /var/log/magicbridge-ram 2>/dev/null || true

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
cp "$SRC_DIR/src/core/mb-mdns-alias.service"     /etc/systemd/system/
cp "$SRC_DIR/src/core/mb-hdmi-init.service"      /etc/systemd/system/   # C790 EDID + timings at boot
cp "$SRC_DIR/src/core/mb-hdmi-watch.service"     /etc/systemd/system/   # re-arms EDID on hot-plug
cp "$SRC_DIR/src/core/mb-oled.service"           /etc/systemd/system/   # SSD1306 status panel

systemctl daemon-reload
systemctl enable mb-gadget
systemctl enable magicbridge
systemctl enable stealth-dashboard
systemctl enable mb-provision
systemctl enable mb-mdns-alias
systemctl enable mb-hdmi-init
systemctl enable mb-hdmi-watch
systemctl enable mb-oled

# First-boot service: copied but deliberately NOT enabled here. Enabling it
# would make THIS (already-installed) unit re-run first-boot — including a
# per-unit secret reset — on its next boot. Only the image builder
# (src/provision/build-image.sh) enables it and removes the done-flag, arming a
# freshly-flashed card. Marking the flag here keeps a normal install inert.
cp "$SRC_DIR/src/core/mb-firstboot.service" /etc/systemd/system/
cp "$SRC_DIR/src/core/mb-firstboot-late.service" /etc/systemd/system/

# BOOT SAFETY: /boot/firmware is NOT essential to a running system, but stock
# fstab mounts it with fsck pass 2 and no `nofail` - so a partition left slightly
# inconsistent (e.g. by a resize) blocks the ENTIRE boot: the unit pings but has
# no SSH/services. With nofail it can never hold boot hostage; worst case it just
# isn't mounted. (Lesson ported from the PiKVM sibling's imaging work.)
if grep -qE '^[^#].*[[:space:]]/boot/firmware[[:space:]]' /etc/fstab && \
   ! grep -qE '^[^#].*[[:space:]]/boot/firmware[[:space:]].*nofail' /etc/fstab; then
    sed -i -E '/^[^#].*[[:space:]]\/boot\/firmware[[:space:]]/ s/(vfat[[:space:]]+)([^[:space:]]+)/\1\2,nofail,x-systemd.device-timeout=15s/' /etc/fstab
    ok "/boot/firmware mount made nofail (a bad boot partition can no longer block boot)"
else
    ok "/boot/firmware already nofail (or not in fstab)"
fi
systemctl daemon-reload
# Both markers: a DIRECT install is already personalized, so neither first-boot
# stage may run (they would wipe WiFi / re-randomize the EDID). build-image.sh
# removes both when arming a distributable image.
touch "$CONFIG_DIR/.firstboot-done" "$CONFIG_DIR/.firstboot-late-done"

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
# Realistic, per-unit hostname. The old "magicbridge" hostname was broadcast to
# the LAN via the DHCP hostname option and mDNS - a blatant product tell on any
# router's client list. "DESKTOP-XXXXXXX" is exactly what an ordinary Windows PC
# announces, so the device blends in. Stable across re-runs/updates (kept if it
# already looks realistic); regenerated per unit by mb-secret-reset on a clone.
CUR_HN="$(hostname)"
if [[ "$CUR_HN" =~ ^DESKTOP-[A-Z0-9]{7}$ ]]; then
    HOSTNAME_NEW="$CUR_HN"
else
    # `|| true`: head closing the pipe SIGPIPEs tr (rc 141); under this script's
    # `set -euo pipefail` that would abort the whole install right here.
    HOSTNAME_NEW="DESKTOP-$(tr -dc 'A-Z0-9' </dev/urandom 2>/dev/null | head -c 7 || true)"
fi
info "Setting hostname to '$HOSTNAME_NEW'..."
hostnamectl set-hostname "$HOSTNAME_NEW"
if ! grep -q "^127.0.1.1.*$HOSTNAME_NEW" /etc/hosts; then
    sed -i "/^127.0.1.1/d" /etc/hosts
    echo "127.0.1.1  $HOSTNAME_NEW.local  $HOSTNAME_NEW" >> /etc/hosts
fi
# unmask first: a masked service refuses to start even after `enable`, and
# this exact thing was found masked on a live unit on 2026-07-09 with no
# clear cause - cheap insurance against a fresh install inheriting a masked
# state from whatever image/tooling prepared the SD card.
systemctl unmask avahi-daemon 2>/dev/null || true
systemctl unmask avahi-daemon.socket 2>/dev/null || true
systemctl enable avahi-daemon
systemctl restart avahi-daemon
ok "Hostname '$HOSTNAME_NEW.local' active (blends in as an ordinary PC)"

# mb-mdns-alias reads "mdns_alias" from config ONCE, at start. On an upgrade the
# config backfill can add that key long after the service already ran and exited
# with "no alias configured", so without this restart magicbridge.local silently
# stays dead until the next reboot - which is exactly what happened on the first
# unit to get the backfill. Restart it so a config change takes effect now.
systemctl restart mb-mdns-alias 2>/dev/null || true
if systemctl is-active --quiet mb-mdns-alias; then
    ok "mDNS alias published (reachable by name)"
else
    info "mDNS alias not published (mdns_alias empty = full stealth)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 12. TAILSCALE (optional)
# ══════════════════════════════════════════════════════════════════════════════
if ! command -v tailscale &>/dev/null; then
    if [[ -t 0 ]]; then
        echo ""
        read -r -t 10 -p "Install Tailscale for remote access? [Y/n]: " TS_ANS || TS_ANS="Y"
    else
        TS_ANS="n"   # non-interactive (self-update / firstboot): don't install unattended
    fi
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
# 13. OPERATIONAL LOG  (RAM-only logs were set up in 6b, before nginx)
# ══════════════════════════════════════════════════════════════════════════════
# The provisioning log is operational (not connection data) and stays on disk
# so `tail` works across reboots; the sensitive auth/session/nginx logs are the
# tmpfs ones from section 6b.
touch /var/log/magicbridge-provision.log 2>/dev/null || true
chmod 640 /var/log/magicbridge-provision.log 2>/dev/null || true

# ══════════════════════════════════════════════════════════════════════════════
# 14. WEBRTC / H.264  (optional — only with --with-webrtc)
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$WITH_WEBRTC" == 1 ]]; then
    info "Building Janus + hardware H.264 WebRTC — this can take 15-30 min..."
    if bash "$INSTALL_DIR/install_janus_webrtc.sh"; then
        systemctl enable janus-webrtc 2>/dev/null || true
        systemctl start  janus-webrtc 2>/dev/null || true
        # Prefer the H.264/WebRTC path; video.py falls back to MJPEG on its own
        # if Janus or the C790 aren't actually available, so this is safe.
        if command -v jq &>/dev/null && [[ -f "$CONFIG_DIR/config.json" ]]; then
            tmp=$(mktemp)
            jq '.video.mode = "h264"' "$CONFIG_DIR/config.json" > "$tmp" \
                && mv "$tmp" "$CONFIG_DIR/config.json" && chmod 600 "$CONFIG_DIR/config.json"
        fi
        ok "WebRTC installed + enabled (video mode set to h264)"
    else
        warn "WebRTC build failed — the MJPEG path still works. Re-run later:"
        warn "  sudo bash $INSTALL_DIR/install_janus_webrtc.sh"
    fi
else
    info "WebRTC not built (MJPEG video works now). Add it anytime with:"
    info "  sudo bash install.sh --with-webrtc   (or bash $INSTALL_DIR/install_janus_webrtc.sh)"
fi

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
warn "A reboot is required to activate the new boot overlays (USB OTG dwc2,"
warn "C790/tc358743 capture, i2c) — video and the OLED won't work until then."
echo -e "  ${BOLD}sudo reboot${NC}"
echo ""
echo -e "  If this Pi has no saved WiFi network yet, it will boot into a"
echo -e "  setup hotspot named ${BOLD}MagicBridge-Setup${NC}. Connect to it and follow"
echo -e "  the on-screen steps to join your WiFi."
echo ""
echo -e "  After reboot, connect BOTH cables to the target computer:"
echo -e "    • HDMI  (target's output) → C790 capture board → Pi CSI ribbon"
echo -e "    • USB-C (Pi OTG port)     → a DATA USB port on the target (keyboard/mouse)"
echo -e "  Open ${BOLD}https://magicbridge.local/${NC} on any device on the same network."
echo -e "  Change both default passwords on first login."
echo ""
echo -e "  ${BOLD}Verify anytime:${NC}  sudo bash install.sh --check   (read-only status)"

# ── Stamp WHAT IS NOW DEPLOYED. Must be the last thing that runs, and only on
# the success path: the web updater pulls the repo BEFORE running this script,
# so if the install dies partway (a shutdown landed mid-run once) the clone is
# already advanced while nothing is deployed. Comparing the clone to origin then
# reports "Up to date" forever with no way to retry. The updater compares THIS
# file to origin instead, so an unfinished install still shows as pending.
if _SHA=$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null); then
    mkdir -p "$CONFIG_DIR" 2>/dev/null
    printf '%s\n' "$_SHA" > "$CONFIG_DIR/.deployed-commit.new" \
      && mv -f "$CONFIG_DIR/.deployed-commit.new" "$CONFIG_DIR/.deployed-commit" \
      && ok "Deployed commit recorded: ${_SHA:0:7}"
fi
echo -e "  ${BOLD}Advanced (optional, manual):${NC} LUKS at-rest encryption of"
echo -e "  /etc/magicbridge is NOT auto-applied — a botched setup can lock out"
echo -e "  config. See MAGICBRIDGE_SYSTEM.md §2. The rest of the anonymity model"
echo -e "  (RAM-only logs, spoofable USB/MAC/EDID identity) is already active."
echo ""
