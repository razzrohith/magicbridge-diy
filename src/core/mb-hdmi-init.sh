#!/bin/bash
# MagicBridge - C790 / TC358743 HDMI capture bring-up.  PORTABLE: no hardcoded
# resolution, works with any source machine (Intel / AMD / NVIDIA, any panel).
#
# WHY THIS EXISTS:
#  1. The TC358743 keeps its EDID only in volatile RAM. After a reboot there is
#     NO EDID, the source sees no sink and sends nothing ("Link has been severed").
#  2. The Pi 4B's 2-lane CSI tops out at 1080p50. Our EDID advertises 1080p50 as
#     the native mode and never offers 1080p60, so ANY source auto-negotiates a
#     mode we can actually capture - no manual refresh-rate fiddling per laptop.
#
# Usage: mb-hdmi-init.sh --init   (oneshot at boot, before magicbridge)
#        mb-hdmi-init.sh --watch  (daemon: re-arms on hot-plug / source change)
DEV=""   # resolved at runtime to the C790/CSI node (never a USB dongle)
EDID=/opt/magicbridge/edid/mb-edid-1080p50.hex
LOG=/var/log/mb-hdmi-init.log
log() { echo "$(date '+%F %T') [$1] ${*:2}" >> "$LOG" 2>/dev/null; }

# Find the C790/TC358743 CSI capture node. A USB HDMI dongle is deliberately
# EXCLUDED: it carries its own fixed EDID and must never be fed --set-edid, and
# it has no DV-timings to lock - EDID/timings bring-up only applies to the CSI
# board. Prints /dev/videoN on stdout, non-zero if there's no CSI board.
find_csi_dev() {
  local d
  for d in /dev/video*; do
    [ -e "$d" ] || continue
    if v4l2-ctl -d "$d" --info 2>/dev/null | grep -qiE 'tc358743|unicam|fe801000'; then
      echo "$d"; return 0
    fi
  done
  return 1
}

have_signal() { v4l2-ctl -d "$DEV" --query-dv-timings 2>/dev/null | grep -q 'Active width: [1-9]'; }
dev_busy()    { fuser "$DEV" >/dev/null 2>&1; }

apply_edid() {
  if [ -r "$EDID" ]; then
    v4l2-ctl -d "$DEV" --set-edid "file=$EDID" --fix-edid-checksums >/dev/null 2>&1 \
      && { log "$1" "EDID applied from $EDID (caps source at 1080p50)"; return 0; }
    log "$1" "WARN: custom EDID failed, falling back to built-in preset"
  fi
  v4l2-ctl -d "$DEV" --set-edid pad=0,type=hdmi --fix-edid-checksums >/dev/null 2>&1
  log "$1" "EDID applied (built-in hdmi preset fallback)"
}

# Lock whatever the source is actually sending. Resolution is NOT hardcoded:
# we adopt the detected timings, then only force the pixel format to UYVY
# (16bpp) because BGR3 (24bpp) cannot reach 1080p50 over 2 lanes.
lock_timings() {
  v4l2-ctl -d "$DEV" --set-dv-bt-timings query >/dev/null 2>&1 || return 1
  v4l2-ctl -d "$DEV" -v pixelformat=UYVY >/dev/null 2>&1
  local res
  res=$(v4l2-ctl -d "$DEV" --get-fmt-video 2>/dev/null | grep -i 'Width/Height' | tr -s ' ')
  log "$1" "locked:$(v4l2-ctl -d "$DEV" --query-dv-timings 2>/dev/null | grep -i pixelclock | tr -s ' ') |$res"
  return 0
}

case "${1:---init}" in
  --init)
    log INIT "=== starting ==="
    # Wait for a CSI capture node to enumerate (the C790 can take a few seconds).
    for i in $(seq 1 30); do DEV=$(find_csi_dev) && break; sleep 1; done
    if [ -z "$DEV" ]; then
      log INIT "no C790/TC358743 CSI device - USB capture or none present; skipping EDID/timings (nothing to do)"
      exit 0
    fi
    log INIT "CSI capture device: $DEV"
    apply_edid INIT
    for i in $(seq 1 10); do
      sleep 2
      if have_signal; then lock_timings INIT; log INIT "=== done (signal) ==="; exit 0; fi
    done
    log INIT "=== done (no source yet; watchdog will arm it) ==="
    exit 0
    ;;
  --watch)
    log WATCH "=== watchdog started ==="
    armed=0
    while true; do
      sleep 10
      # Re-resolve each pass: a CSI board may appear/disappear, and on a USB-only
      # unit there's simply nothing here to arm.
      DEV=$(find_csi_dev) || { armed=0; continue; }
      if have_signal; then
        if [ "$armed" = "0" ] && ! dev_busy; then
          lock_timings WATCH && armed=1
        fi
      else
        # source unplugged / asleep: re-arm and re-push EDID so the next
        # machine plugged in negotiates correctly with no manual steps
        if [ "$armed" = "1" ]; then
          log WATCH "signal lost - re-arming"
          armed=0
          dev_busy || apply_edid WATCH
        fi
      fi
    done
    ;;
esac
