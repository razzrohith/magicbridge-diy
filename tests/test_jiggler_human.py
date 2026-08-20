#!/usr/bin/env python3
"""Offline realism check for the jiggler's "human" wander style.

Extracts the ACTUAL pure motion functions from src/core/magicbridge.py (no HID,
no import of the whole service) and simulates a long session, then asserts the
movement does NOT carry the signals cursor-analysis uses to flag automation:

  - straight-line paths (bots move in a line; a hand curves)
  - constant velocity (bots; a hand accelerates then decelerates)
  - integer-grid / identical-delta lock
  - perfectly periodic timing
  - unbounded drift to a screen corner
  - no corrective overshoot

Run:  python tests/test_jiggler_human.py
"""
import os, re, math, statistics as st, random

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "core", "magicbridge.py")
src = open(SRC, encoding="utf-8").read()

# pull the three module-level pure functions verbatim
blk = src[src.index("def _wander_pick_dir"): src.index("class MouseJiggler")]
ns = {"_jrand": random, "_jmath": math}
exec(blk, ns)
_wander_pick_dir = ns["_wander_pick_dir"]
_gen_human_stroke = ns["_gen_human_stroke"]

# pull the real "human" style params
m = re.search(r'"human":\s*(\{[^}]*\})', src)
cfg = eval(m.group(1), {"__builtins__": {}}, {})
assert cfg["mode"] == "wander"
MAXD = cfg["max_drift"]

random.seed(20260820)   # deterministic, but exercises the real RNG-driven code

def steps_of(L, ux, uy, short):
    return list(_gen_human_stroke(L, ux, uy, short, cfg))

def path_from(steps):
    """Absolute path points from the relative deltas."""
    x = y = 0; pts = [(0, 0)]
    for dx, dy, _ in steps:
        x += dx; y += dy; pts.append((x, y))
    return pts

PASS = FAIL = 0
def chk(name, cond, extra=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok; FAIL += (not ok)
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + extra) if extra else ""))

# ---- per-stroke geometry/timing over many long strokes --------------------
straight, cov_speed, uniq_frac, overshoot, end_err = [], [], [], 0, []
edge_pause, mid_pause = [], []
NL = 400
for _ in range(NL):
    ux, uy = _wander_pick_dir([0.0, 0.0], MAXD)          # low drift -> free dir
    L = random.uniform(*cfg["stroke_px"])
    steps = steps_of(L, ux, uy, False)
    pts = path_from(steps)
    # straightness = path length / straight-line distance
    plen = sum(math.hypot(pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1]) for i in range(len(pts)-1))
    dist = math.hypot(pts[-1][0], pts[-1][1])
    if dist > 1:
        straight.append(plen / dist)
    # endpoint accuracy vs intended target
    end_err.append(math.hypot(pts[-1][0]-ux*L, pts[-1][1]-uy*L))
    # per-step speed profile
    speeds = [math.hypot(dx, dy) for dx, dy, _ in steps if (dx or dy)]
    if len(speeds) > 4 and st.mean(speeds) > 0:
        cov_speed.append(st.pstdev(speeds) / st.mean(speeds))
        # peak speed should sit in the middle, not at an end
    # delta variety (integer-grid lock check)
    deltas = [(dx, dy) for dx, dy, _ in steps]
    uniq_frac.append(len(set(deltas)) / max(1, len(deltas)))
    # overshoot: does the path go PAST the target along the direction, then return?
    proj = [px*ux + py*uy for px, py in pts]
    if max(proj) > dist + 2:
        overshoot += 1
    # timing: ends slower than middle
    ps = [p for _, _, p in steps]
    q = max(1, len(ps)//4)
    edge_pause.append(st.mean(ps[:q] + ps[-q:]))
    mid_pause.append(st.mean(ps[q:-q] or ps))

chk("path is curved, not a straight line", st.median(straight) > 1.02,
    f"median length/dist = {st.median(straight):.3f}")
chk("speed is non-constant (accel/decel)", st.median(cov_speed) > 0.30,
    f"median speed CoV = {st.median(cov_speed):.2f}")
chk("no integer-grid / identical-delta lock", st.median(uniq_frac) > 0.30,
    f"median unique-delta frac = {st.median(uniq_frac):.2f}")
chk("endpoint lands on target", max(end_err) <= 2.0,
    f"worst endpoint error = {max(end_err):.2f}px")
chk("corrective overshoot appears on some strokes", overshoot/NL > 0.20,
    f"{overshoot}/{NL} strokes overshoot = {overshoot/NL:.0%}")
chk("timing not flat: ends pause longer than middle",
    st.mean(edge_pause) > st.mean(mid_pause) * 1.15,
    f"edge {st.mean(edge_pause)*1000:.1f}ms vs mid {st.mean(mid_pause)*1000:.1f}ms")
# per-step pauses vary a lot (not periodic)
allp = []
for _ in range(50):
    allp += [p for _, _, p in steps_of(random.uniform(*cfg["stroke_px"]), 1, 0, False)]
chk("per-step timing is non-periodic", st.pstdev(allp)/st.mean(allp) > 0.20,
    f"pause CoV = {st.pstdev(allp)/st.mean(allp):.2f}")

# ---- drift stays bounded over a long continuous session -------------------
drift = [0.0, 0.0]
mags, angles = [], []
N = 1000
for i in range(N):
    ux, uy = _wander_pick_dir(drift, MAXD)
    angles.append(math.atan2(uy, ux))
    short = random.random() < cfg["short_prob"]
    L = random.uniform(*(cfg["short_px"] if short else cfg["stroke_px"]))
    drift[0] += ux * L; drift[1] += uy * L
    mags.append(math.hypot(*drift))
first_half = st.mean(mags[:N//2]); second_half = st.mean(mags[N//2:])
chk("drift stays bounded (no runaway to a corner)", max(mags) < MAXD * 2.4,
    f"max|drift| = {max(mags):.0f}px (cap ~{MAXD*2.4:.0f})")
chk("drift does not grow over time (mean-reverting)", second_half < first_half * 1.5,
    f"1st-half {first_half:.0f}px vs 2nd-half {second_half:.0f}px")

# direction diversity when free (all four quadrants used)
free = [math.atan2(*_wander_pick_dir([0.0, 0.0], MAXD)[::-1]) for _ in range(400)]
quads = {int((a % (2*math.pi)) // (math.pi/2)) for a in free}
chk("directions span all quadrants (not one axis)", len(quads) == 4,
    f"quadrants used = {sorted(quads)}")

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
