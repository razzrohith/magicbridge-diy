"""Load the fleet console in headless Chrome, exercise its logic, read back."""
import os, subprocess, tempfile, json, re, shutil

CH = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
chrome = next((c for c in CH if os.path.exists(c)), None) or shutil.which("chrome")
src = open("tools/fleet-console.html", encoding="utf-8").read()

# drive it: add two units (one bare host, one full url, one junk url), then a
# rejected javascript: url, and dump the resulting model + a couple of asserts.
DRIVE = """
<script>
window.__r = {steps:[], errs:[]};
window.addEventListener('error', e=>window.__r.errs.push(String(e.message)));
window.__done = (function(){
  try{
    localStorage.removeItem('mb_fleet_units');
    units.length = 0;
    document.getElementById('f-name').value='Office tower';
    document.getElementById('f-url').value='100.100.200.96';
    addUnit();
    document.getElementById('f-name').value='Rack 2';
    document.getElementById('f-url').value='https://mb.local/';
    addUnit();
    window.__r.count = units.length;
    window.__r.urls = units.map(u=>u.url);
    window.__r.names = units.map(u=>u.name);
    window.__r.cleanBad = cleanUrl('javascript:alert(1)');
    window.__r.cleanData = cleanUrl('data:text/html,x');
    window.__r.rows = document.querySelectorAll('.unit').length;
    window.__r.hasOpen = !!document.querySelector('.unit .pri');
  }catch(e){ window.__r.errs.push('drive: '+e); }
  var d=document.createElement('div'); d.id='__out__';
  d.setAttribute('data-r', JSON.stringify(window.__r));
  document.body.appendChild(d);
  return true;
})();
</script>
"""
src = src.replace("</body>", DRIVE + "</body>", 1)
td = tempfile.mkdtemp(prefix="mb-fleet-")
tmp = os.path.join(td, "f.html"); open(tmp, "w", encoding="utf-8").write(src)
cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
       "--virtual-time-budget=3000", "--dump-dom",
       "--user-data-dir=" + os.path.join(td, "p"),
       "file:///" + tmp.replace("\\", "/")]
dom = subprocess.run(cmd, capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
m = re.search(r'id="__out__" data-r="([^"]*)"', dom)
if not m:
    print("FAIL: driver did not run"); raise SystemExit(1)
r = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;","<").replace("&gt;",">").replace("&#39;","'"))
print(json.dumps(r, indent=1))
ok = True
if r.get("count") != 2: print("FAIL count"); ok=False
if r.get("urls") != ["https://100.100.200.96", "https://mb.local"]: print("FAIL urls"); ok=False
if r.get("names") != ["Office tower", "Rack 2"]: print("FAIL names"); ok=False
if r.get("cleanBad") is not None: print("FAIL javascript: url not rejected"); ok=False
if r.get("cleanData") is not None: print("FAIL data: url not rejected"); ok=False
if r.get("rows") != 2 or not r.get("hasOpen"): print("FAIL render"); ok=False
if r.get("errs"): print("FAIL errors:", r["errs"]); ok=False
print("\n" + ("PASS: fleet console logic ok" if ok else "FAILURES present"))
raise SystemExit(0 if ok else 1)
