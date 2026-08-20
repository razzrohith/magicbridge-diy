#!/usr/bin/env python3
"""The admin panel must FAIL CLOSED on a corrupt config. Runs on the device.

The panel used to swallow every read error and return {}, which looked exactly
like a brand-new device: it then wrote back the PUBLISHED default password,
minted a new secret, and persisted a config containing only the auth block -
destroying the main panel's hash, the USB/EDID identity and the MAC. Anyone on
the LAN could then sign in with the word from the public repo.

Reaching a torn config was not exotic: the panel's own writer was a
truncate-then-write called from 19 places, on a USB-powered device that gets
unplugged. This asserts the three fixes: corrupt is not treated as empty, the
writer is atomic, and an unauthenticated hit on /login cannot trigger a
bootstrap write.
"""
import json, os, shutil, subprocess, time, urllib.request, urllib.error

CFG = "/etc/magicbridge/config.json"
S = "http://127.0.0.1:7777"
fails = []

def check(name, ok, detail=""):
    print("  %-52s %s %s" % (name, "ok" if ok else "FAIL", detail))
    if not ok: fails.append(name)

shutil.copy(CFG, "/tmp/cfg.fc.bak")
orig = json.load(open(CFG))
try:
    import bcrypt
    known = bcrypt.hashpw(b"a-real-password", bcrypt.gensalt()).decode()
    def is_default(h):
        try: return bcrypt.checkpw(b"stealthbridge", h.encode())
        except Exception: return False
except Exception:
    known, is_default = None, lambda h: False

# a known, non-default admin password we can detect a reset away from
c = json.load(open(CFG)); c.setdefault("auth", {})["password_hash"] = known
json.dump(c, open(CFG, "w"), indent=2)
subprocess.run(["systemctl", "restart", "stealth-dashboard"], check=False); time.sleep(5)

print("1. CORRUPT the config while the panel is running")
good = open(CFG).read()
open(CFG, "w").write("{ this is not json")

print("2. hit /stealth/login unauthenticated (this used to trigger a bootstrap write)")
try:
    urllib.request.urlopen(S + "/login", timeout=10).read()
except Exception as e:
    print("     (login returned %s - fine, we only care what it WROTE)" % type(e).__name__)
time.sleep(2)
after = open(CFG).read()
check("corrupt config was NOT overwritten by an anon request", after == "{ this is not json",
      "" if after == "{ this is not json" else "-> file was rewritten!")

print("3. restart the panel with the config still corrupt")
subprocess.run(["systemctl", "restart", "stealth-dashboard"], check=False); time.sleep(5)
after2 = open(CFG).read()
check("corrupt config survived a restart (no default reset)", after2 == "{ this is not json",
      "" if after2 == "{ this is not json" else "-> rewritten on boot!")
try:
    parsed = json.loads(after2)
    reset = is_default(parsed.get("auth", {}).get("password_hash", ""))
    check("panel did NOT reset itself to 'stealthbridge'", not reset)
except Exception:
    check("panel did NOT reset itself to 'stealthbridge'", True, "(config still corrupt, nothing written)")

print("4. restore a good config; the panel must recover")
open(CFG, "w").write(good)
subprocess.run(["systemctl", "restart", "stealth-dashboard"], check=False); time.sleep(6)
st = subprocess.run(["systemctl", "is-active", "stealth-dashboard"], capture_output=True, text=True).stdout.strip()
check("panel healthy again after a good config returns", st == "active", st)
h = json.load(open(CFG)).get("auth", {}).get("password_hash", "")
check("the operator's password survived the whole episode", h == known)

print("5. the writer is atomic (temp file + rename, never truncate-in-place)")
src = open("/opt/magicbridge/dashboard/stealth-dashboard.py").read()
check("_save uses os.replace", "os.replace" in src or "_os.replace" in src)
check("_save no longer uses write_text on the live file",
      "Path(CONFIG_PATH).write_text" not in src)

shutil.copy("/tmp/cfg.fc.bak", CFG)
subprocess.run(["systemctl", "restart", "stealth-dashboard", "magicbridge"], check=False)
print()
print("FAILURES: %d" % len(fails))
raise SystemExit(1 if fails else 0)
