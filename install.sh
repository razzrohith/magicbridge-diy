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
  chk "config.txt: i2c_arm_baudrate (OLED)"    "grep -q '^dtparam=i2c_arm_baudrate=' '$CONFIG_TXT'"
  chk "core: magicbridge.py / hid.py / video.py" "test -f $INSTALL_DIR/core/magicbridge.py -a -f $INSTALL_DIR/core/video.py"
  chk "core: oled.py"                          "test -f $INSTALL_DIR/core/oled.py"
  chk "web: index.html"                        "test -f $INSTALL_DIR/web/index.html"
  chk "edid blob (1080p50)"                    "test -f $INSTALL_DIR/edid/mb-edid-1080p50.hex"
  chk "script: mb-hdmi-init.sh"                "test -x /usr/local/bin/mb-hdmi-init.sh"
  chk "RAM-log tmpfs mounted"                  "findmnt -no FSTYPE /var/log/magicbridge-ram | grep -q tmpfs"
  chk "journald RAM-only (Storage=volatile)"   "grep -qs '^Storage=volatile' /etc/systemd/journald.conf.d/*.conf"
  chk "no persistent journal on the card"      "[ ! -d /var/log/journal ]"
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
# Tolerate a failing refresh. Under `set -euo pipefail` a bare apt-get update
# aborts the WHOLE installer on an unreachable mirror, an expired key or a
# captive portal - and because api_update has already advanced the clone by
# this point, install.sh never reaches its deployed-commit stamp, so the panel
# re-offers the same upgrade forever and it fails identically every time.
# Every apt-get install below already tolerates failure.
apt-get update -qq || warn "apt refresh failed (offline or bad mirror) - continuing with cached lists"

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
    # WiFi / network (iw: read/set 802.11 power-save; wireless-tools for iwconfig)
    network-manager wireless-tools wpasupplicant iw
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
# Slow the OLED's I2C bus to 50kHz. The default 100kHz was marginal on this unit's
# OLED wiring and dropped out intermittently (repeated "I2C device not found on
# 0x3C", blanking the panel); 50kHz makes the link reliable and a status display
# needs no speed. (Key "dtparam=i2c_arm_baudrate=" does not collide with the
# "dtparam=i2c_arm=" set above.)
set_cfgtxt "dtparam=i2c_arm_baudrate=" "dtparam=i2c_arm_baudrate=50000"

# ── Video pipeline memory: match the settings real PiKVM ships ────────────────
# Taken from a stock PiKVM image's config.txt (dtoverlay=cma,cma-128 + gpu_mem=128,
# and notably NO vc4-kms-v3d). All three are Pi-internal: the target sees nothing,
# no EDID/USB/HDMI behaviour changes.
#
# 1. vc4-kms-v3d is the DESKTOP display driver for the Pi's own HDMI OUTPUT. A
#    headless KVM never uses it, and it takes over the video pipeline the
#    hardware JPEG/H.264 encoders need. PiKVM deliberately does not load it.
# 2. Removing it drops the CMA pool to a 64 MB default (that overlay was what
#    sized it). CMA is where capture/encode buffers are allocated and a 1080p
#    buffer is ~4 MB, so 64 MB is tight enough that an allocation can fail
#    mid-frame - which is exactly how partial/garbage (green) frames are
#    produced. Measured on the unit: 512 MB before, 64 MB after removal with
#    only 18 MB free. PiKVM sizes it explicitly for this reason; 256 MB here
#    since this board has 8 GB RAM (PiKVM sized 128 for a 1-2 GB CM4).
# 3. gpu_mem was auto-assigned 76 MB; the hardware codecs want PiKVM's 128 MB.
set_cfgtxt "dtoverlay=cma," "dtoverlay=cma,cma-256"
set_cfgtxt "gpu_mem=" "gpu_mem=128"
# Comment out rather than delete, so the original is still visible on the card.
if grep -qE '^\s*dtoverlay=vc4-kms-v3d' "$CONFIG_TXT" 2>/dev/null; then
    sed -i 's|^\s*dtoverlay=vc4-kms-v3d|#dtoverlay=vc4-kms-v3d   # MagicBridge: headless KVM, frees the video pipeline|' "$CONFIG_TXT"
