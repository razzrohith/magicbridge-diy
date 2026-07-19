#!/bin/bash
# ============================================================
#  MagicBridge image "arm for distribution" tool
#
#  Takes a MagicBridge base .img (see docs/IMAGE_BUILD.md for how to make one -
#  either a clone of a configured unit or a pi-gen build) and arms it so that,
#  when flashed with Raspberry Pi Imager and booted, it runs first-boot
#  personalization (regenerate per-unit secrets) + WiFi provisioning, guided on
#  the OLED. Optionally shrinks the result with pishrink.
#
#  Run on a LINUX host (WSL2/Ubuntu, a Pi, or Docker) as root - it needs loop
#  devices + mount. It CANNOT run on Windows directly, and not in a sandbox.
#
#  Usage:
#    sudo bash build-image.sh magicbridge-base.img [magicbridge-dist.img]
# ============================================================
set -euo pipefail
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
ok(){ echo -e "${GRN}✓${NC} $*"; }; info(){ echo -e "→ $*"; }
warn(){ echo -e "${YEL}⚠${NC} $*"; }; die(){ echo -e "${RED}✗${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (needs loop mount): sudo bash $0 ..."
BASE="${1:-}"; [[ -f "$BASE" ]] || die "Usage: sudo bash $0 <base.img> [out.img]"
OUT="${2:-}"
command -v losetup >/dev/null || die "losetup not found (install util-linux)"

# Work on a copy so the base image is never mutated.
if [[ -n "$OUT" && "$OUT" != "$BASE" ]]; then
    info "Copying $BASE -> $OUT ..."; cp --reflink=auto "$BASE" "$OUT"
else
    OUT="$BASE"; warn "Editing $BASE in place (pass an output name to keep the base)"
fi

MNT=$(mktemp -d); LOOP=""
cleanup(){ mountpoint -q "$MNT" && umount "$MNT" 2>/dev/null || true
           [[ -n "$LOOP" ]] && losetup -d "$LOOP" 2>/dev/null || true
           rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT

info "Attaching image..."
LOOP=$(losetup --show -fP "$OUT")           # -P exposes partitions (p1 boot, p2 root)
ROOTPART="${LOOP}p2"
[[ -e "$ROOTPART" ]] || die "No root partition ${ROOTPART} - is this a Pi OS image?"
mount "$ROOTPART" "$MNT"
[[ -d "$MNT/opt/magicbridge" ]] || warn "No /opt/magicbridge in the image - is MagicBridge actually installed in this base? (net-install images will install on first boot instead)"

info "Arming first-boot personalization..."
# 1. Remove the 'already set up' flag so mb-firstboot runs on the flashed unit.
rm -f "$MNT/etc/magicbridge/.firstboot-done"
# 2. Enable mb-firstboot.service (offline: create the wants-symlink directly).
if [[ -f "$MNT/etc/systemd/system/mb-firstboot.service" ]]; then
    mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
    ln -sf ../mb-firstboot.service \
        "$MNT/etc/systemd/system/multi-user.target.wants/mb-firstboot.service"
    ok "mb-firstboot.service enabled in the image"
else
    warn "mb-firstboot.service missing from image - run install.sh in the base first"
fi
# 3. Wipe any secrets already present in the base (belt-and-suspenders; the
#    first-boot secret-reset regenerates them, but don't ship them at all).
rm -f "$MNT"/etc/ssh/ssh_host_* \
      "$MNT"/etc/NetworkManager/system-connections/*.nmconnection \
      "$MNT"/var/lib/tailscale/tailscaled.state \
      "$MNT"/etc/machine-id 2>/dev/null || true
: > "$MNT/etc/machine-id" 2>/dev/null || true
rm -f "$MNT"/var/log/magicbridge-ram/* "$MNT"/var/log/magicbridge-firstboot.log 2>/dev/null || true
# Also clear the spoofed-MAC identity, so no two flashed units ever share a MAC
# (a shared MAC would cross-link units + collide on one LAN). First boot then
# generates a fresh per-unit vendor MAC. Belt-and-suspenders with mb-secret-reset.
rm -f "$MNT"/etc/NetworkManager/conf.d/00-mb-macspoof.conf 2>/dev/null || true
if [[ -f "$MNT/etc/magicbridge/config.json" ]] && command -v python3 >/dev/null; then
    python3 - "$MNT/etc/magicbridge/config.json" <<'PY' 2>/dev/null || true
import json,sys
p=sys.argv[1]
try: c=json.load(open(p))
except Exception: c={}
c["mac_persist"]={}          # empty -> first boot picks a unique vendor MAC
c.setdefault("video",{})["mode"]="auto"   # detect C790/CSI vs USB on each unit
json.dump(c,open(p,"w"),indent=2)
PY
fi
ok "Stripped baked secrets + MAC identity from the image"

sync; umount "$MNT"; losetup -d "$LOOP"; LOOP=""; trap - EXIT; rmdir "$MNT"

# 4. Optional shrink so the .img is small + flashes fast.
if command -v pishrink.sh >/dev/null 2>&1; then
    info "Shrinking with pishrink..."; pishrink.sh "$OUT" && ok "Shrunk"
else
    warn "pishrink.sh not found - image is full-card size. Get it from"
    warn "  https://github.com/Drewsif/PiShrink  then re-run, or shrink manually."
fi

echo ""; ok "Armed image ready: $OUT"
echo "  Flash it with Raspberry Pi Imager → 'Use custom' → select this .img."
echo "  On first boot the unit personalizes itself (OLED: 'please wait') and"
echo "  then shows 'Join hotspot MagicBridge-Setup' for WiFi setup."
