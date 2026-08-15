#!/usr/bin/env python3
"""Regression test for _mjpeg_fallback_policy().

RUN THIS ON THE DEVICE, not on a dev laptop - it imports the real backend, so
it needs aiohttp and the rest of the runtime deps:

    sudo python3 tests/test_mjpeg_policy.py                       # repo copy
    sudo python3 tests/test_mjpeg_policy.py /opt/magicbridge/core/magicbridge.py

Why this exists. Two settings interact and one pairing is fatal:

  * video.mode == "mjpeg" means MJPEG is the ONLY transport the unit has. It
    gets there either because "auto" resolved that way, or because an H.264
    launch failed and video.py's _fallback_or_fail() took over.
  * mjpeg_fallback == False makes the browser hard-gate the MJPEG <img> off.

Ship both together and the buyer gets a permanent "Connecting..." with a
working keyboard and mouse and no picture - the <img> never errors, so the
Retry button is never even revealed. build-image.sh used to force
mjpeg_fallback=True to dodge this, but that armed MJPEG on every unit,
including ones whose link cannot carry 1080p MJPEG (measured: 27 MB pulled and
a ~3 Mbit/s path driven to 6.9 Mbit/s, which is what stops WebRTC recovering).

The policy resolves it by deriving the answer from what the device is ACTUALLY
running. These cases pin that behaviour down.
"""
import importlib.util
import sys

DEFAULT = "src/core/magicbridge.py"

# (running mode, stored mjpeg_fallback, expected, what it protects)
CASES = [
    ("h264",  None,  False, "h264 running, nothing configured -> OFF, no flood"),
    ("h264",  False, False, "h264 running, explicitly off -> OFF"),
    ("h264",  True,  True,  "h264 running, operator forced it on -> ON, their call"),
    ("mjpeg", None,  True,  "AUTO-ENABLE: fell back to mjpeg -> ON"),
    ("mjpeg", False, True,  "AUTO-ENABLE beats a stale off -> ON, never a black screen"),
    ("mjpeg", True,  True,  "mjpeg running, configured on -> ON"),
]


def load(path):
    spec = importlib.util.spec_from_file_location("mb_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mb_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class FakeVideo:
    def __init__(self, mode):
        self.mode = mode


class ExplodingVideo:
    @property
    def mode(self):
        raise RuntimeError("probe blew up")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    mb = load(path)
    fails = 0

    for mode, stored, want, desc in CASES:
        cfg = {"mode": mode}
        if stored is not None:
            cfg["mjpeg_fallback"] = stored
        mb.video = FakeVideo(mode)
        mb._load_cfg = (lambda c: (lambda: {"video": c}))(cfg)
        got = mb._mjpeg_fallback_policy()
        ok = got == want
        fails += (not ok)
        print("  %s  %-58s got=%-5s want=%s" % ("PASS" if ok else "FAIL", desc, got, want))

    # A failing probe must never be allowed to answer "no video".
    mb.video = ExplodingVideo()
    mb._load_cfg = lambda: {"video": {"mjpeg_fallback": False}}
    got = mb._mjpeg_fallback_policy()
    ok = got is True
    fails += (not ok)
    print("  %s  %-58s got=%-5s want=True"
          % ("PASS" if ok else "FAIL", "video probe raises -> permissive, never black", got))

    print("\nFAILURES: %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