fi
if grep -qE '^\s*max_framebuffers=' "$CONFIG_TXT" 2>/dev/null; then
    sed -i 's|^\s*max_framebuffers=|#max_framebuffers=|' "$CONFIG_TXT"
fi
ok "config.txt set: dwc2 + tc358743(+audio) + camera_auto_detect=0 + i2c_arm + i2c_baud + cma-256/gpu_mem=128 (idempotent)"

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
# Give THIS install a unique EDID monitor serial right here (B6), so every
# install path - direct curl|bash AND net-install-via-mb-firstboot - ships a
# per-unit serial instead of the shared baked 0x00000001. The identity stays a
# genuine "DELL P2419H"; only bytes 12-15 (+ checksum) change. mb-firstboot-late
# still re-randomizes on the distributable-image path as a safety net.
python3 - "$INSTALL_DIR/edid/mb-edid-1080p50.hex" <<'PY' 2>/dev/null || true
import random, re, sys
p=sys.argv[1]; b=[int(x,16) for x in re.findall(r'[0-9a-fA-F]{2}', open(p).read())]
if len(b) >= 128:
    ser=random.randint(0x01000000, 0xfffffffe)
    b[12],b[13],b[14],b[15]=ser&0xFF,(ser>>8)&0xFF,(ser>>16)&0xFF,(ser>>24)&0xFF
    b[127]=(256-(sum(b[0:127])%256))%256
    assert sum(b[0:128])%256==0
    with open(p,"w",newline="\n") as f:
        for blk in (b[:128], b[128:256]):
            if not blk: continue
            for i in range(0,len(blk),16): f.write(" ".join("%02x"%x for x in blk[i:i+16])+"\n")
            f.write("\n")
    print("EDID serial personalized -> 0x%08x" % ser)
PY

# Web UI
cp "$SRC_DIR/src/web/index.html"      "$INSTALL_DIR/web/"
[[ -d "$SRC_DIR/src/web/static" ]] && \
    cp -r "$SRC_DIR/src/web/static"   "$INSTALL_DIR/web/"

# Stealth dashboard
cp "$SRC_DIR/src/dashboard/stealth-dashboard.py" "$INSTALL_DIR/dashboard/"
# mb_edidconf.py: imported by the dashboard for runtime monitor-identity (EDID)
# apply/validate. Was never installed by install.sh, so on a curl|bash unit the
# dashboard's EDID feature silently errored ("mb_edidconf module not installed").
cp "$SRC_DIR/src/dashboard/mb_edidconf.py" "$INSTALL_DIR/dashboard/"

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
    "_comment":   "fps/quality are the MJPEG levers and MUST stay conservative: MJPEG has no inter-frame compression, so 1080p at fps 50 / quality 80 is about 58 Mbit/s, which no real remote link can carry and which saturates even a good LAN. Measured at 1080p: q12 = 96 KB per frame, so 20 fps is about 16 Mbit/s. fps must also divide the source refresh evenly (50Hz -> 25/20/10/5) or frames arrive unevenly. bitrate is the H.264/WebRTC lever, unused by MJPEG.",
    "fps":        20,
    "quality":    12,
    "bitrate":    4000,
    "mode":       "mjpeg"
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
        "fps": 20, "quality": 12, "mode": "mjpeg",
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

# ── Per-unit passwords ────────────────────────────────────────────────────────
# Without this, a DIY install left auth.* unwritten and the two services
# bootstrapped themselves to "magicbridge" / "stealthbridge" - passwords
# published in this very repo. nginx proxies /stealth/ on :443, so the :7777
# DROP does not help: anyone on the same WiFi could open the admin panel with a
# password they could look up, then rotate the USB/MAC/EDID identity or read
# back saved WiFi PSKs. That is a direct break of the stealth model, so the
# DIY path now does what the flashed image already did.
#
# Only ever fills a slot that is EMPTY or still on a known public default, so
# a self-update (MB_SELF_UPDATE=1) can never rotate a password out from under
# a running unit. Both slots get the same value, matching mb-secret-reset.sh.
#
# One nuance: an EMPTY slot is always safe to fill (nobody has ever signed in
# with it). A slot that still holds a published default is only rotated when a
# human is watching the terminal - during a background self-update, silently
# changing a working password would strand the owner exactly the way the
# firewall-flush bug did. Those units get the banner in the panel instead.
MB_GEN_PW=""
if command -v python3 >/dev/null 2>&1; then
    MB_GEN_PW="$(MB_SELF_UPDATE="${MB_SELF_UPDATE:-0}" python3 - "$CONFIG_DIR/config.json" <<'PYPW'
