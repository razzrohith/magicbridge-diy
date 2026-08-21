#!/usr/bin/env python3
"""After a reload, the Picture-quality dropdown must show the unit's REAL setting.

    python3 tests/test_quality_seeding.py

WHY. The <option> values are min_qp numbers (20/26/30/34/40), but a SHIPPED unit
runs MJPEG, where the live setting is a --quality number on a different scale
entirely. Seeding the select straight from the live value therefore matched no
option, the select silently kept its markup default ("Balanced"), and the
operator's next Apply POSTed that default over the quality they had actually
saved. That is the same silent-overwrite the seeding block was written to
prevent, arriving through the other lever.

Two things this pins down, both of which were wrong at some point:
  1. the value is mapped through _MJPEG_Q, nearest-rung, so the off-ladder
     values the MJPEG bandwidth presets write (10, 12, 25) still land sanely;
  2. the branch is chosen from d.stream.mode, NOT from _adaptField. The seeding
     is a ONE-SHOT on the first status poll, which routinely beats the separate
     /api/stream/adapt fetch that sets _adaptField - so keying on _adaptField
     meant the branch never ran at all. The mode arrives in the same payload.

Drives the shipped index.html in headless Chrome against a stubbed MJPEG device.
No unit, no network. SKIPs (does not fail) when Chrome is absent.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

CH = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
      "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]

# (quality the device reports, option the dropdown must select)
# _MJPEG_Q is {16:24, 20:20, 26:16, 30:12, 34:9, 40:6}, so these are nearest-rung:
#   10 -> 9  (option 34), because |9-10| = 1 beats |12-10| = 2
#   25 -> 24 (option 16 "Max"), the sharpest rung now (|24-25|=1 beats |20-25|=5)
CASES = [(12, "30"), (9, "34"), (6, "40"), (16, "26"),
         (20, "20"), (10, "34"), (25, "16")]


def main():
    chrome = next((c for c in CH if os.path.exists(c)), None) or shutil.which("chrome")
    if not chrome:
        print("SKIP: no Chrome/Chromium found.")
        return 0
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "src/web/index.html"), encoding="utf-8").read()

    print("QUALITY DROPDOWN SEEDING (MJPEG unit)")
    fails = 0
    for live_q, expect in CASES:
        stub = ("<script>\n"
                "window.fetch=async function(u){u=String(u);\n"
                " if(u.indexOf('/api/stream/adapt')>-1)\n"
                "  return {json:async()=>({ok:true,adapt:{ceiling:%d,field:'quality',"
                "min_qp:%d,adapted:false}})};\n"
                " return {json:async()=>({ok:true,enabled:false,styles:{},timezones:[],"
                "version:'0',stream:{mode:'mjpeg',quality:%d,fps:20,"
                "resolution:'1920x1080'},display:{},power:{},services:{},viewers:[]}),"
                "text:async()=>'{}'};};\n"
                "window.WebSocket=function(){this.readyState=0;this.send=function(){};"
                "this.close=function(){};};\n"
                "</script>" % (live_q, live_q, live_q))
        # reset the one-shot and leave _adaptField EMPTY: that is the real
        # first-load ordering, and the case that used to fail.
        drive = ("<script>(async function(){try{_vidSelsLoaded=false;_adaptCeiling=0;"
                 "_adaptField='';await refreshStatus();}catch(e){}"
                 "var q=document.getElementById('sel-quality');"
                 "var d=document.createElement('div');d.id='__r__';"
                 "d.setAttribute('data-v',q?q.value:'none');"
                 "document.body.appendChild(d);})();</script>")
        page = src.replace("<head>", "<head>\n" + stub, 1).replace("</body>", drive + "</body>", 1)
        td = tempfile.mkdtemp(prefix="mb-seed-")
        p = os.path.join(td, "s.html")
        open(p, "w", encoding="utf-8").write(page)
        try:
            dom = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--virtual-time-budget=4000", "--dump-dom",
                 "--user-data-dir=" + os.path.join(td, "p"),
                 "file:///" + p.replace("\\", "/")],
                capture_output=True, timeout=90).stdout.decode("utf-8", "replace")
        except Exception as e:
            print("  FAIL could not run Chrome: %s" % e)
            return 1
        m = re.search(r'id="__r__" data-v="([^"]*)"', dom)
        got = m.group(1) if m else "NO-RESULT"
        ok = got == expect
        if not ok:
            fails += 1
        print("  device quality %-3s -> dropdown %-9s expected %-4s %s"
              % (live_q, got, expect, "ok" if ok else "MISMATCH"))

    print()
    print("  PASS  the dropdown reports the unit's real setting" if not fails
          else "  FAIL  the dropdown misreports the setting")
    print("\nFAILURES: %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
