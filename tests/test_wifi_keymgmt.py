#!/usr/bin/env python3
"""Every WiFi profile we create with a password must also set key-mgmt.

    python3 tests/test_wifi_keymgmt.py

WHY THIS EXISTS. Commit 0a015cb set out to support WPA3 routers. It reasoned
that pinning wifi-sec.key-mgmt=wpa-psk stops an SAE-only router associating, so
it REMOVED key-mgmt from all three WiFi paths and let NetworkManager negotiate.

That inverted the bug it was fixing. key-mgmt is a REQUIRED property of
NetworkManager's 802-11-wireless-security setting, so supplying wifi-sec.psk
without it makes `nmcli connection add` fail verification outright:

    Error: Failed to add connection:
    802-11-wireless-security.key-mgmt: property is missing

No profile is created, `nmcli connection up` has nothing to activate, and the
owner is told "Wrong password?" while holding the correct password - the precise
false accusation that commit was written to eliminate. It shipped in all three
places at once (provisioning, the web UI, the stealth dashboard), so a customer
could not join a protected network by ANY route.

The correct way to support WPA3 is to TRY each key-mgmt (wpa-psk, then sae), not
to omit a required property. This test enforces exactly that: wherever a psk is
passed to nmcli, key-mgmt must be passed too, and sae must be reachable so WPA3
support does not quietly regress to WPA2-only.

Static check: no NetworkManager, no device, no network needed.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    "src/provision/mb-provision.sh",
    "src/core/magicbridge.py",
    "src/dashboard/stealth-dashboard.py",
]

# Lines that merely TALK about the properties (comments, docstrings) must not be
# read as code - the fix's own explanation quotes the broken form.
def strip_comments(text, is_py):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        # inline comment, crude but sufficient for these files
        if is_py:
            line = re.sub(r"\s+#.*$", "", line)
        else:
            line = re.sub(r"\s+#.*$", "", line)
        out.append(line)
    return "\n".join(out)


def main():
    fails = []
    checked = 0
    print("WIFI KEY-MGMT GUARD")
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            fails.append("%s: missing" % rel)
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        body = strip_comments(raw, rel.endswith(".py"))

        psk_hits = len(re.findall(r"wifi-sec\.psk", body))
        km_hits = len(re.findall(r"wifi-sec\.key-mgmt", body))
        has_sae = "sae" in body
        checked += 1

        if psk_hits == 0:
            print("  --   %s: sets no psk (nothing to guard)" % rel)
            continue

        # 1. every psk site needs key-mgmt available in the same file
        if km_hits == 0:
            fails.append("%s: passes wifi-sec.psk %d time(s) but NEVER sets "
                         "wifi-sec.key-mgmt -> nmcli rejects the profile and the "
                         "user is blamed for a wrong password" % (rel, psk_hits))
            print("  FAIL %s: psk=%d key-mgmt=0" % (rel, psk_hits))
            continue

        # 2. key-mgmt must be set at least as often as psk, so no single psk
        #    site is left bare
        if km_hits < psk_hits:
            fails.append("%s: %d psk site(s) but only %d key-mgmt site(s) - at "
                         "least one profile is created without key-mgmt"
                         % (rel, psk_hits, km_hits))
            print("  FAIL %s: psk=%d key-mgmt=%d" % (rel, psk_hits, km_hits))
            continue

        # 3. WPA3 must still be reachable, or we have "fixed" this by dropping
        #    the support the original change was chasing
        if not has_sae:
            fails.append("%s: sets key-mgmt but never tries 'sae' - WPA3-only "
                         "routers can no longer be joined" % rel)
            print("  FAIL %s: no sae fallback" % rel)
            continue

        print("  ok   %s: psk=%d key-mgmt=%d, sae fallback present"
              % (rel, psk_hits, km_hits))

    # 4. the exact broken shape must not reappear: a psk argument with no
    #    key-mgmt anywhere near it (same statement / same few lines)
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            continue
        body = strip_comments(p.read_text(encoding="utf-8", errors="replace"),
                              rel.endswith(".py"))
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if "wifi-sec.psk" not in line:
                continue
            window = "\n".join(lines[max(0, i - 6):i + 7])
            if "wifi-sec.key-mgmt" not in window:
                fails.append("%s:%d: wifi-sec.psk with no key-mgmt within 6 "
                             "lines -> %s" % (rel, i + 1, line.strip()[:70]))
                print("  FAIL %s:%d bare psk" % (rel, i + 1))

    print()
    if fails:
        for f in fails:
            print("  * %s" % f)
    print("checked %d file(s), %d failure(s)" % (checked, len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
