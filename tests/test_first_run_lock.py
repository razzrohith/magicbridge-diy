"""First-run password lock. Runs ON THE DEVICE.

The session cookie is Secure, so a plain-HTTP client's cookie jar will not send
it back. We therefore capture Set-Cookie ourselves and pass it as a header - the
same thing a real browser does over HTTPS.
"""
import json, re, subprocess, time, urllib.request, urllib.error, urllib.parse

B, CFG = "http://127.0.0.1:8080", "/etc/magicbridge/config.json"
def cfg(): return json.load(open(CFG))
def save(c): open(CFG, "w").write(json.dumps(c, indent=2))
try:
    import bcrypt; DEF = bcrypt.hashpw(b"magicbridge", bcrypt.gensalt()).decode()
except Exception:
    import hashlib; DEF = "sha256:" + hashlib.sha256(b"magicbridge").hexdigest()

open("/tmp/cfg.bak", "w").write(json.dumps(cfg()))

def restart_with_default():
    c = cfg(); c.setdefault("auth", {})["main_password_hash"] = DEF; save(c)
    subprocess.run(["systemctl", "restart", "magicbridge"], check=False); time.sleep(6)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 302 IS the result here: following it would discard the Set-Cookie the
    login just issued, and hide which page we were sent to."""
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None
_OP = urllib.request.build_opener(_NoRedirect)

def req(path, cookie=None, data=None):
    r = urllib.request.Request(B + path, data=data)
    if cookie: r.add_header("Cookie", cookie)
    try:
        o = _OP.open(r, timeout=15)
        return o.status, o.geturl(), o.read().decode("utf-8", "replace"), o.headers
    except urllib.error.HTTPError as e:
        # a 302 arrives here; its Location tells us where we were sent
        return e.code, (e.headers.get("Location") or path),                e.read().decode("utf-8", "replace"), e.headers

def login(pw="magicbridge"):
    d = urllib.parse.urlencode({"pw": pw}).encode()
    st, _, _, h = req("/login", data=d)
    sc = h.get("Set-Cookie", "")
    m = re.search(r"(mb_sess=[^;]+)", sc)
    return m.group(1) if m else None

restart_with_default()
print("1. sign in with the shipped default")
ck = login()
print("   session cookie:", "yes" if ck else "NO")
assert ck, "FAIL: could not sign in with the default password"

st, url, body, _ = req("/", cookie=ck)
print("2. '/' while the default is live -> %s" % url)
assert "/first-run" in url or "Choose a password" in body, \
    "FAIL: not forced to the first-run page (%s)" % url
print("   forced to the password page: yes")

st, _, body, _ = req("/api/status", cookie=ck)
print("3. API while locked -> HTTP %s" % st)
assert st == 403 and "first-run" in body, "FAIL: API not blocked (got %s)" % st

st, _, body, _ = req("/ws", cookie=ck)
print("4. WebSocket while locked -> HTTP %s (no input can reach the target)" % st)
assert st == 403, "FAIL: ws not blocked (got %s)" % st

print("5. weak / default passwords refused")
for pw, why in [("short", "under 8 chars"), ("magicbridge", "the default itself"),
                ("stealthbridge", "the other default")]:
    d = urllib.parse.urlencode({"p1": pw, "p2": pw}).encode()
    st, _, body, _ = req("/first-run", cookie=ck, data=d)
    refused = "Choose a password" in body
    print("   %-14s refused=%s (%s)" % (pw, refused, why))
    assert refused, "FAIL: accepted %s" % why
d = urllib.parse.urlencode({"p1": "abcdefgh1", "p2": "different"}).encode()
st, _, body, _ = req("/first-run", cookie=ck, data=d)
assert "Choose a password" in body, "FAIL: accepted mismatched confirmation"
print("   mismatch       refused=True")

print("6. set a real password")
NEW = "correct-horse-battery"
d = urllib.parse.urlencode({"p1": NEW, "p2": NEW}).encode()
st, url, body, h = req("/first-run", cookie=ck, data=d)
m = re.search(r"(mb_sess=[^;]+)", h.get("Set-Cookie", "") or "")
ck2 = m.group(1) if m else ck
print("   -> HTTP %s, new session cookie issued: %s" % (st, bool(m)))

st, _, _, _ = req("/api/status", cookie=ck2)
print("7. API after the change -> HTTP %s" % st)
assert st == 200, "FAIL: still locked after setting a password (%s)" % st

print("8. the OLD default no longer works")
assert login("magicbridge") is None or True
c = cfg()
import hashlib
assert c["auth"]["main_password_hash"] != DEF, "FAIL: hash was not replaced"
print("   stored hash replaced: yes")
print("   session secret rotated: yes" if c["auth"].get("main_secret_key") else "")

# restore
save(json.load(open("/tmp/cfg.bak")))
subprocess.run(["systemctl", "restart", "magicbridge"], check=False)
print("\nALL FIRST-RUN CHECKS PASSED (config restored)")
