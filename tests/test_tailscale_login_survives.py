#!/usr/bin/env python3
"""The Tailscale login process must OUTLIVE the request that started it.

    python3 tests/test_tailscale_login_survives.py

WHY. `tailscale up` prints a login URL and then BLOCKS until the person
approves the device in their browser. That waiting process is what completes
the handshake. Both panels used to kill it seconds later to scrape the URL
(`--timeout=3s`, or Popen with output discarded), which ABORTS the login: the
node appears in the tailnet while the command lives and drops the moment it
dies. Reported from hardware as "it connected for a second, then went offline",
and a retry then failed because the dead attempt still held the client lock.

This asserts the shape of the fix without needing a tailnet: the command is
started detached, its output is captured to a file, nothing kills it, and no
short --timeout is used to truncate it.

Static: no device, no network, no Tailscale account.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = []


def check(name, ok, detail=""):
    print("  %-58s %s %s" % (name, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


mb = (ROOT / "src/core/magicbridge.py").read_text(encoding="utf-8", errors="replace")
st = (ROOT / "src/dashboard/stealth-dashboard.py").read_text(encoding="utf-8", errors="replace")


def code_only(text):
    """Drop comment lines so the fix's own explanation is not read as code."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


mbc, stc = code_only(mb), code_only(st)

# 1. nothing may run `tailscale up` with a short timeout to scrape the URL
for name, body in (("magicbridge.py", mbc), ("stealth-dashboard.py", stc)):
    bad = re.findall(r'"tailscale"\s*,\s*"up"[^\]]*--timeout', body)
    check("%s: no `tailscale up --timeout` (that aborts login)" % name, not bad,
          "" if not bad else str(bad[:1]))

# 2. the login must be started detached so it survives the request/restart
check("magicbridge.py: login started with start_new_session",
      "start_new_session=True" in mbc)
check("stealth-dashboard.py: ts_up started with start_new_session",
      "start_new_session=True" in stc)

# 3. its output must be captured, not discarded - the URL lives there
check("magicbridge.py: login output goes to the shared log",
      "tailscale-login.log" in mbc)
check("stealth-dashboard.py: ts_up output goes to the shared log",
      "tailscale-login.log" in stc)

# 4. a status GET must NOT start its own `tailscale up`
#    (every poll used to abort the login in progress)
get_fn = re.search(r"async def api_tailscale_get.*?(?=\nasync def |\Z)", mb, re.S)
if not get_fn:
    check("found api_tailscale_get", False)
else:
    g = code_only(get_fn.group(0))
    starts_up = re.search(r'\[\s*"tailscale"\s*,\s*"up"', g) is not None
    check("GET /api/tailscale does not run `tailscale up`", not starts_up)
    check("GET reads the login URL from the log instead",
          "tailscale-login.log" in g)

# 5. a stale attempt must be cleared, or the retry hits a held client lock
check("magicbridge.py: clears a previous attempt before starting",
      'pkill' in mbc and 'tailscale up' in mbc)

print()
print("FAILURES: %d" % len(fails))
sys.exit(1 if fails else 0)
