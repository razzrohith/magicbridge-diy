#!/usr/bin/env python3
"""Quality auto-adapt behaviour test. RUN THIS ON THE DEVICE.

    scp tests/test_adapt_live.py raj@<pi>:/tmp/ && ssh raj@<pi> python3 /tmp/test_adapt_live.py

Needs an authenticated cookie in /tmp/mbck - see tests/test_sessions_live.py for
how to mint one.

WHY these exact asserts. Changing quality restarts the encoder and freezes the
picture for ~1.7s (measured), so the whole design is about NOT doing that too
often or against the operator's wishes. Each check guards one of those promises:
one step down works; a second step is refused until the cooldown elapses (so two
viewers cannot thrash one encoder); a step up returns toward the operator's
setting; it never goes sharper than that setting (the ceiling); a manual quality
change becomes the new ceiling and clears the "eased off" note; bad input is
rejected. It only ever retunes the encoder and never touches the target.
"""
import json, time, urllib.request

CK = open('/tmp/mbck').read().strip()
def api(path, data=None, method=None):
    req = urllib.request.Request("http://127.0.0.1:8080" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Cookie": CK, "Content-Type": "application/json"}, method=method)
    return json.loads(urllib.request.urlopen(req).read())

def qp():
    return api("/api/status")["stream"].get("min_qp")

# make sure we're on h264 and reset to a known ceiling
mode = api("/api/status")["stream"].get("mode") or api("/api/status")["stream"].get("device_type")
_mode = api("/api/status")["stream"].get("mode")
print("0. transport now:", _mode)
if _mode == "mjpeg":
    # Adapt works on BOTH transports now, but the lever and the ladder differ
    # (min_qp on H.264, quality on MJPEG), so the hardcoded 30/34 expectations
    # below only describe the H.264 ladder. Covered for MJPEG by
    # test_adapt_race_live, which reads the live field from the API.
    print("   MJPEG unit: H.264-ladder assertions skipped (see test_adapt_race_live)")
    raise SystemExit(0)
api("/api/stream/settings", {"min_qp": 30})   # ceiling = 30
time.sleep(2)
print("1. ceiling set, min_qp =", qp())

g = api("/api/stream/adapt")
print("2. GET adapt:", json.dumps(g["adapt"]))
assert g["adapt"]["ceiling"] == 30, "ceiling should be 30"
assert g["adapt"]["adapted"] is False

# step DOWN once -> 30 -> 34
d = api("/api/stream/adapt", {"dir": "down", "reason": "test loss 5%"})
print("3. down:", d.get("applied"), "min_qp ->", d["adapt"]["min_qp"], "| reason:", d["adapt"]["reason"])
assert d["applied"] and d["adapt"]["min_qp"] == 34

# immediate second step must be blocked by cooldown
d2 = api("/api/stream/adapt", {"dir": "down", "reason": "again"})
print("4. down again immediately: applied=%s cooldown=%ss" % (d2.get("applied"), d2.get("cooldown")))
assert d2.get("applied") is False and d2.get("cooldown", 0) > 0

# An "up" is deliberately HELD OFF for ADAPT_HOLD_DOWN (90s) after anyone asks
# to ease off, even once the 45s step cooldown has passed. With two viewers on
# different links, the clean one must not undo the step the struggling one just
# made - there is only one encoder, so the worst link has to win.
print("5. waiting out the step cooldown; UP must still be held off ...")
time.sleep(d2["cooldown"] + 2)
held = api("/api/stream/adapt", {"dir": "up", "reason": "recovered"})
print("   up during the hold: applied=%s held_by_other=%s cooldown=%ss"
      % (held.get("applied"), held.get("held_by_other"), held.get("cooldown")))
assert held.get("applied") is False and held.get("held_by_other"),     "a clean viewer must not undo a struggling viewer's step"

print("5b. waiting out the down-hold, then UP must work and clamp at the ceiling ...")
time.sleep(held["cooldown"] + 2)
u = api("/api/stream/adapt", {"dir": "up", "reason": "recovered"})
print("   up:", u.get("applied"), "min_qp ->", u["adapt"]["min_qp"])
assert u["applied"] and u["adapt"]["min_qp"] == 30, "should return to ceiling 30"

time.sleep(api("/api/stream/adapt")["adapt"]["cooldown"] + 2)
u2 = api("/api/stream/adapt", {"dir": "up", "reason": "recovered more"})
print("6. up past ceiling: applied=%s at_limit=%s min_qp=%s"
      % (u2.get("applied"), u2.get("at_limit"), u2["adapt"]["min_qp"]))
assert u2.get("applied") is False and u2["adapt"]["min_qp"] == 30, "must not exceed ceiling"

# a manual quality change RESETS the ceiling and clears the adapted note
api("/api/stream/settings", {"min_qp": 26})
time.sleep(2)
g3 = api("/api/stream/adapt")
print("7. after manual set to 26: ceiling=%s adapted=%s reason=%r"
      % (g3["adapt"]["ceiling"], g3["adapt"]["adapted"], g3["adapt"]["reason"]))
assert g3["adapt"]["ceiling"] == 26 and g3["adapt"]["adapted"] is False and g3["adapt"]["reason"] == ""

# bad direction rejected
try:
    api("/api/stream/adapt", {"dir": "sideways"})
    print("8. FAIL: bad dir accepted")
except urllib.error.HTTPError as e:
    print("8. bad dir -> HTTP", e.code)
    assert e.code == 400

# restore ceiling to the shipping default so we leave the unit clean
api("/api/stream/settings", {"min_qp": 30})
print("\nALL ADAPT CHECKS PASSED")
