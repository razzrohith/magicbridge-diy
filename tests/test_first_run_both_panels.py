"""First-run must close BOTH default-password windows (main AND stealth)."""
import json, re, subprocess, time, urllib.request, urllib.error, urllib.parse
B, CFG = "http://127.0.0.1:8080", "/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
import bcrypt
c = cfg(); a = c.setdefault("auth", {})
a["main_password_hash"] = bcrypt.hashpw(b"magicbridge", bcrypt.gensalt()).decode()
a["password_hash"]      = bcrypt.hashpw(b"stealthbridge", bcrypt.gensalt()).decode()
old_stealth_secret = a.get("secret_key", "")
json.dump(c, open(CFG, "w"), indent=2)
subprocess.run(["systemctl", "restart", "magicbridge"], check=False); time.sleep(7)
print("0. both panels put on their shipped defaults")

class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): return None
OP = urllib.request.build_opener(_NR)
def req(path, cookie=None, data=None):
    r = urllib.request.Request(B + path, data=data)
    if cookie: r.add_header("Cookie", cookie)
    try:
        o = OP.open(r, timeout=15); return o.status, o.read().decode("utf-8","replace"), o.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace"), e.headers

st, _, h = req("/login", data=urllib.parse.urlencode({"pw": "magicbridge"}).encode())
ck = (re.search(r"(mb_sess=[^;]+)", h.get("Set-Cookie","") or "") or [None,""])[0] if h.get("Set-Cookie") else None
ck = re.search(r"(mb_sess=[^;]+)", h.get("Set-Cookie","")).group(1)
print("1. signed in with the main default")
NEW = "one-password-both"
st, body, h = req("/first-run", cookie=ck, data=urllib.parse.urlencode({"p1":NEW,"p2":NEW}).encode())
print("2. first-run submitted -> HTTP", st)

a = cfg()["auth"]
main_ok = bcrypt.checkpw(NEW.encode(), a["main_password_hash"].encode())
steal_ok = bcrypt.checkpw(NEW.encode(), a["password_hash"].encode())
still_default = bcrypt.checkpw(b"stealthbridge", a["password_hash"].encode())
print("3. main panel now uses the new password :", main_ok)
print("4. STEALTH panel now uses it too        :", steal_ok)
print("5. stealth still accepts 'stealthbridge':", still_default, "(must be False)")
print("6. stealth session secret rotated       :", a.get("secret_key","") != old_stealth_secret)
assert main_ok, "FAIL: main password not set"
assert steal_ok, "FAIL: stealth password NOT closed by first-run"
assert not still_default, "FAIL: stealthbridge still works after first-run"
assert a.get("secret_key","") != old_stealth_secret, "FAIL: stealth secret not rotated"
print("\nBOTH DEFAULT-PASSWORD WINDOWS CLOSED BY ONE FIRST-RUN - PASS")
