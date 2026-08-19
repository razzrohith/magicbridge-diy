"""Every rung on the MJPEG ladder must actually reach the encoder."""
import json, hmac, hashlib, time, glob, urllib.request
a = json.load(open("/etc/magicbridge/config.json"))["auth"]
b = "%d.3600.%d" % (int(time.time()), int(a.get("session_epoch", 0)))
CK = "mb_sess=" + b + "." + hmac.new(a["main_secret_key"].encode(), b.encode(), hashlib.sha256).hexdigest()
def post(body):
    r = urllib.request.Request("http://127.0.0.1:8080/api/stream/settings",
        data=json.dumps(body).encode(), headers={"Cookie": CK, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=40).read())
def live():
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try: args = open(p, "rb").read().split(b"\0")
        except Exception: continue
        if args and b"ustreamer" in args[0] and b"--quality" in args:
            return int(args[args.index(b"--quality") + 1])
    return None
bad = []
for q in [20, 16, 12, 9, 6]:
    post({"quality": q}); time.sleep(3)
    got = live()
    ok = (got == q)
    print("  rung %-2d -> encoder %-4s %s" % (q, got, "ok" if ok else "MISMATCH"))
    if not ok: bad.append((q, got))
post({"quality": 12})   # leave it on the factory value
print()
print("PASS: every rung reaches the encoder" if not bad else "FAIL: %s" % bad)
raise SystemExit(1 if bad else 0)
