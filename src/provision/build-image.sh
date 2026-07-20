#!/bin/bash
# ============================================================
#  MagicBridge DIY — distributable image tool
#
#  Turns a clone of a working unit into a tiny, self-completing .img that any
#  blank card can be flashed with. Run on a LINUX host (WSL2/Ubuntu, a Pi) as
#  root — needs loop devices + mount. Cannot run on Windows directly.
#
#  MODES
#    (default)   arm     strip every per-unit secret, re-arm first-boot, boot-safety
#    --shrink            zero free space (erase remnants + compress well), then
#                        shrink the LAST partition (= root here) and truncate
#    --compress          xz -T0 -> .img.xz (Imager flashes it natively) + xz -t
#    --verify            assert every strip/setting took; exit 1 if any fail
#
#  Usage:
#    sudo bash build-image.sh base.img [dist.img]     # arm
#    sudo bash build-image.sh --shrink   dist.img
#    sudo bash build-image.sh --compress dist.img
#    sudo bash build-image.sh --verify   dist.img
#
#  DIY vs the PiKVM sibling — deliberately different, do not cross-copy:
#    DIY  = bare Pi OS, 2 partitions, rootfs is the LAST partition, rw rootfs,
#           NetworkManager. Shrinking here resizes ROOT itself, so the first-boot
#           re-grow is boot-critical (pishrink's Pi-OS rc.local hook is the
#           primary path; mb-firstboot-late.sh is the safety net).
#    PiKVM = kvmd/Arch, 4 partitions, root is p3, read-only rootfs; it shrinks a
#           trailing virtual-media partition instead, and pishrink is wrong there.
# ============================================================
set -euo pipefail
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
ok(){ echo -e "${GRN}✓${NC} $*"; }; info(){ echo -e "→ $*"; }
warn(){ echo -e "${YEL}⚠${NC} $*"; }; die(){ echo -e "${RED}✗${NC} $*"; exit 1; }

MODE="arm"
case "${1:-}" in
  --shrink)   MODE="shrink";   shift ;;
  --verify)   MODE="verify";   shift ;;
  --compress) MODE="compress"; shift ;;
  -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
esac

[[ $EUID -eq 0 || "$MODE" == "compress" ]] || die "Run as root (needs loop mount): sudo bash $0 ..."
IMG="${1:-}"; [[ -f "$IMG" ]] || die "Usage: sudo bash $0 [--shrink|--verify|--compress] <image.img> [out.img]"
OUT="${2:-}"
command -v losetup >/dev/null || die "losetup not found (install util-linux)"

LOOP=""; MNT=""; BOOTMNT=""
cleanup(){
  [[ -n "${BOOTMNT:-}" ]] && mountpoint -q "$BOOTMNT" && umount "$BOOTMNT" 2>/dev/null || true
  [[ -n "${MNT:-}" ]] && mountpoint -q "$MNT" && umount "$MNT" 2>/dev/null || true
  [[ -n "$LOOP" ]] && losetup -d "$LOOP" 2>/dev/null || true
  [[ -n "${MNT:-}" ]] && rmdir "$MNT" 2>/dev/null || true
  [[ -n "${BOOTMNT:-}" ]] && rmdir "$BOOTMNT" 2>/dev/null || true
}
trap cleanup EXIT

# ---- attach + identify partitions BY CONTENT, never by index ---------------
# A flashed/cloned card can differ from the golden one; hardcoding p2 is exactly
# how the sibling would have silently stripped nothing. Find the vfat boot part
# and the ext4 root part (the one that actually holds the OS).
ROOTPART=""; BOOTPART=""
attach(){
  info "Attaching $IMG ..."
  LOOP=$(losetup --show -fP "$IMG")
  for p in "${LOOP}"p*; do
    [[ -e "$p" ]] || continue
    local t; t=$(blkid -o value -s TYPE "$p" 2>/dev/null || echo "")
    case "$t" in
      crypto_LUKS)
        die "LUKS partition found ($p). Arming would silently MISS the encrypted
   config and ship a shared key. De-LUKS the image first (see docs/IMAGE_BUILD.md)." ;;
      vfat) [[ -z "$BOOTPART" ]] && BOOTPART="$p" ;;
      ext4)
        local m; m=$(mktemp -d)
        if mount -o ro "$p" "$m" 2>/dev/null; then
          [[ -d "$m/etc" && ( -d "$m/opt/magicbridge" || -d "$m/etc/magicbridge" ) ]] && ROOTPART="$p"
          umount "$m" 2>/dev/null || true
        fi
        rmdir "$m" 2>/dev/null || true ;;
    esac
  done
  [[ -n "$ROOTPART" ]] || die "No MagicBridge root filesystem found in $IMG"
  ok "root=$ROOTPART  boot=${BOOTPART:-<none>}"
}

