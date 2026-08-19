"""Does the Picture-quality control actually move the MJPEG encoder?"""
import json, subprocess, time, urllib.request, glob, os
CK = open('/tmp/mbck').read().strip()
def post(body):
    r = urllib.request.Request("http://127.0.0.1:8080/api/stream/settings",
        data=json.dumps(body).encode(),
        headers={"Cookie": CK, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())
def ustreamer_quality():
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            args = open(p, "rb").read().split(b"\0")
        except Exception:
            continue
        if args and b"ustreamer" in args[0]:
            for i, a in enumerate(args):
                if a == b"--quality":
                    return int(args[i+1])
    return None

res = []
for min_qp, q in [(20, 30), (40, 14), (30, 22)]:
    post({"min_qp": min_qp, "quality": q})
    time.sleep(3)
    got = ustreamer_quality()
    res.append((min_qp, q, got))
    print("  sent min_qp=%-2d quality=%-2d  ->  ustreamer --quality %s" % (min_qp, q, got))

vals = [g for _, _, g in res if g is not None]
print()
if len(set(vals)) > 1:
    print("PASS: the control genuinely changes the MJPEG encoder (%s)" % sorted(set(vals)))
else:
    print("FAIL: encoder quality never changed (%s) - still theatre" % vals)
