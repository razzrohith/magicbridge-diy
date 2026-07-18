#!/bin/bash
# MagicBridge - initialise the C790 / TC358743 HDMI capture path at boot.
#
# WHY THIS EXISTS: the TC358743 stores its EDID only in volatile RAM. After any
# reboot there is NO EDID, so the HDMI source sees no valid sink, sends nothing,
# and /dev/video0 reports "Link has been severed" with 0x0 timings. Without this
# service, video is dead after every power cycle.
DEV=/dev/video0
LOG=/var/log/mb-hdmi-init.log
log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

log "=== mb-hdmi-init starting ==="

# wait for the capture node to appear
for i in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
if [ ! -e "$DEV" ]; then
  log "ERROR: $DEV never appeared - is dtoverlay=tc358743 set in config.txt?"
  exit 0
fi

# 1. push an EDID so the source knows what we accept (built-in preset includes
#    the CTA-861 audio bit, needed for I2S audio de-embedding)
v4l2-ctl -d "$DEV" --set-edid pad=0,type=hdmi --fix-edid-checksums >/dev/null 2>&1
log "EDID applied to $DEV"

# 2. wait for the source to re-read the EDID and start sending, then lock timings
for i in $(seq 1 10); do
  sleep 2
  if v4l2-ctl -d "$DEV" --query-dv-timings 2>/dev/null | grep -q 'Active width: [1-9]'; then
    v4l2-ctl -d "$DEV" --set-dv-bt-timings query >/dev/null 2>&1
    v4l2-ctl -d "$DEV" --set-fmt-video=width=1920,height=1080,pixelformat=UYVY >/dev/null 2>&1
    log "signal detected, timings locked: $(v4l2-ctl -d "$DEV" --get-dv-timings 2>/dev/null | tr '\n' ' ' | cut -c1-140)"
    log "=== done (ok) ==="
    exit 0
  fi
done

# No source attached yet - that's fine. EDID is set, so as soon as a cable is
# plugged in the source will negotiate; MagicBridge locks timings when it opens
# the device.
log "no HDMI signal within ~20s (source likely powered off). EDID is set."
log "=== done (no signal) ==="
exit 0
