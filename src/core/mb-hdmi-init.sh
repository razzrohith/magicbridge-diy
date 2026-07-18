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
DEV=/dev/video0
EDID=/opt/magicbridge/edid/mb-edid-1080p50.hex
LOG=/var/log/mb-hdmi-init.log
log() { echo "$(date '+%F %T') [$1] ${*:2}" >> "$LOG" 2>/dev/null; }

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
    for i in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
    [ -e "$DEV" ] || { log INIT "ERROR: $DEV absent - is dtoverlay=tc358743 set?"; exit 0; }
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
      [ -e "$DEV" ] || continue
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
