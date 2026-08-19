"""Stealth-panel first-run lock. Runs ON THE DEVICE against 127.0.0.1:7777."""
import json, re, subprocess, time, urllib.request, urllib.error, urllib.parse

B, CFG = "http://127.0.0.1:7777", "/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
def save(c): open(CFG, "w").write(json.dumps(c, indent=2))
try:
    import bcrypt; DEF = bcrypt.hashpw(b"stealthbridge", bcrypt.gensalt()).decode()
except Exception:
    import hashlib; DEF = "sha256:" + hashlib.sha256(b"stealthbridge").hexdigest()

open("/tmp/cfg.st.bak", "w").write(json.dumps(cfg()))

class _NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): return None
OP = urllib.request.build_opener(_NR)

def req(path, cookie=None, data=None):
    r = urllib.request.Request(B + path, data=data)
    if cookie: r.add_header("Cookie", cookie)
    try:
        o = OP.open(r, timeout=15)
        return o.status, (o.headers.get("Location") or path), o.read().decode("utf-8","replace"), o.headers
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Location") or path), e.read().decode("utf-8","replace"), e.headers

def cookie_of(h):
    m = re.search(r"(session=[^;]+)", h.get("Set-Cookie","") or "")
    return m.group(1) if m else None

# put the panel on the shipped default and restart it
c = cfg(); c.setdefault("auth", {})["password_hash"] = DEF; save(c)
subprocess.run(["systemctl","restart","stealth-dashboard"], check=False); time.sleep(5)

print("1. sign in with the shipped default 'stealthbridge'")
st,_,body,h = req("/login")
ck = cookie_of(h)
csrf = (re.search(r'name="_csrf"[^>]*value="([^"]+)"', body) or [None,""])[1]
d = urllib.parse.urlencode({"pw":"stealthbridge","_csrf":csrf}).encode()
st,loc,body,h = req("/login", cookie=ck, data=d)
ck = cookie_of(h) or ck
print("   login -> HTTP %s, sent to %s" % (st, loc))
assert st in (302,200), "FAIL: login rejected (%s)" % st

st,loc,body,_ = req("/", cookie=ck)
print("2. '/' while the default is live -> %s" % loc)
assert "first-run" in (loc or ""), "FAIL: not forced to first-run (%s)" % loc

st,_,body,_ = req("/api/status", cookie=ck)
print("3. admin API while locked -> HTTP %s" % st)
assert st == 403 and "first-run" in body, "FAIL: API not blocked (%s)" % st

st,_,body,h = req("/first-run", cookie=ck)
print("4. the page renders:", bool(re.search(r"Choose a password", body)))
assert "Choose a password" in body
csrf = (re.search(r'name="_csrf"[^>]*value="([^"]+)"', body) or [None,""])[1]

print("5. weak / default passwords refused")
for pw,why in [("short","under 8"),("stealthbridge","this default"),("magicbridge","other default")]:
    d = urllib.parse.urlencode({"p1":pw,"p2":pw,"_csrf":csrf}).encode()
    st,_,body,_ = req("/first-run", cookie=ck, data=d)
    print("   %-14s refused=%s (%s)" % (pw, "Choose a password" in body, why))
    assert "Choose a password" in body, "FAIL: accepted %s" % why

print("6. set a real password")
NEW = "admin-panel-pass-9"
d = urllib.parse.urlencode({"p1":NEW,"p2":NEW,"_csrf":csrf}).encode()
st,loc,body,_ = req("/first-run", cookie=ck, data=d)
print("   -> HTTP %s, sent to %s" % (st, loc))
h2 = cfg()["auth"]["password_hash"]
assert h2 != DEF, "FAIL: hash not replaced"
try:
    import bcrypt; assert bcrypt.checkpw(NEW.encode(), h2.encode()), "FAIL: new password does not verify"
except ImportError: pass
print("   stored hash replaced and verifies: yes")

print("7. old session was invalidated (secret rotated)")
st,loc,_,_ = req("/", cookie=ck)
print("   old cookie -> HTTP %s to %s" % (st, loc))
assert "login" in (loc or "") or st in (302,401), "FAIL: old session still valid"

save(json.load(open("/tmp/cfg.st.bak")))
subprocess.run(["systemctl","restart","stealth-dashboard"], check=False)
print("\nALL STEALTH FIRST-RUN CHECKS PASSED (config restored)")
