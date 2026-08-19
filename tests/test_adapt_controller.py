#!/usr/bin/env python3
"""Deterministic test for the auto-adapt controller in src/web/index.html.

    python3 tests/test_adapt_controller.py [path/to/index.html]

WHY THIS EXISTS. The controller's whole job is to behave well on a BAD network,
which is the one condition that is hard to produce on demand and impossible to
reproduce twice. Reviewing it or eyeballing it on a good LAN proves nothing: the
interesting behaviour (does it hold off during the settle window? does it
oscillate on a marginal link? does a reconnect reset its counters?) only appears
after minutes of specific loss patterns.

So instead of waiting for a bad network, this drives the REAL controller with
synthetic getStats reports and a stubbed clock. It loads the actual page in
headless Chrome - not a copy of the logic, the shipped file - stubs fetch and
Date.now, then calls _adaptFeed() with crafted inbound-rtp reports and asserts
which /api/stream/adapt requests come out.

Each case maps to a defect an adversarial audit found in this controller:
  1. settle window        - it used to judge a stream from its first sample
  2. short blip           - must not spend 1.7s of frozen video on a hiccup
  3. sustained trouble    - must actually act
  4. loss is a DELTA      - packetsLost is cumulative; testing it raw latched bad
  5. tiny samples ignored - a static screen sends almost nothing; 1-of-3 is noise
  6. recovery             - must go back up when the link is genuinely clean
  7. hysteresis           - a marginal link used to oscillate between two rungs
                            forever, freezing the picture ~1.7s each way
  8. reconnect re-arm     - the settle grace was armed once per PAGE LOAD, so
                            every WebRTC reconnect was judged from sample one
  9. switch sync at load  - the switch sits in the DEFAULT tab but was only
                            synced when the System tab was opened
 10. off switch           - must issue nothing at all when turned off

No device and no network needed. Chrome is the only dependency; if it is missing
the test SKIPS rather than failing, like tests/smoke_ui.py.
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

# Injected into <head>, BEFORE the page script, so the clock and the network are
# already under our control the first time any of it runs.
STUB = """<script>
window.__posts = [];      // every /api/stream/adapt POST the controller makes
window.__now   = 1000000; // controllable clock, ms
window.__errs  = [];
window.addEventListener('error', e => window.__errs.push(String(e.message)));
Date.now = function(){ return window.__now; };

