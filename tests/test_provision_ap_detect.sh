#!/bin/bash
# Does the setup hotspot's "did it start?" check actually recognise a RUNNING
# hostapd/dnsmasq?  Run:  bash tests/test_provision_ap_detect.sh
#
# WHY THIS EXISTS. The shipped check was:
#
#     pgrep -f "hostapd /tmp/mb-hostapd"   ->  AP_FAIL="hostapd"
#
# but the real command line is "hostapd -B /tmp/mb-hostapd.conf -P ...". The -B
# sits between the two halves, so the pattern could never match. Every unit
# therefore declared "Hotspot FAILED / hostapd" and tore down an access point
# that had already reached AP-ENABLED. A field report proved it: hostapd was
# alive as PID 1222 and its own stderr was empty, while the OLED said it failed.
#
# It shipped because it LOOKS right, and because it sits directly beside the
# dnsmasq line, which uses ".*" and does match. Reading cannot reliably catch a
# regex that is wrong only in the part you are not looking at - matching it
# against a real command line can. That is all this test does, and it is enough:
# every customer's first boot depended on this one pattern.
set -u
SCRIPT="${1:-src/provision/mb-provision.sh}"
fails=0
pass(){ echo "  ok   $1"; }
fail(){ echo "  FAIL $1"; fails=$((fails+1)); }

# The command lines mb-provision.sh actually launches (kept in sync with it).
HOSTAPD_CMD='hostapd -B /tmp/mb-hostapd.conf -P /tmp/mb-hostapd.pid'
DNSMASQ_CMD='dnsmasq -C /tmp/mb-dnsmasq.conf --pid-file=/tmp/mb-dnsmasq.pid'
# A system dnsmasq that must NOT be mistaken for ours (it runs on normal units).
SYSTEM_DNSMASQ='/usr/sbin/dnsmasq -x /run/dnsmasq/dnsmasq.pid -u dnsmasq -7 /etc/dnsmasq.d'

echo "AP DETECTION  $SCRIPT"

[ -f "$SCRIPT" ] || { echo "  FAIL cannot read $SCRIPT"; exit 1; }

# 1+2. Every process pattern the script greps for must match the real cmdline.
#      Pull them straight out of the file so the test tracks the code.
while read -r pat; do
  case "$pat" in
    *hostapd*)
      if echo "$HOSTAPD_CMD" | grep -qE "$pat"; then pass "pattern matches running hostapd:  $pat"
      else fail "pattern CANNOT match a running hostapd:  $pat"; fi ;;
    *dnsmasq*)
      if echo "$DNSMASQ_CMD" | grep -qE "$pat"; then pass "pattern matches running dnsmasq:  $pat"
      else fail "pattern CANNOT match a running dnsmasq:  $pat"; fi
      if echo "$SYSTEM_DNSMASQ" | grep -qE "$pat"; then
        fail "pattern also matches the SYSTEM dnsmasq (false positive):  $pat"
      else pass "pattern ignores the system dnsmasq:  $pat"; fi ;;
  esac
#      Strip comments first: the fix's own comment quotes the OLD broken pattern
#      to explain it, and a test that reads prose as code fails on its own
#      documentation.
done < <(sed -E 's/[[:space:]]*#.*$//' "$SCRIPT" \
         | grep -oE '(pgrep|pkill) -f "[^"]+"' | sed -E 's/.*-f "([^"]+)"/\1/' | sort -u)

# 3. The liveness helper must accept a live pid and reject a dead one. Exercise
#    the real function by sourcing just its definition out of the script.
tmp=$(mktemp); sed -n '/^alive() {/,/^}/p' "$SCRIPT" > "$tmp"
if [ -s "$tmp" ]; then
  # shellcheck disable=SC1090
  . "$tmp"
  sleep 30 & live=$!
  pf=$(mktemp); echo "$live" > "$pf"
  alive "$pf" 'no-such-process-xyz' && pass "alive() accepts a live pid file" \
                                    || fail "alive() rejected a LIVE pid"
  kill "$live" 2>/dev/null; wait "$live" 2>/dev/null
  echo "999999" > "$pf"
  alive "$pf" 'no-such-process-xyz' && fail "alive() accepted a DEAD pid" \
                                    || pass "alive() rejects a dead pid file"
  : > "$pf"
  alive "$pf" 'no-such-process-xyz' && fail "alive() accepted an EMPTY pid file" \
                                    || pass "alive() rejects an empty pid file"
  echo "not-a-number" > "$pf"
  alive "$pf" 'no-such-process-xyz' && fail "alive() accepted GARBAGE in pid file" \
                                    || pass "alive() rejects a garbage pid file"
  rm -f "$pf"
else
  fail "could not find the alive() helper in $SCRIPT"
fi
rm -f "$tmp"

# 4. The script must still be valid bash.
if bash -n "$SCRIPT" 2>/dev/null; then pass "script syntax is valid"
else fail "script has a syntax error"; fi

echo
echo "FAILURES: $fails"
[ "$fails" -eq 0 ] || exit 1
