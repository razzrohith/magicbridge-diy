#!/bin/bash
# Device test runner. Re-mints the session cookie before EVERY test, because
# several tests deliberately rotate the session secret (that is what they are
# testing) and would otherwise 401 every test that runs after them.
# Clear the first-run lock before each test. With the unit on the shipped
# default password the lock (correctly) 403s every API call, so any test that
# touches the API fails for the right reason at the wrong time. The tests that
# EXERCISE the lock set the defaults back themselves as their first action, so
# doing this unconditionally is safe and keeps the suite order-independent -
# which it was not: one test restoring its own snapshot used to leave the next
# five unable to authenticate.
unlock() {
  python3 - <<'PYUNLOCK'
import json
p = "/etc/magicbridge/config.json"
try:
    c = json.load(open(p))
except Exception:
    raise SystemExit(0)
a = c.setdefault("auth", {})
try:
    import bcrypt
    a["main_password_hash"] = bcrypt.hashpw(b"suite-runner-pw", bcrypt.gensalt()).decode()
except Exception:
    import hashlib
    a["main_password_hash"] = "sha256:" + hashlib.sha256(b"suite-runner-pw").hexdigest()
json.dump(c, open(p, "w"), indent=2)
PYUNLOCK
}
mint() {
  python3 - <<'PY'
import json,hmac,hashlib,time
a=json.load(open("/etc/magicbridge/config.json"))["auth"]
b="%d.3600.%d"%(int(time.time()),int(a.get("session_epoch",0)))
open("/tmp/mbck","w").write("mb_sess="+b+"."+hmac.new(a["main_secret_key"].encode(),b.encode(),hashlib.sha256).hexdigest())
PY
}
cp /etc/magicbridge/config.json /tmp/cfg.suite.bak
PASS=0; FAIL=0; FAILED=""
run() {  # run <name> <cmd...>
  local name="$1"; shift
  unlock
  mint
  printf "  %-34s " "$name"
  out=$("$@" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo "PASS"; PASS=$((PASS+1));
  else echo "FAIL"; FAIL=$((FAIL+1)); FAILED="$FAILED $name"; echo "$out" | tail -5 | sed 's/^/        /'; fi
}
run test_mjpeg_policy            bash -c 'cd /opt/magicbridge-repo && python3 tests/test_mjpeg_policy.py'
run test_html_structure          bash -c 'cd /opt/magicbridge-repo && python3 tests/test_html_structure.py'
run test_sessions_live           python3 /tmp/test_sessions_live.py
run test_device_name_live        python3 /tmp/test_device_name_live.py
run test_quality_lever_live      python3 /tmp/test_quality_lever_live.py
run test_adapt_live              python3 /tmp/test_adapt_live.py
run test_adapt_race_live         python3 /tmp/test_adapt_race_live.py
run test_first_run_lock          python3 /tmp/test_first_run_lock.py
run test_stealth_first_run       python3 /tmp/test_stealth_first_run.py
run test_first_run_both_panels   python3 /tmp/test_first_run_both_panels.py
run test_session_revocation_live python3 /tmp/test_session_revocation_live.py
run test_sweeper_precision     python3 /tmp/regfix_test.py
run test_mjpeg_rungs           python3 /tmp/rungcheck.py
run test_first_run_journey     python3 /tmp/e2e.py
# Restore to the SHIPPED DEFAULTS, not to "whatever was there when the suite
# started". Several tests deliberately change passwords, and restoring a
# start-of-run snapshot just propagates the previous run's leftovers - which is
# exactly how this rig ended up on a password nobody could guess, twice. Ending
# in a known state means the unit is always usable after a run.
cp /tmp/cfg.suite.bak /etc/magicbridge/config.json
python3 - <<'PYRESET'
import json
p = "/etc/magicbridge/config.json"
c = json.load(open(p)); a = c.setdefault("auth", {})
try:
    import bcrypt
    h = lambda s: bcrypt.hashpw(s.encode(), bcrypt.gensalt()).decode()
except Exception:
    import hashlib
    h = lambda s: "sha256:" + hashlib.sha256(s.encode()).hexdigest()
a["main_password_hash"] = h("magicbridge")
a["password_hash"]      = h("stealthbridge")
json.dump(c, open(p, "w"), indent=2)
PYRESET
systemctl restart magicbridge stealth-dashboard >/dev/null 2>&1; sleep 5
echo
echo "unit left on the SHIPPED DEFAULTS:  main = magicbridge   admin = stealthbridge"
echo
echo "SUITE: $PASS passed, $FAIL failed"
[ -n "$FAILED" ] && echo "FAILED:$FAILED"
exit $FAIL
