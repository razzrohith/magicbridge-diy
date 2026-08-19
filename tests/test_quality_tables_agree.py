#!/usr/bin/env python3
"""The UI's quality map and the backend's MJPEG ladder must stay identical.

    python3 tests/test_quality_tables_agree.py

WHY. The picture-quality control and the auto-adapter step the SAME encoder
through two tables that live in different files and different languages:
_MJPEG_Q in src/web/index.html and MJPEG_LADDER in src/core/magicbridge.py.
Nothing links them, so they can drift silently - and when they do, the operator
picks a value the adapter does not recognise as a rung, so the adapter snaps to
its nearest one and quietly overrides the choice.

It also pins both to the value the factory image ships (build-image.sh sets
video.quality). The first version of this ladder ran entirely ABOVE that value,
so a shipped unit that touched the control jumped to MORE bandwidth than it left
the factory with, and the adapter could never ease back down to the safe value
it started on. That is the specific mistake this test exists to prevent.

Static: no device, no network, no browser.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = []

ui = (ROOT / "src/web/index.html").read_text(encoding="utf-8", errors="replace")
be = (ROOT / "src/core/magicbridge.py").read_text(encoding="utf-8", errors="replace")
bi = (ROOT / "src/provision/build-image.sh").read_text(encoding="utf-8", errors="replace")

m = re.search(r"_MJPEG_Q\s*=\s*\{([^}]*)\}", ui)
if not m:
    fails.append("could not find _MJPEG_Q in index.html")
    ui_vals = []
else:
    ui_vals = [int(v) for _, v in re.findall(r"(\d+)\s*:\s*(\d+)", m.group(1))]

m = re.search(r"MJPEG_LADDER\s*=\s*\[([^\]]*)\]", be)
if not m:
    fails.append("could not find MJPEG_LADDER in magicbridge.py")
    be_vals = []
else:
    be_vals = [int(x) for x in re.findall(r"\d+", m.group(1))]

print("UI  _MJPEG_Q values : %s" % ui_vals)
print("BE  MJPEG_LADDER    : %s" % be_vals)

if ui_vals and be_vals:
    if sorted(ui_vals, reverse=True) != sorted(be_vals, reverse=True):
        fails.append("the two tables disagree: UI %s vs backend %s - the adapter "
                     "would snap the operator's choice to a different rung"
                     % (sorted(ui_vals), sorted(be_vals)))
    else:
        print("  ok   both tables hold the same rungs")

    if be_vals != sorted(be_vals, reverse=True):
        fails.append("MJPEG_LADDER must be ordered sharpest-first (descending); "
                     "the adapter's index arithmetic depends on it: %s" % be_vals)
    else:
        print("  ok   ladder is ordered sharpest-first")

# the factory-pinned quality must be REACHABLE, or the unit can never get back
# to the setting it shipped with
m = re.search(r'c\["video"\]\["quality"\]\s*=\s*(\d+)', bi)
if not m:
    fails.append("could not find the factory quality pin in build-image.sh")
else:
    factory = int(m.group(1))
    print("factory-pinned quality: %d" % factory)
    if be_vals:
        if factory not in be_vals:
            fails.append("the factory quality %d is not a rung (%s), so the unit "
                         "can never return to what it shipped with" % (factory, be_vals))
        else:
            print("  ok   the factory value is a rung")
        if min(be_vals) > factory:
            fails.append("every rung (%s) is heavier than the factory value %d - "
                         "the adapter cannot ease off past what it shipped with"
                         % (be_vals, factory))
        else:
            print("  ok   the ladder can ease off below the factory value")

print()
for f in fails:
    print("  FAIL  %s" % f)
if not fails:
    print("  PASS  quality tables agree and bracket the factory value")
print("\nFAILURES: %d" % len(fails))
sys.exit(1 if fails else 0)