import hashlib, json, os, secrets, string, sys
p = sys.argv[1]
try:
    c = json.load(open(p))
except Exception:
    sys.exit(0)
a = c.setdefault("auth", {})

def _mk(s):
    try:
        import bcrypt
        return bcrypt.hashpw(s.encode(), bcrypt.gensalt()).decode()
    except Exception:
        return "sha256:" + hashlib.sha256(s.encode()).hexdigest()

SELF_UPDATE = os.environ.get("MB_SELF_UPDATE") == "1"

def _needs_pw(h, word):
    """Empty slot -> always fill. Published default -> only when a human is
    watching, so an unattended update never changes a password in silence."""
    if not h:
        return True
    if SELF_UPDATE:
        return False
    if h == "sha256:" + hashlib.sha256(word.encode()).hexdigest():
        return True
    try:
        import bcrypt
        return bcrypt.checkpw(word.encode(), h.encode())
    except Exception:
        return False

need_main = _needs_pw(a.get("main_password_hash"), "magicbridge")
need_adm  = _needs_pw(a.get("password_hash"),      "stealthbridge")
if not (need_main or need_adm):
    sys.exit(0)

alpha = string.ascii_letters + string.digits
pw = "".join(secrets.choice(alpha) for _ in range(12))
h = _mk(pw)
if need_main:
    a["main_password_hash"] = h
    a.setdefault("main_secret_key", secrets.token_hex(32))
if need_adm:
    a["password_hash"] = h
    a.setdefault("secret_key", secrets.token_hex(32))
# Temp file + replace, matching the PYBACKFILL block above. A plain
# open(p,"w") truncates config.json the instant it opens, so an interrupted
# write (power loss, ENOSPC, Ctrl-C) leaves it unparseable. That is not a
# recoverable inconvenience here: stealth-dashboard.py's _load() swallows the
# parse error and returns {}, then _ensure_defaults writes back a config
# holding ONLY auth.*, silently discarding the USB HID identity, the MAC
# spoof, the EDID profile and the mDNS alias. The device would come back up
# detectable, with the installer still reporting success.
_tmp = p + ".new"
with open(_tmp, "w") as _fh:
    json.dump(c, _fh, indent=2)
os.replace(_tmp, p)
os.chmod(p, 0o600)   # os.replace takes the temp file's mode, not the old one
# Line 1 = the password, line 2 = WHICH logins it actually opens. Only one slot
# can need filling (an owner who set the web password but left the admin panel
# on its default), and telling them "MagicBridge web login" for a password that
# only opens /stealth/ would recreate the exact locked-out dead end this block
# exists to prevent.
print(pw)
print("both" if (need_main and need_adm) else ("main" if need_main else "admin"))
PYPW
)" || MB_GEN_PW=""
    # Belt and braces after python rewrote the file. Never allowed to be the
    # thing that aborts an install under `set -e`: the authoritative chmod is
    # the one a few lines above, and if config.json were genuinely missing the
    # script would already have died there.
    chmod 600 "$CONFIG_DIR/config.json" 2>/dev/null || true
    MB_GEN_SLOTS="$(printf '%s\n' "$MB_GEN_PW" | sed -n 2p)"
    MB_GEN_PW="$(printf '%s\n' "$MB_GEN_PW" | sed -n 1p)"
fi
MB_GEN_SLOTS="${MB_GEN_SLOTS:-both}"

