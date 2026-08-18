#!/usr/bin/env python3
"""Headless smoke test for the web UI. Run this before deploying index.html.

    python3 tests/smoke_ui.py [path/to/index.html]

WHY. Three separate regressions shipped to the operator during development and
NONE were caught by the checks that existed:

  1. A stray </div> closed <aside id="sidebar"> early, so the right-hand rail
     rendered at the bottom-left. HTML was still well formed; JS still parsed.
  2. An IIFE ran at load and called a function that dereferenced a `const`
     declared 2000 lines later - a temporal-dead-zone ReferenceError that killed
     the whole inline script (no WebSocket, no status, no settings panel).
     `node --check` passes: TDZ is valid syntax, it only fails at runtime.
  3. Two cards were appended AFTER a section's closing tag, so they rendered in
     every section instead of one. Tree well formed, tags balanced.

The common thread: all three need the page to actually RUN. This loads it in
headless Chrome, stubs the network so no device is needed, and asserts the
things that were broken each time.

No npm dependencies - it drives Chrome directly and reads back the DOM.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# Functions defined near the END of the inline script. If the script aborted
# part-way (bug 2) these are missing, which is the cheapest reliable signal.
LATE_FUNCS = ["refreshJigglerStatus", "jigOnSwitch", "jigSchedSwitch",
              "railGo", "sysNavShow", "applyVideo", "connect", "ping"]

STUB = """<script>
// Stub the network BEFORE the page script runs: no device is needed to smoke it.
window.__smokeErrors = [];
window.addEventListener('error', e => window.__smokeErrors.push(String(e.message)));
window.fetch = async function(){ return { json: async()=>({ok:true, enabled:false,
  styles:{}, timezones:[], version:'0', stream:{}, display:{}, power:{}, services:{}}),
  text: async()=>'{}' }; };
window.WebSocket = function(){ this.readyState=0; this.send=function(){};
  this.close=function(){}; };
</script>
"""

ASSERT = """<script>
(function(){
  var out = {errors: (window.__smokeErrors||[]).slice(0,5), missing: [], sections: {}, dupes: [], notes: []};
  var LATE = %s;
  LATE.forEach(function(n){ if(typeof window[n] !== 'function') out.missing.push(n); });

  // layout landmarks
  var rail=document.getElementById('railnav'), side=document.getElementById('sidebar');
  out.railParent = rail && rail.parentElement ? (rail.parentElement.id||rail.parentElement.tagName) : null;
  out.sidebarParent = side && side.parentElement ? (side.parentElement.id||side.parentElement.tagName) : null;

  // duplicate ids
  var seen={}, dup=[];
  Array.prototype.forEach.call(document.querySelectorAll('[id]'), function(e){
    if(seen[e.id]) dup.push(e.id); else seen[e.id]=1;
  });
  out.duplicateIds = dup;

  // which cards are VISIBLE in each system section - a card visible in more
  // than one section is loose (bug 3)
  try{
    window._sideOpen = true;
    ['monitoring','peripherals','security','power','update'].forEach(function(cat){
      if(typeof railGo === 'function') railGo('system', cat);
      var vis = Array.prototype.filter.call(
        document.querySelectorAll('#panel-system .scard'), function(c){ return c.offsetParent !== null; });
      out.sections[cat] = vis.map(function(c){
        var t=c.querySelector('.scard-title');
        return t ? t.textContent.trim().split('\\n')[0].slice(0,40) : '?';
      });
    });
    var count={};
    Object.keys(out.sections).forEach(function(k){
      out.sections[k].forEach(function(t){ count[t]=(count[t]||0)+1; });
    });
    Object.keys(count).forEach(function(t){ if(count[t]>1) out.dupes.push(t+' x'+count[t]); });
  }catch(e){ out.notes.push('section walk failed: '+e); }

  var d=document.createElement('div');
  d.id='__smoke__';
  d.setAttribute('data-result', JSON.stringify(out));
  document.body.appendChild(d);
})();
</script>
"""


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "src/web/index.html"
    chrome = find_chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium found; cannot run the runtime smoke test.")
        print("      (install one, or run this on a machine that has it - the")
        print("       static checks in tests/test_html_structure.py still apply)")
        return 0

    src = open(path, encoding="utf-8").read()
    # stub goes first so it is in place before the page's own script runs
    if "<head>" in src:
        src = src.replace("<head>", "<head>\n" + STUB, 1)
    else:
        src = STUB + src
    src = src.replace("</body>", (ASSERT % json.dumps(LATE_FUNCS)) + "</body>", 1)

    tmpdir = tempfile.mkdtemp(prefix="mb-smoke-")
    tmp = os.path.join(tmpdir, "smoke.html")
    open(tmp, "w", encoding="utf-8").write(src)

    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--virtual-time-budget=4000", "--dump-dom",
           "--user-data-dir=" + os.path.join(tmpdir, "profile"),
           "file:///" + tmp.replace("\\", "/")]
    try:
        # encoding must be forced: the DOM contains emoji (the rail icons) and
        # Python would otherwise decode Chrome's stdout as the ANSI codepage.
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        dom = r.stdout.decode('utf-8', 'replace')
    except Exception as e:
        print("FAIL: could not run Chrome: %s" % e)
        return 1

    m = re.search(r'id="__smoke__" data-result="([^"]*)"', dom)
    if not m:
        print("FAIL: the assertion block never ran - the page script almost")
        print("      certainly died before finishing (this is bug class 2).")
        snippet = [l for l in dom.split("\n") if "Error" in l][:3]
        for l in snippet:
            print("      " + l.strip()[:120])
        return 1

    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    res = json.loads(raw)

    fails = []
    if res["missing"]:
        fails.append("script did not run to completion; missing: %s" % ", ".join(res["missing"]))
    if res["railParent"] != "main":
        fails.append("#railnav is inside %r, expected #main (layout will be wrong)" % res["railParent"])
    if res["sidebarParent"] != "main":
        fails.append("#sidebar is inside %r, expected #main" % res["sidebarParent"])
    if res["duplicateIds"]:
        fails.append("duplicate ids: %s" % ", ".join(res["duplicateIds"][:6]))
    if res["dupes"]:
        fails.append("cards visible in more than one section (loose): %s" % ", ".join(res["dupes"]))
    for e in res["errors"]:
        fails.append("uncaught error at load: %s" % e)

    print("SMOKE TEST  %s" % path)
    print("  chrome        : %s" % os.path.basename(chrome))
    print("  script ran    : %s" % ("yes" if not res["missing"] else "NO"))
    print("  rail parent   : #%s" % res["railParent"])
    for cat, cards in res["sections"].items():
        print("  %-12s: %d cards" % (cat, len(cards)))
    for n in res.get("notes", []):
        print("  note          : %s" % n)
    print()
    for f in fails:
        print("  FAIL  %s" % f)
    if not fails:
        print("  PASS  page loads, script completes, layout and sections correct")
    print("\nFAILURES: %d" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