mount_root(){ MNT=$(mktemp -d); mount "$ROOTPART" "$MNT"; }

# =====================================================================
#  VERIFY — assert the arming actually took. Exit 1 on ANY failure.
# =====================================================================
if [[ "$MODE" == "verify" ]]; then
  attach; mount_root
  R="$MNT"; FAIL=0
  chk(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else echo -e "${RED}✗${NC} $1"; FAIL=$((FAIL+1)); fi; }
  chk "no SSH host keys (regenerated per unit)"      '! ls "$R"/etc/ssh/ssh_host_* 2>/dev/null | grep -q .'
  chk "machine-id blank/absent"                      '[ ! -s "$R/etc/machine-id" ]'
  chk "no saved WiFi profiles"                       '! ls "$R"/etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null | grep -q .'
  chk "no spoofed-MAC conf (unique MAC per unit)"    '[ ! -f "$R/etc/NetworkManager/conf.d/00-mb-macspoof.conf" ]'
  chk "no Tailscale identity"                        '[ ! -f "$R/var/lib/tailscale/tailscaled.state" ]'
  chk "no TLS cert (regenerated per unit)"           '[ ! -d "$R/etc/magicbridge/ssl" ]'
  chk "no plaintext config backup"                   '[ ! -d "$R/etc/magicbridge.orig_backup" ]'
  chk "LUKS fully absent (crypttab empty)"           '[ ! -s "$R/etc/crypttab" ]'
  chk "no LUKS container file"                       '[ ! -f "$R/var/lib/magicbridge-secure.img" ]'
  chk "first-boot WILL run (.firstboot-done absent)" '[ ! -e "$R/etc/magicbridge/.firstboot-done" ]'
  chk "late step WILL run (.firstboot-late-done absent)" '[ ! -e "$R/etc/magicbridge/.firstboot-late-done" ]'
  chk "mb-firstboot.service enabled"                 '[ -L "$R/etc/systemd/system/multi-user.target.wants/mb-firstboot.service" ]'
  chk "mb-firstboot-late.service enabled"            '[ -L "$R/etc/systemd/system/multi-user.target.wants/mb-firstboot-late.service" ]'
  chk "MagicBridge installed (/opt/magicbridge)"     '[ -f "$R/opt/magicbridge/core/magicbridge.py" ]'
  chk "/boot/firmware is nofail (cannot block boot)" 'grep -qE "^[^#].*/boot/firmware.*nofail" "$R/etc/fstab"'
  chk "RAM-log tmpfs is mode=0755 (not 1777)"        '! grep -qE "magicbridge-ram.*mode=1777" "$R/etc/fstab"'
  chk "config: no auth (defaults on first boot)"     'python3 -c "import json,sys;sys.exit(0 if \"auth\" not in json.load(open(\"$R/etc/magicbridge/config.json\")) else 1)"'
  chk "config: mac_persist empty"                    'python3 -c "import json,sys;sys.exit(0 if not json.load(open(\"$R/etc/magicbridge/config.json\")).get(\"mac_persist\") else 1)"'
  chk "config: video.mode=auto (detects C790 or USB)" 'python3 -c "import json,sys;sys.exit(0 if json.load(open(\"$R/etc/magicbridge/config.json\")).get(\"video\",{}).get(\"mode\")==\"auto\" else 1)"'
  echo ""
  if [[ $FAIL -eq 0 ]]; then ok "ALL CHECKS PASSED — safe to distribute"; exit 0
  else die "$FAIL check(s) FAILED — do NOT distribute this image"; fi