if [[ -n "$MB_GEN_PW" ]]; then
    # Same escape hatch the image uses: the FAT boot partition is the only
    # thing a headless owner can read by pulling the card, and the login page
    # points at this exact filename.
    case "$MB_GEN_SLOTS" in
        main)  MB_PW_URL="https://magicbridge.local/  (or the IP on the OLED)"
               MB_PW_NOTE="This opens the web UI only. Your admin panel password is unchanged." ;;
        admin) MB_PW_URL="https://magicbridge.local/stealth/"
               MB_PW_NOTE="This opens the ADMIN PANEL only. Your web UI password is unchanged." ;;
        *)     MB_PW_URL="https://magicbridge.local/  (or the IP on the OLED)"
               MB_PW_NOTE="The same password opens the admin panel at /stealth/." ;;
    esac
    MB_BOOTP=/boot/firmware; [ -d "$MB_BOOTP" ] || MB_BOOTP=/boot
    if [ -d "$MB_BOOTP" ]; then
        { echo "MagicBridge login";
          echo "URL : ${MB_PW_URL}";
          echo "User: (none)";
          echo "Password: ${MB_GEN_PW}"; echo;
          echo "${MB_PW_NOTE}";
          echo "Change it after first login. Delete this file once you have it."; echo;
          echo "NOTE: your browser will warn the connection is not private. That";
          echo "is expected on a private device with a self-signed certificate:";
          echo "choose Advanced, then Proceed."; } \
          > "$MB_BOOTP/magicbridge-password.txt" 2>/dev/null || true
        chmod 600 "$MB_BOOTP/magicbridge-password.txt" 2>/dev/null || true
        sync
    fi
    ok "Generated a per-unit password (shown at the end of this install)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# 6. TLS CERTIFICATE
# ══════════════════════════════════════════════════════════════════════════════
CERT="$CONFIG_DIR/ssl/cert.pem"
KEY="$CONFIG_DIR/ssl/key.pem"

if [[ ! -f "$CERT" ]]; then
    info "Generating self-signed TLS certificate..."
    # B5: CN = this unit's hostname, not a hardcoded "magicbridge.local". A
    # fleet-wide identical branded CN is a pre-auth TLS beacon that outs every
    # clone as MagicBridge to any ssl-cert scan. magicbridge.local stays a SAN
    # so browsing to it still validates. (mb-secret-reset regenerates this
    # per-unit on first boot of a flashed image; this is the install-time cert.)
    CN_HOST="$(hostname 2>/dev/null)"; [ -n "$CN_HOST" ] || CN_HOST="localhost"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=${CN_HOST}" \
        -addext "subjectAltName=DNS:${CN_HOST}.local,DNS:magicbridge.local,IP:127.0.0.1" \
        2>/dev/null
    chmod 600 "$KEY"
    ok "TLS cert generated (10-year, self-signed, CN=${CN_HOST})"
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
# nofail so a tmpfs mount failure (size typo, extreme low-RAM) degrades to
# writing on the rootfs dir instead of failing local-fs.target and stalling a
# headless boot with no SSH - same boot-survival rationale as /boot/firmware.
FSTAB_LINE="tmpfs /var/log/magicbridge-ram tmpfs defaults,noatime,nofail,mode=0755,size=32m 0 0"
# Normalize any existing entry (e.g. an older mode=1777 line) to the correct one
# so a re-run self-heals instead of leaving the boot-time mode wrong.
if grep -q '[[:space:]]/var/log/magicbridge-ram[[:space:]]' /etc/fstab; then
    sed -i '\|[[:space:]]/var/log/magicbridge-ram[[:space:]]|d' /etc/fstab
fi
echo "$FSTAB_LINE" >> /etc/fstab
ok "tmpfs fstab entry set (mode=0755) for /var/log/magicbridge-ram"

# ── journald must be RAM-only too ────────────────────────────────────────────
# The app logs every connection (client IP, User-Agent, timestamps) through the
# root logger, and systemd sends service stdout to the journal. Putting the
# app's own logs on a tmpfs achieves nothing if journald is persisting the same
# information to /var/log/journal on the SD card - pulling the card would still
# reveal a full connection history, which is exactly what the anonymity model
# forbids. Storage=volatile keeps the journal in /run (RAM, wiped on power off).
# ForwardToSyslog=no stops a second copy reaching /var/log/syslog if rsyslog is
# ever installed. Removing an existing /var/log/journal matters because
# journald's default Storage=auto persists *whenever that directory exists*.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/00-magicbridge-volatile.conf <<'JCONF'
# MagicBridge: keep the journal in RAM. See the anonymity model - nothing that
# identifies usage, location or peers may persist on the SD card.
[Journal]
Storage=volatile
ForwardToSyslog=no
RuntimeMaxUse=32M
JCONF
rm -rf /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald 2>/dev/null || true
ok "journald pinned to RAM (Storage=volatile, no syslog forwarding)"
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

