#!/bin/bash
# ============================================================
#  MagicBridge — power-path A/B test
#
#  Compares wiring options objectively instead of by feel. Run it ONCE PER
#  WIRING OPTION, always straight after a fresh boot, and compare the summaries.
#
#  WHY A REBOOT IS MANDATORY:
#    `vcgencmd get_throttled` has two halves. Bits 0-3 = happening RIGHT NOW.
#    Bits 16-19 = "has happened since boot" and are STICKY — they never clear
#    until power-cycle. 0x50000 (bit16 + bit18) therefore means "under-voltage
#    and throttling occurred at some point since boot", NOT "under-voltage now".
#    Reading it hours later tells you nothing about the supply you're testing —
#    it may be a flag set by the inrush spike at plug-in. So: boot on the wiring
#    under test, then run this.
#
#  Usage on the Pi:
#     sudo bash /usr/local/bin/mb-power-test.sh  "option-2-gpio-psu"  120
#                                                 ^ label            ^ seconds
#  Results append to /var/log/magicbridge-powertest.log so runs can be diffed.
# ============================================================
set -uo pipefail

LABEL="${1:-unlabelled}"
DURATION="${2:-120}"
LOG=/var/log/magicbridge-powertest.log
VC=$(command -v vcgencmd || echo /usr/bin/vcgencmd)
[[ -x "$VC" ]] || { echo "vcgencmd not found — run this on the Pi"; exit 1; }

say(){ echo -e "$*" | tee -a "$LOG"; }

# --- decode the sticky/live halves of get_throttled separately --------------
decode(){                      # <hexvalue>
    local v=$(( $1 )) out=""
    (( v & 0x1     )) && out+="NOW:under-voltage "
    (( v & 0x2     )) && out+="NOW:arm-capped "
    (( v & 0x4     )) && out+="NOW:throttled "
    (( v & 0x8     )) && out+="NOW:temp-limit "
    (( v & 0x10000 )) && out+="SINCE-BOOT:under-voltage "
    (( v & 0x20000 )) && out+="SINCE-BOOT:arm-capped "
    (( v & 0x40000 )) && out+="SINCE-BOOT:throttled "
    (( v & 0x80000 )) && out+="SINCE-BOOT:temp-limit "
    echo "${out:-clean}"
}

UPTIME_S=$(cut -d. -f1 /proc/uptime)
say ""
say "============================================================"
say "MagicBridge power test — '$LABEL'   $(date)"
say "uptime at start : ${UPTIME_S}s $( [[ $UPTIME_S -gt 1800 ]] && echo '  <-- WARNING: not a fresh boot, sticky bits may be stale' )"
say "duration        : ${DURATION}s"

BASE=$("$VC" get_throttled | cut -d= -f2)
say "throttled@start : $BASE  ->  $(decode "$BASE")"
say "board           : $(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"

# What is actually drawing current right now — the comparison is only fair if
# the same peripherals are attached each run.
say "capture device  : $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
say "usb attached    : $(lsusb 2>/dev/null | grep -viE 'root hub' | sed 's/^/                  /' | tail -n +1 | head -6 | tr '\n' '|')"
say "usb gadget      : $(cat /sys/class/udc/*/state 2>/dev/null | tr '\n' ' ')  (want: configured)"

# --- load the board the way MagicBridge actually loads it -------------------
# Real workload beats stress-ng here: encode + HID + network is the profile that
# was browning out. Fall back to CPU burn if the stream isn't running.
say ""
say "--- applying load ---"
STREAM_UP=0
systemctl is-active --quiet magicbridge && STREAM_UP=1
say "magicbridge.service active : $([[ $STREAM_UP == 1 ]] && echo yes || echo NO - CPU burn only)"
BURN_PIDS=()
for _ in 1 2 3 4; do ( while :; do :; done ) & BURN_PIDS+=($!); done
trap 'kill "${BURN_PIDS[@]}" 2>/dev/null' EXIT

# --- sample ----------------------------------------------------------------
MINV=99; MAXT=0; WORST=0; SAMPLES=0; UV_HITS=0
END=$(( $(date +%s) + DURATION ))
while [[ $(date +%s) -lt $END ]]; do
    T=$("$VC" get_throttled | cut -d= -f2)
    V=$("$VC" measure_volts core | cut -d= -f2 | tr -d 'V')
    C=$("$VC" measure_temp | cut -d= -f2 | tr -d "'C")
    (( $(echo "$V < $MINV" | bc -l) )) && MINV="$V"
    (( $(echo "$C > $MAXT" | bc -l) )) && MAXT="$C"
    (( $(( T )) > WORST )) && WORST=$(( T ))
    (( $(( T )) & 0x1 )) && UV_HITS=$(( UV_HITS + 1 ))
    SAMPLES=$(( SAMPLES + 1 ))
    sleep 2
done
kill "${BURN_PIDS[@]}" 2>/dev/null; trap - EXIT

FINAL=$("$VC" get_throttled | cut -d= -f2)
say ""
say "--- RESULT: $LABEL ---"
say "samples             : $SAMPLES over ${DURATION}s"
say "min core voltage    : ${MINV}V      (sags = weak supply/cable)"
say "max temp            : ${MAXT}C"
say "live under-voltage  : $UV_HITS / $SAMPLES samples"
say "throttled@end       : $FINAL  ->  $(decode "$FINAL")"
if [[ "$FINAL" == "$BASE" && $UV_HITS -eq 0 ]]; then
    say "VERDICT             : PASS — no new under-voltage or throttling under load"
else
    say "VERDICT             : FAIL — supply sagged under load (new bits set)"
fi
say "gadget state@end    : $(cat /sys/class/udc/*/state 2>/dev/null | tr '\n' ' ')"
say "capture@end         : $(ls /dev/video* 2>/dev/null | tr '\n' ' ')   (gone = brownout dropped USB)"
say "============================================================"
