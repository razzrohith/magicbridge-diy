"""Device-name API + the stealth boundary. Runs on the device.
The name must be operator-facing ONLY: it must never appear in what the target
sees (USB identity), nor in the network hostname or the EDID."""
import json, urllib.request, subprocess

CK = open('/tmp/mbck').read().strip()
def api(path, data=None):
    req = urllib.request.Request("http://127.0.0.1:8080" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Cookie": CK, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())

MARK = "ZZ-fleet-probe-42"

d = api("/api/device", {"name": MARK})
print("1. set name  :", d)
assert d["ok"] and d["name"] == MARK

g = api("/api/device")
print("2. get name  :", g["name"], "| version", g.get("version"))
assert g["name"] == MARK

st = api("/api/status")
print("3. status.device_name:", st.get("device_name"))
assert st.get("device_name") == MARK

# the boundary: the target-facing USB identity must NOT contain the label
ident = api("/api/identity")
print("4. usb identity:", ident.get("product"), "/", ident.get("manufacturer"), "/ serial:", ident.get("serial"))
assert MARK not in json.dumps(ident), "LEAK: operator label reached the USB identity!"

# the boundary: the network hostname must NOT contain the label
host = subprocess.check_output(["hostname"]).decode().strip()
print("5. hostname  :", host)
assert MARK not in host, "LEAK: operator label reached the hostname!"

# the boundary: config stores it under device.name, and NOT under usb/edid
cfg = json.load(open("/etc/magicbridge/config.json"))
print("6. cfg.device:", cfg.get("device"))
assert cfg.get("device", {}).get("name") == MARK
blob = json.dumps({k: v for k, v in cfg.items() if k in ("usb", "edid", "network")})
assert MARK not in blob, "LEAK: operator label reached usb/edid/network config!"

# length + control-char clamp
d2 = api("/api/device", {"name": "x" * 100 + "\n\t\x00bad"})
print("7. clamp     : len", len(d2["name"]), "repr", repr(d2["name"][:12]) + "...")
assert len(d2["name"]) <= 40 and "\n" not in d2["name"] and "\x00" not in d2["name"]

# clearing works
d3 = api("/api/device", {"name": ""})
print("8. clear     :", repr(d3["name"]))
assert d3["ok"] and d3["name"] == ""
assert api("/api/status").get("device_name") == ""

print("\nALL DEVICE-NAME CHECKS PASSED (label stayed operator-facing)")