# Start gadget immediately if we have a UDC.
# S7: during a self-update (MB_SELF_UPDATE=1) do NOT restart an already-running
# gadget - that re-enumerates the spoofed USB device on the target (a device
# remove/insert it logs) and drops the operator's live HID. A routine update
# never changes the gadget config; a real gadget change still applies on the
# next reboot. Fresh installs / manual runs restart as before.
if ls /sys/class/udc/* &>/dev/null; then
    if [[ "${MB_SELF_UPDATE:-0}" == "1" ]] && systemctl is-active --quiet mb-gadget; then
        ok "USB gadget already running - left as-is (self-update, avoid re-enumerating the target)"
    else
        systemctl restart mb-gadget && ok "USB gadget started" || warn "USB gadget start failed, may need reboot"
    fi
else
    warn "No UDC found, USB gadget will start after reboot (needs dtoverlay=dwc2)"
fi

systemctl restart magicbridge       && ok "magicbridge started"       || warn "magicbridge start failed"
systemctl restart stealth-dashboard && ok "stealth-dashboard started" || warn "stealth-dashboard start failed"

# ══════════════════════════════════════════════════════════════════════════════
# 9. FIREWALL
# ══════════════════════════════════════════════════════════════════════════════
info "Configuring firewall (iptables)..."

# PRESERVE an active Tailscale-only lockdown across this rebuild. The web
# updater re-runs install.sh for any structural change; this section flushes
# INPUT and rebuilds "allow 80/443 from anywhere", which silently wiped
# mb-lockdown's rules AND overwrote the persisted rules.v4 without them - so a
# routine update quietly re-exposed the internal-only web UI + stealth admin
# panel to the whole LAN. Capture the state before the flush, restore it after.
WAS_LOCKED=0
if iptables -C INPUT -p tcp --dport 443 -m comment --comment mb-lockdown -j DROP 2>/dev/null; then
    WAS_LOCKED=1
    info "Tailscale-only lockdown is active - will re-apply after firewall rebuild"
fi

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
iptables -A INPUT -p tcp --dport 8188 -j DROP     # Janus WebRTC WS: internal only
iptables -A INPUT -p tcp --dport 8088 -j DROP     # Janus WebRTC HTTP: internal only

# WebRTC media. install_janus_webrtc.sh pins Janus's RTP range to 20000-20100
# and opens it here, because the INPUT policy is DROP and leaning on the
# conntrack ESTABLISHED path is fragile (it only holds once Janus's own
# outbound connectivity checks have created the entry). The "iptables -F INPUT"
# above wipes that rule, and this file is saved to rules.v4 below, so WITHOUT
# this block an in-UI "Full upgrade" silently and permanently deletes the
# WebRTC media allow, recoverable only over SSH. Keep the range in sync with
# rtp_port_range in /opt/janus/etc/janus/janus.jcfg.
if [[ -x /opt/janus/bin/janus ]] \
   || systemctl list-unit-files 2>/dev/null | grep -q '^janus-webrtc\.service'; then
    iptables -A INPUT -p udp --dport 20000:20100 -j ACCEPT
    info "WebRTC media allow preserved (UDP 20000-20100)"
fi

# IPv6 firewall (S8): nginx also listens on [::]:443, and Janus/RaspiOS bring up
# IPv6. Without matching ip6tables the ip6tables INPUT policy defaults to ACCEPT,
# so the web UI (and internal ports) would be reachable over IPv6 with NO
# firewall - and mb-lockdown's "Tailscale-only" would be bypassable over v6.
# Mirror the v4 policy. (Backends bind 127.0.0.1 only, so they stay loopback-safe,
# but nginx :: is real.)
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -F INPUT
    ip6tables -P INPUT DROP
    ip6tables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A INPUT -i lo -j ACCEPT
    ip6tables -A INPUT -p ipv6-icmp -j ACCEPT                 # NDP/ping6 - v6 needs this
    ip6tables -A INPUT -p tcp --dport 22  -j ACCEPT
    ip6tables -A INPUT -p tcp --dport 80  -j ACCEPT
    ip6tables -A INPUT -p tcp --dport 443 -j ACCEPT
    ip6tables -A INPUT -p udp --dport 5353 -j ACCEPT          # mDNS
    for p in 7777 8080 8081 8188 8088; do ip6tables -A INPUT -p tcp --dport "$p" -j DROP; done
fi

# Persist rules across reboots
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iptables-persistent 2>&1 | grep -E "^(Setting|E:|W:)" || true
iptables-save > /etc/iptables/rules.v4
command -v ip6tables-save >/dev/null 2>&1 && ip6tables-save > /etc/iptables/rules.v6 || true
ok "Firewall configured (IPv4 + IPv6)"

# Re-apply the lockdown we found active, so an update can't silently re-expose
# the admin surface. mb-lockdown.sh re-inserts the Tailscale-only rules at the
# top of INPUT and re-saves rules.v4 including them.
if [[ "$WAS_LOCKED" == "1" && -x /usr/local/bin/mb-lockdown.sh ]]; then
    /usr/local/bin/mb-lockdown.sh on >/dev/null 2>&1 \
        && ok "Re-applied Tailscale-only lockdown (was active before update)" \
        || warn "Could not re-apply lockdown - run: sudo mb-lockdown.sh on"
fi

# ── Disable Wi-Fi power-save (latency jitter) ─────────────────────────────────
# The Pi's brcmfmac Wi-Fi ships power-save ON: the radio parks between packets
# and wakes in 100+ ms, so RTT spikes from a few ms to 100-140ms with 0% packet
# loss - visible video stutter and an inflated WebRTC jitter buffer. (Measured
# by the PiKVM sibling on the same Wi-Fi silicon.) A NetworkManager drop-in
# disables it for every Wi-Fi connection, persistently, with no extra package.
NM_PS="/etc/NetworkManager/conf.d/wifi-powersave-off.conf"
if [[ ! -f "$NM_PS" ]] || ! grep -q "wifi.powersave = 2" "$NM_PS" 2>/dev/null; then
    mkdir -p /etc/NetworkManager/conf.d
    printf '[connection]\nwifi.powersave = 2\n' > "$NM_PS"   # 2 = disable
    systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager 2>/dev/null || true
    ok "Wi-Fi power-save disabled (latency jitter fix)"
fi

# ── Wi-Fi latency dispatcher: enforce power-save OFF live + fq_codel on wlan0 ──
# The NM drop-in above sets the policy, but a `reload` does not re-apply it to an
# already-active connection, so the radio can keep parking until the next
# reconnect. This dispatcher forces it OFF for real on every wlan0 up/change, and
# adds fq_codel so a burst of H.264 cannot balloon the Tx queue (bufferbloat),
# which shows up on a weak link as green bands that crawl and a cursor that
# sticks then catches up. Mirrors the PiKVM sibling's mb-wifi-latency approach.
NM_DISP="/etc/NetworkManager/dispatcher.d/90-mb-wifi-latency"
mkdir -p /etc/NetworkManager/dispatcher.d
cat > "$NM_DISP" <<'DISP'
#!/bin/bash
# MagicBridge: keep wlan0 low-latency for the KVM path (power-save off + fq_codel).
IFACE="$1"; ACTION="$2"
[ "$IFACE" = "wlan0" ] || exit 0
case "$ACTION" in
  up|dhcp4-change|dhcp6-change|connectivity-change)
    iw dev wlan0 set power_save off 2>/dev/null || true
    tc qdisc replace dev wlan0 root fq_codel 2>/dev/null || true
    # DSCP -> WiFi WMM access categories. Video frames leave as large bursts and
    # the small control/WebSocket packets were queuing behind them AND behind
    # their 802.11 retries, which is what the operator feels as cursor lag.
    # Marking small packets EF (WMM Voice) and large ones AF41 (WMM Video) makes
    # the radio service input ahead of frame bursts. Measured on a unit while
    # streaming: 79ms avg / 538ms peak before, 16ms avg / 36ms peak after.
    for l in "0:512 EF" "1200:65535 AF41"; do
      set -- $l
      iptables -t mangle -C OUTPUT -p tcp -m length --length $1 -j DSCP --set-dscp-class $2 2>/dev/null         || iptables -t mangle -A OUTPUT -p tcp -m length --length $1 -j DSCP --set-dscp-class $2 2>/dev/null || true
    done
  ;;
esac
exit 0
DISP
chmod 755 "$NM_DISP"
# Apply once now so the running system benefits without waiting for a reconnect.
iw dev wlan0 set power_save off 2>/dev/null || true
tc qdisc replace dev wlan0 root fq_codel 2>/dev/null || true
for _l in "0:512 EF" "1200:65535 AF41"; do
  set -- $_l
  iptables -t mangle -C OUTPUT -p tcp -m length --length "$1" -j DSCP --set-dscp-class "$2" 2>/dev/null     || iptables -t mangle -A OUTPUT -p tcp -m length --length "$1" -j DSCP --set-dscp-class "$2" 2>/dev/null || true
done
ok "Wi-Fi latency dispatcher installed (power-save off + fq_codel + control-packet QoS)"

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
        # Deliberately does NOT flip video.mode to h264 any more. Installing the
        # WebRTC stack should make H.264 AVAILABLE, not silently move the capture
        # pipeline onto it. On a Pi 4 the SoC H.264 M2M encoder corrupts
        # high-contrast repeating patterns after 10-20 seconds
        # (raspberrypi/linux#5180, still open, and the I-frame does not repair
        # it), and a KVM screen is exactly that content. Flipping the mode here
        # is one of the paths that put units onto the broken encoder without
        # anyone choosing it. Select WebRTC in the web UI, or set
        # video.mode = "h264" in config.json, to opt in deliberately.
        ok "WebRTC installed + enabled (capture stays on MJPEG; pick WebRTC in the UI to use it)"
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
if [[ -n "$MB_GEN_PW" ]]; then
echo -e "  ${BOLD}${GREEN}Password${NC}          ${BOLD}${MB_GEN_PW}${NC}   (unique to this unit)"
echo -e "                    ${MB_PW_NOTE}"
echo -e "                    also saved to ${MB_BOOTP}/magicbridge-password.txt"
else
# Do NOT imply this unit is safe. A background self-update deliberately
# leaves an existing password alone, including a published default, so
# "already had one set" read like reassurance on exactly the units that
# were still wide open. Say which case it actually is.
if [[ -f "$CONFIG_DIR/config.json" ]] && python3 - "$CONFIG_DIR/config.json" <<'PYCHK'
import hashlib, json, sys
try:
    a = json.load(open(sys.argv[1])).get("auth", {})
except Exception:
    sys.exit(1)
h = a.get("main_password_hash", "")
if not h:
    sys.exit(1)
if h == "sha256:" + hashlib.sha256(b"magicbridge").hexdigest():
    sys.exit(0)
try:
    import bcrypt
    sys.exit(0 if bcrypt.checkpw(b"magicbridge", h.encode()) else 1)
except Exception:
    sys.exit(1)
PYCHK
then
echo -e "  ${BOLD}${RED}Password${NC}          ${BOLD}still the public default 'magicbridge'${NC}"
echo -e "                    Change it now: open the UI, Settings, System, Account."
else
echo -e "  ${BOLD}Password${NC}          unchanged (this unit already has its own)"
fi
fi
echo ""
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "?")
echo -e "  ${BOLD}Local IP${NC}          $LOCAL_IP"
echo ""
warn "A reboot is required to activate the new boot overlays (USB OTG dwc2,"
warn "C790/tc358743 capture, i2c) — video and the OLED won't work until then."
echo -e "  ${BOLD}sudo reboot${NC}"
echo ""
echo -e "  If this Pi has no saved WiFi network yet, it will boot into a"
echo -e "  setup hotspot named ${BOLD}Setup-XXXX${NC} (exact name is on the OLED).
  Connect to it and follow"
echo -e "  the on-screen steps to join your WiFi."
echo ""
echo -e "  After reboot, connect BOTH cables to the target computer:"
echo -e "    • HDMI  (target's output) → C790 capture board → Pi CSI ribbon"
echo -e "    • USB-C (Pi OTG port)     → a DATA USB port on the target (keyboard/mouse)"
echo -e "  Open ${BOLD}https://magicbridge.local/${NC} on any device on the same network."
echo -e "  Set your own password on first login (panel -> System -> Account)."
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