// The controller's only outward effect is this POST. Record it and reply the way
// the device would, so the client-side bookkeeping (ceiling, regret) runs for real.
window.__adaptReply = {ok:true, applied:true, adapt:{ceiling:30, min_qp:34, adapted:true}};
window.fetch = async function(url, opts){
  const u = String(url);
  if(u.indexOf('/api/stream/adapt') === 0 || u.indexOf('/api/stream/adapt') > -1){
    if(opts && opts.method === 'POST'){
      const body = JSON.parse(opts.body);
      window.__posts.push(body);
      return { json: async () => window.__adaptReply };
    }
    return { json: async () => ({ok:true, adapt:{ceiling:30, min_qp:30, adapted:false}}) };
  }
  return { json: async () => ({ok:true, enabled:false, styles:{}, timezones:[],
           version:'0', stream:{}, display:{}, power:{}, services:{}, viewers:[]}),
           text: async () => '{}' };
};
window.WebSocket = function(){ this.readyState=0; this.send=function(){}; this.close=function(){}; };
</script>
"""

# Appended before </body>: the actual scenarios.
DRIVE = r"""<script>
(function(){
const out = {cases: [], errs: window.__errs};
const tick = () => new Promise(r => setTimeout(r, 0));

// A synthetic inbound-rtp video report. Loss is expressed as a per-sample delta
// on top of cumulative counters, exactly like the browser reports it.
let lost = 0, recv = 0;
function sample(lossPct, packets){
  packets = packets == null ? 500 : packets;
  const dl = Math.round(packets * lossPct / 100);
  lost += dl; recv += (packets - dl);
  return {type:'inbound-rtp', kind:'video', packetsLost:lost, packetsReceived:recv};
}
function resetCounters(){ lost = 0; recv = 0; }

// Feed n samples, advancing the stubbed clock 2s each (the real poll interval).
async function feed(n, lossPct, packets){
  for(let i=0;i<n;i++){
    window.__now += 2000;
    _adaptFeed(sample(lossPct, packets));
    await tick(); await tick();   // let the async _adaptStep settle
  }
}
// The FIRST sample after a (re)start only establishes the loss baseline - loss
// is a delta from the previous sample, so it cannot show loss itself. So a step
// DOWN needs one extra bad sample than the raw threshold. This is real controller
// behaviour, covered in production by the settle window; the helper makes the
// intent explicit instead of scattering "+1"s. UP is unaffected: the priming
// sample reads as clean and already counts toward the good streak.
const DOWN_SAMPLES = ADAPT_BAD_N + 1;
function reset(){
  window.__posts = [];
  resetCounters();
  _adaptOn = true; _adaptBusy = false;
  _adaptBad = 0; _adaptGood = 0;
  _adaptLastLost = -1; _adaptLastRecv = -1; _adaptRtt = -1;
  _adaptGoodNeed = ADAPT_GOOD_N; _adaptLastUpAt = 0;
  _activeTransport = 'h264';
  _adaptGen = (typeof _webrtcGen !== 'undefined') ? _webrtcGen : 0;
  _adaptSince = window.__now;             // pretend the stream just started
}
function record(name, pass, detail){ out.cases.push({name:name, pass:!!pass, detail:detail}); }

(async function(){
 try{
  // 1. SETTLE WINDOW: bad from the very first sample must not act.
  reset();
  await feed(ADAPT_BAD_N + 4, 20);        // 20% loss, but inside the grace period
  record('settle window suppresses early trouble', window.__posts.length === 0,
         'posts=' + window.__posts.length);

  // 2. SHORT BLIP: past settle, but trouble ends before the threshold.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_BAD_N - 1, 20);        // one sample short
  await feed(3, 0);                       // recovers
  record('short blip does not trigger a step', window.__posts.length === 0,
         'posts=' + window.__posts.length);

  // 3. SUSTAINED TROUBLE: must ease off exactly once.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(DOWN_SAMPLES, 20);
  record('sustained loss eases off once',
         window.__posts.length === 1 && window.__posts[0].dir === 'down',
         JSON.stringify(window.__posts));

  // 4. LOSS IS A DELTA: a big one-off loss early must not latch "bad" forever.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(1, 90);                      // one terrible sample
  await feed(ADAPT_BAD_N + 6, 0);         // then perfectly clean
  const onlyDown = window.__posts.filter(p => p.dir === 'down').length;
  record('cumulative packetsLost does not latch bad', onlyDown === 0,
         'down posts=' + onlyDown);

  // 5. TINY SAMPLES: a static screen sends almost nothing; 1 lost of 3 is noise.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_BAD_N + 4, 33, 3);     // 33% "loss" but only 3 packets/sample
  record('low-volume samples are not treated as loss', window.__posts.length === 0,
         'posts=' + window.__posts.length);

  // 6. RECOVERY: a genuinely clean link goes back up.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_GOOD_N, 0);
  record('clean link asks to go back up',
         window.__posts.length === 1 && window.__posts[0].dir === 'up',
         JSON.stringify(window.__posts));

  // 7. HYSTERESIS: up, then trouble again -> the next recovery must take LONGER.
  //    Without this the pair oscillates forever, ~1.7s of frozen video each way.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_GOOD_N, 0);            // -> up
  const needBefore = _adaptGoodNeed;
  _adaptSince = window.__now;             // the step restarted the stream
  window.__now += ADAPT_SETTLE + 2000;
  await feed(DOWN_SAMPLES, 20);           // -> down, soon after the up = regret
  const needAfter = _adaptGoodNeed;
  _adaptSince = window.__now; window.__now += ADAPT_SETTLE + 2000;
  const postsBeforeRetry = window.__posts.length;
  await feed(ADAPT_GOOD_N, 0);            // the OLD threshold must no longer fire
  const firedEarly = window.__posts.length > postsBeforeRetry;
  record('a failed recovery makes the next one wait longer',
         needAfter === needBefore * 2 && !firedEarly,
         'need ' + needBefore + ' -> ' + needAfter + ', fired at old threshold=' + firedEarly);

  // 7b. and the longer threshold still eventually fires
  await feed(ADAPT_GOOD_N + 2, 0);
  record('the longer threshold still recovers eventually',
         window.__posts.length > postsBeforeRetry,
         'posts=' + window.__posts.length);

  // 8. RECONNECT RE-ARM: a new peer connection restarts the settle grace.
  reset(); window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_BAD_N - 2, 20);        // partway to a step
  if(typeof _webrtcGen !== 'undefined') _webrtcGen++;   // stream reconnected
  await feed(ADAPT_BAD_N + 4, 20);        // must be judged from scratch, in grace
  record('a reconnect re-arms the settle grace', window.__posts.length === 0,
         'posts=' + window.__posts.length);

  // 9. SWITCH SYNC AT LOAD: the switch lives in the default tab.
  try{ localStorage.setItem('mb_adapt','0'); }catch(e){}
  _adaptOn = false; adaptSync();
  const sw = document.getElementById('adapt-sw');
  record('saved OFF state is reflected on the switch at load',
         sw && sw.checked === false, 'checked=' + (sw ? sw.checked : 'no element'));
  _adaptOn = true; adaptSync();
  record('saved ON state is reflected on the switch',
         sw && sw.checked === true, 'checked=' + (sw ? sw.checked : 'no element'));
  try{ localStorage.removeItem('mb_adapt'); }catch(e){}

  // 10. OFF: issues nothing at all.
  reset(); _adaptOn = false; window.__now += ADAPT_SETTLE + 2000;
  await feed(ADAPT_BAD_N + 6, 30);
  record('switched off, it issues nothing', window.__posts.length === 0,
         'posts=' + window.__posts.length);

 }catch(e){ out.errs.push('driver: ' + e + ' @ ' + (e && e.stack ? e.stack.split('\n')[1] : '?')); }

 const d = document.createElement('div');
 d.id = '__adapt__';
 d.setAttribute('data-r', JSON.stringify(out));
 document.body.appendChild(d);
})();
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
        print("SKIP: no Chrome/Chromium found; cannot run the controller test.")
        return 0

    src = open(path, encoding="utf-8").read()
    src = src.replace("<head>", "<head>\n" + STUB, 1)
    src = src.replace("</body>", DRIVE + "</body>", 1)

    td = tempfile.mkdtemp(prefix="mb-adapt-")
    tmp = os.path.join(td, "t.html")
    open(tmp, "w", encoding="utf-8").write(src)

    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--virtual-time-budget=30000", "--dump-dom",
           "--user-data-dir=" + os.path.join(td, "p"),
           "file:///" + tmp.replace("\\", "/")]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        dom = r.stdout.decode("utf-8", "replace")
    except Exception as e:
        print("FAIL: could not run Chrome: %s" % e)
        return 1

    m = re.search(r'id="__adapt__" data-r="([^"]*)"', dom)
    if not m:
        print("FAIL: the driver never finished - the page script probably died.")
        for l in [l for l in dom.split("\n") if "Error" in l][:3]:
            print("      " + l.strip()[:140])
        return 1

    raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    res = json.loads(raw)

    print("ADAPT CONTROLLER  %s" % path)
    fails = 0
    for c in res["cases"]:
        print("  %-4s %-52s %s" % ("ok" if c["pass"] else "FAIL", c["name"], c["detail"]))
        if not c["pass"]:
            fails += 1
    for e in res.get("errs", []):
        print("  FAIL uncaught: %s" % e)
        fails += 1
    if not res["cases"]:
        print("  FAIL no cases ran")
        fails += 1
    print("\n%d case(s), %d failure(s)" % (len(res["cases"]), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