fi

# =====================================================================
#  COMPRESS — xz -T0 then verify the archive
# =====================================================================
if [[ "$MODE" == "compress" ]]; then
  command -v xz >/dev/null || die "xz not found (apt install xz-utils)"
  info "Compressing with xz -T0 (all cores) — this is the big win..."
  xz -T0 -v -k -f "$IMG"
  ok "Wrote ${IMG}.xz ($(du -h "${IMG}.xz" | cut -f1), from $(du -h "$IMG" | cut -f1))"
  info "Verifying archive integrity (xz -t)..."
  xz -t "${IMG}.xz" && ok "Archive integrity OK"
  echo "  Raspberry Pi Imager flashes .img.xz directly — ship this file."
  exit 0
fi

# =====================================================================
#  SHRINK — zero free space, then shrink the LAST partition + truncate
# =====================================================================
if [[ "$MODE" == "shrink" ]]; then
  attach
  # --- 1. Zero free space on EVERY partition -------------------------------
  # Deleting a file does NOT erase its blocks: an armed-but-unzeroed image still
  # holds recoverable remnants (old WiFi config, SSH keys, and for DIY the
  # deleted LUKS container + plaintext config backup). Zeroing overwrites them
  # AND makes the image compress enormously. Do this before distributing.
  for part in "$ROOTPART" "$BOOTPART"; do
    [[ -n "$part" && -e "$part" ]] || continue
    local_m=$(mktemp -d)
    if mount "$part" "$local_m" 2>/dev/null; then
      info "Zeroing free space on $part ..."
      dd if=/dev/zero of="$local_m/zero.fill" bs=4M status=none 2>/dev/null || true
      sync
      rm -f "$local_m/zero.fill"; sync
      ok "  free space on $part zeroed (remnants erased)"
      umount "$local_m" 2>/dev/null || true
    fi
    rmdir "$local_m" 2>/dev/null || true
  done
  losetup -d "$LOOP"; LOOP=""; trap - EXIT

  # --- 2. Shrink + truncate ------------------------------------------------
  # pishrink is the RIGHT tool for DIY (unlike the PiKVM sibling): it targets the
  # LAST partition — which here IS the rootfs — and injects the Pi-OS auto-expand
  # hook so a flashed card grows back. mb-firstboot-late.sh is the safety net if
  # that hook ever fails to run.
  if command -v pishrink.sh >/dev/null 2>&1; then
    info "Shrinking (resize2fs -M + partition shrink + truncate)..."
    pishrink.sh "$IMG" && ok "Shrunk: $(du -h "$IMG" | cut -f1)"
  else
    die "pishrink.sh not found. Install it:
   wget -qO /usr/local/bin/pishrink.sh https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
   chmod +x /usr/local/bin/pishrink.sh"
  fi
  echo ""; ok "Shrink complete: $IMG"
  echo "  Next: $0 --verify $IMG   then:  $0 --compress $IMG"
  exit 0
fi

# =====================================================================
#  ARM (default) — strip secrets, re-arm first-boot, boot-safety
# =====================================================================
if [[ -n "$OUT" && "$OUT" != "$IMG" ]]; then
  info "Copying $IMG -> $OUT (base stays untouched) ..."; cp --reflink=auto "$IMG" "$OUT"; IMG="$OUT"
else
  warn "Editing $IMG in place (pass an output name to keep the base)"
fi
attach; mount_root

[[ -d "$MNT/opt/magicbridge" ]] || warn "No /opt/magicbridge — net-install image? (it will install on first boot)"

# SELF-HEAL: refresh the first-boot logic from THIS repo into the image. The
# golden unit may predate the current scripts (or, as the sibling discovered,
# never have had the service installed at all) — a distributable image must not
# inherit stale first-boot behaviour. This is what guarantees the shipped image
# has the verified-marker + keep-WiFi safeguards regardless of the golden unit.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
if [[ -d "$REPO_DIR/src/core" ]]; then
  info "Refreshing first-boot logic in the image from $REPO_DIR ..."
  for f in mb-firstboot.sh mb-firstboot-late.sh mb-secret-reset.sh mb-hdmi-init.sh; do
    [[ -f "$REPO_DIR/src/core/$f" ]] || continue
    install -m 0755 "$REPO_DIR/src/core/$f" "$MNT/usr/local/bin/$f" && ok "  refreshed $f"
  done
  for s in mb-firstboot.service mb-firstboot-late.service; do
    [[ -f "$REPO_DIR/src/core/$s" ]] || continue
    install -m 0644 "$REPO_DIR/src/core/$s" "$MNT/etc/systemd/system/$s" && ok "  installed $s"
  done
