#!/bin/bash
# Device test runner. Re-mints the session cookie before EVERY test, because
# several tests deliberately rotate the session secret (that is what they are
# testing) and would otherwise 401 every test that runs after them.
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
cp /tmp/cfg.suite.bak /etc/magicbridge/config.json
systemctl restart magicbridge stealth-dashboard >/dev/null 2>&1; sleep 5
echo
echo "SUITE: $PASS passed, $FAIL failed"
[ -n "$FAILED" ] && echo "FAILED:$FAILED"
exit $FAIL