else
  warn "repo not found next to this script - cannot refresh first-boot logic"
fi

info "Arming first-boot personalization..."
# Both markers gone -> both first-boot stages run on the flashed card.
rm -f "$MNT/etc/magicbridge/.firstboot-done" "$MNT/etc/magicbridge/.firstboot-late-done"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
for svc in mb-firstboot mb-firstboot-late; do
  if [[ -f "$MNT/etc/systemd/system/$svc.service" ]]; then
    ln -sf "../$svc.service" "$MNT/etc/systemd/system/multi-user.target.wants/$svc.service"
    ok "$svc.service enabled in the image"
  else
    warn "$svc.service missing from image - run install.sh in the base first"
  fi
done

# Strip every per-unit secret (belt-and-suspenders with first-boot secret-reset).
rm -f "$MNT"/etc/ssh/ssh_host_* \
      "$MNT"/etc/NetworkManager/system-connections/*.nmconnection \
      "$MNT"/var/lib/tailscale/tailscaled.state \
      "$MNT"/etc/machine-id 2>/dev/null || true
: > "$MNT/etc/machine-id" 2>/dev/null || true
rm -rf "$MNT/etc/magicbridge/ssl" "$MNT/etc/magicbridge.orig_backup" 2>/dev/null || true
rm -f "$MNT"/var/log/magicbridge-ram/* "$MNT"/var/log/magicbridge-firstboot.log \
      "$MNT"/var/log/magicbridge-firstboot-late.log 2>/dev/null || true
rm -f "$MNT"/etc/NetworkManager/conf.d/00-mb-macspoof.conf 2>/dev/null || true
if [[ -f "$MNT/etc/magicbridge/config.json" ]] && command -v python3 >/dev/null; then
  python3 - "$MNT/etc/magicbridge/config.json" <<'PY' 2>/dev/null || true
import json,sys
p=sys.argv[1]
try: c=json.load(open(p))
except Exception: c={}
c.pop("auth",None); c.pop("tailscale",None); c.pop("duckdns",None)
if isinstance(c.get("usb"),dict): c["usb"]["serial"]=""
c["mac_persist"]={}                        # -> unique vendor MAC per unit
c.setdefault("video",{})["mode"]="auto"    # -> detects C790/CSI or USB per unit
json.dump(c,open(p,"w"),indent=2)
PY
fi
ok "Stripped baked secrets + MAC identity"

# BOOT SAFETY (lesson from the sibling): /boot/firmware is not essential to a
# running system, but stock fstab gives it fsck pass 2 and no `nofail`, so a
# slightly-inconsistent boot partition blocks the ENTIRE boot (pings, no SSH).
if grep -qE '^[^#].*[[:space:]]/boot/firmware[[:space:]]' "$MNT/etc/fstab" && \
   ! grep -qE '^[^#].*/boot/firmware.*nofail' "$MNT/etc/fstab"; then
  sed -i -E '/^[^#].*[[:space:]]\/boot\/firmware[[:space:]]/ s/(vfat[[:space:]]+)([^[:space:]]+)/\1\2,nofail,x-systemd.device-timeout=15s/' "$MNT/etc/fstab"
  ok "/boot/firmware made nofail (cannot block boot)"
fi

sync; umount "$MNT"; MNT=""; losetup -d "$LOOP"; LOOP=""; trap - EXIT

echo ""; ok "Armed image ready: $IMG"
echo "  Next:  $0 --verify $IMG"
echo "         $0 --shrink $IMG      # zero free space + shrink"
echo "         $0 --compress $IMG    # -> .img.xz for distribution"
