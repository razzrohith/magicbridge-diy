#!/usr/bin/env python3
"""MagicBridge Stealth Dashboard
   bcrypt auth · CSRF protection · WCAG AA UI
   USB identity · MAC spoofing · WiFi · Tailscale · DuckDNS · logs · backup
"""
import json, os, re, subprocess, secrets, time, datetime, hashlib, hmac, logging
from pathlib import Path
from flask import (Flask, jsonify, request, render_template_string,
                   session, redirect, Response)

# ── bcrypt (preferred) with SHA-256 fallback ──────────────────────────────────
try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_PATH     = "/etc/magicbridge/config.json"
USB_DIR         = "/sys/kernel/config/usb_gadget/g1"
GADGET_SH       = "/usr/local/bin/mb-gadget.sh"
AUTH_LOG        = "/var/log/magicbridge-auth.log"
SESS_LOG        = "/var/log/magicbridge-sessions.log"
SESSION_TIMEOUT = 1800   # 30 min idle

# NOTE: the stealth panel and the main KVM page used to share one session
# cookie (logging into either unlocked both). They now have fully independent
# passwords and sessions by design — a compromised main-page password no
# longer exposes the admin panel. This panel uses Flask's own session only.

# Default USB identity (Logitech K120 — what install.sh sets up)
ORIG = {
    "manufacturer": "Logitech",
    "product":      "USB Keyboard K120",
    "serial":       "12AB34CD",
    "idVendor":     "0x046d",
    "idProduct":    "0xc31c",
}

USB_PROFILES = [
    {"name":"Logitech K120",        "mfr":"Logitech",   "prod":"USB Keyboard K120",      "vid":"0x046d","pid":"0xc31c","pfx":"LGK"},
    {"name":"Microsoft Wired 600",  "mfr":"Microsoft",  "prod":"Wired Keyboard 600",      "vid":"0x045e","pid":"0x0750","pfx":"MSK"},
    {"name":"Dell KB216",           "mfr":"Dell",       "prod":"KB216 Wired Keyboard",    "vid":"0x413c","pid":"0x2003","pfx":"DEL"},
    {"name":"HP KU-0316",           "mfr":"HP",         "prod":"KU-0316 Keyboard",        "vid":"0x03f0","pid":"0x0224","pfx":"HPK"},
    {"name":"Corsair K55 RGB",      "mfr":"Corsair",    "prod":"K55 RGB Keyboard",        "vid":"0x1b1c","pid":"0x1b48","pfx":"COR"},
    {"name":"Apple Magic Keyboard", "mfr":"Apple Inc.", "prod":"Magic Keyboard",          "vid":"0x05ac","pid":"0x0267","pfx":"APL"},
]

LOG_SOURCES = {
    "auth":      AUTH_LOG,
    "sessions":  SESS_LOG,
    "nginx":     "/var/log/nginx/access.log",
    "nginx-err": "/var/log/nginx/error.log",
    "system":    "/var/log/syslog",
    "magicbridge": "/var/log/magicbridge.log",
}

# ── Auth logging ──────────────────────────────────────────────────────────────
_al = logging.getLogger("magicbridge.stealth")
_al.setLevel(logging.INFO)
try:
    _fh = logging.FileHandler(AUTH_LOG)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _al.addHandler(_fh)
except Exception:
    pass

# ── Progressive login delay ───────────────────────────────────────────────────
_login_fails: dict = {}

def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or
            request.headers.get("X-Real-IP") or
            request.remote_addr or "").split(",")[0].strip()

def _apply_delay(ip: str):
    n = _login_fails.get(ip, 0)
    if n > 0:
        time.sleep(min(n, 10))

def _record_fail(ip: str):
    _login_fails[ip] = _login_fails.get(ip, 0) + 1

def _record_ok(ip: str):
    _login_fails.pop(ip, None)

# ── Password helpers ──────────────────────────────────────────────────────────
def _hash_pw(pw: str) -> str:
    if _HAS_BCRYPT:
        return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    return "sha256:" + hashlib.sha256(pw.encode()).hexdigest()

def _check_pw(pw: str, stored: str) -> bool:
    if _HAS_BCRYPT and stored.startswith("$2"):
        return _bcrypt.checkpw(pw.encode(), stored.encode())
    raw = stored.removeprefix("sha256:")
    return hashlib.sha256(pw.encode()).hexdigest() == raw

# ── Config helpers ────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        return json.loads(Path(CONFIG_PATH).read_text())
    except Exception:
        return {}

def _save(cfg: dict):
    Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))

def _ensure_defaults(cfg: dict) -> dict:
    auth = cfg.setdefault("auth", {})
    if not auth.get("password_hash"):
        auth["password_hash"] = _hash_pw("stealthbridge")
        _save(cfg)
    if not auth.get("secret_key"):
        auth["secret_key"] = secrets.token_hex(32)
        _save(cfg)
    return cfg

def _boot():
    cfg = _load()
    _ensure_defaults(cfg)
    app.secret_key = cfg["auth"]["secret_key"]

# ── CSRF ──────────────────────────────────────────────────────────────────────
def _csrf_ok() -> bool:
    tok = (request.headers.get("X-CSRF-Token") or request.form.get("_csrf", ""))
    return tok == session.get("csrf")

def _fresh_login_csrf() -> str:
    t = secrets.token_hex(32)
    session["login_csrf"] = t
    return t

# ── Auth helpers ──────────────────────────────────────────────────────────────
def _authed() -> bool:
    if session.get("ok"):
        if time.time() - session.get("t", 0) > SESSION_TIMEOUT:
            session.clear()
        else:
            session["t"] = time.time()
            return True
    return False

def _stealth(path: str = "") -> str:
    return "https://" + request.host + "/stealth/" + path.lstrip("/")

# ── USB helpers ───────────────────────────────────────────────────────────────
def _usb_r(rel: str) -> str:
    try:
        return Path(f"{USB_DIR}/{rel}").read_text().strip()
    except Exception:
        return ""

def _usb_w(rel: str, val: str):
    try:
        Path(f"{USB_DIR}/{rel}").write_text(val + "\n")
    except Exception:
        pass

def _rebind(fn):
    udc = _usb_r("UDC")
    _usb_w("UDC", "")
    time.sleep(0.3)
    fn()
    time.sleep(0.3)
    if udc:
        _usb_w("UDC", udc)

def _apply_usb(mfr: str, prod: str, ser: str, vid: str = None, pid: str = None):
    """Apply USB identity to live configfs and persist to config.json."""
    def _do():
        _usb_w("strings/0x409/manufacturer", mfr)
        _usb_w("strings/0x409/product",      prod)
        _usb_w("strings/0x409/serialnumber", ser)
        if vid: _usb_w("idVendor",  vid)
        if pid: _usb_w("idProduct", pid)
    _rebind(_do)
    # Persist so mb-gadget.sh applies identity on next reboot
    cfg = _load()
    usb = cfg.setdefault("usb", {})
    usb.update({"manufacturer": mfr, "product": prod, "serial": ser})
    if vid: usb["idVendor"]  = vid
    if pid: usb["idProduct"] = pid
    _save(cfg)

def _rand_serial(pfx: str = "MB") -> str:
    return pfx + secrets.token_hex(4).upper()

# ── Network helpers ───────────────────────────────────────────────────────────
def _cur_mac(iface: str = "eth0") -> str:
    try:
        return Path(f"/sys/class/net/{iface}/address").read_text().strip()
    except Exception:
        return ""

def _set_mac(iface: str, mac: str):
    for cmd in [["ip","link","set",iface,"down"],
                ["ip","link","set",iface,"address",mac],
                ["ip","link","set",iface,"up"]]:
        subprocess.run(cmd, capture_output=True)

def _persist_mac(iface: str, mac: str):
    cfg = _load()
    cfg.setdefault("mac_persist", {})[iface] = mac
    _save(cfg)
    _write_mac_svc(cfg)

def _write_mac_svc(cfg: dict):
    persist = cfg.get("mac_persist", {})
    valid   = {k: v for k, v in persist.items()
               if v and re.match(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$', v)}
    if not valid:
        return
    cmds = []
    for inf, m in valid.items():
        cmds += [
            f"ip link set {inf} down || true",
            f"ip link set {inf} address {m} || true",
            f"ip link set {inf} up || true",
        ]
    exec_str = " ; ".join(cmds)
    svc = (
        "[Unit]\nDescription=MagicBridge persistent MAC addresses\n"
        "Before=network.target dhcpcd.service NetworkManager.service\n\n"
        "[Service]\nType=oneshot\n"
        f'ExecStart=/bin/bash -c "{exec_str}"\n'
        "RemainAfterExit=yes\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    try:
        Path("/etc/systemd/system/mb-mac.service").write_text(svc)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "enable", "mb-mac"], capture_output=True)
    except Exception:
        pass

def _rand_mac() -> str:
    b = [0x00, 0x1a, 0x2b] + [secrets.randbits(8) for _ in range(3)]
    return ":".join(f"{x:02x}" for x in b)

def _tailscale_status() -> dict:
    try:
        r = subprocess.run(["tailscale","status","--json"],
                           capture_output=True, text=True, timeout=4)
        d = json.loads(r.stdout)
        return {
            "connected": d.get("BackendState") == "Running",
            "ip":   (d.get("TailscaleIPs") or [""])[0],
            "state": d.get("BackendState", "unknown"),
        }
    except Exception:
        return {"connected": False, "ip": "", "state": "not running"}

def _funnel_status() -> dict:
    try:
        r  = subprocess.run(["tailscale","funnel","status"],
                            capture_output=True, text=True, timeout=5)
        active = ":443" in r.stdout
        sr = subprocess.run(["tailscale","status","--json"],
                            capture_output=True, text=True, timeout=4)
        d  = json.loads(sr.stdout or "{}")
        dns = d.get("Self", {}).get("DNSName", "").rstrip(".")
        url = f"https://{dns}/" if (dns and active) else ""
        return {"active": active, "url": url, "hostname": dns}
    except Exception:
        return {"active": False, "url": "", "hostname": ""}

def _local_ip() -> str:
    try:
        return subprocess.run(["hostname","-I"],
                              capture_output=True, text=True).stdout.strip().split()[0]
    except Exception:
        return ""

def _cpu_temp():
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:
        return None

def _uptime() -> str:
    try:
        s = int(float(Path("/proc/uptime").read_text().split()[0]))
        d, r = divmod(s, 86400); h, r = divmod(r, 3600); m = r // 60
        return "".join([f"{d}d " if d else "", f"{h}h " if h else "", f"{m}m"])
    except Exception:
        return ""

def _kvm_last() -> dict:
    try:
        r = subprocess.run(["grep","-v","/stealth","/var/log/nginx/access.log"],
                           capture_output=True, text=True)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if not lines: return None
        last = lines[-1]
        ip = re.match(r"(\S+)", last)
        ts = re.search(r"\[([^\]]+)\]", last)
        return {"ip": ip.group(1) if ip else "?",
                "time": ts.group(1) if ts else "?"}
    except Exception:
        return None

def _tail_log(source: str, n: int = 50) -> str:
    path = LOG_SOURCES.get(source, AUTH_LOG)
    try:
        return subprocess.run(["tail", f"-{n}", path],
                              capture_output=True, text=True).stdout
    except Exception:
        return f"(could not read {path})"

def _log_sess(msg: str):
    try:
        with open(SESS_LOG, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass

# ── DuckDNS ───────────────────────────────────────────────────────────────────
def _ddns_update(host: str, token: str) -> bool:
    try:
        r = subprocess.run(
            ["curl","-s","--max-time","8",
             f"https://www.duckdns.org/update?domains={host}&token={token}&ip="],
            capture_output=True, text=True)
        return r.stdout.strip() == "OK"
    except Exception:
        return False

def _ext_ip() -> str:
    try:
        return subprocess.run(["curl","-s","--max-time","5","https://ipv4.icanhazip.com"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""

def _ddns_cron(host: str, token: str):
    try:
        Path("/etc/cron.d/mb-duckdns").write_text(
            f"*/5 * * * * root curl -s 'https://www.duckdns.org/update"
            f"?domains={host}&token={token}&ip=' >/var/log/mb-duckdns.log 2>&1\n"
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# HTML — Login
# ══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MagicBridge — Stealth</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%;background:#05060b;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#eef1f8}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem;position:relative;overflow:hidden}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 900px 620px at 10% -10%, rgba(139,92,246,.17), transparent 60%),
    radial-gradient(ellipse 760px 560px at 110% 15%, rgba(34,211,200,.13), transparent 60%),
    radial-gradient(ellipse 820px 640px at 50% 120%, rgba(34,211,238,.07), transparent 62%),
    linear-gradient(180deg,#05060b 0%,#080a14 55%,#05060b 100%);}
.card{position:relative;z-index:1;background:rgba(20,26,44,.62);backdrop-filter:blur(20px) saturate(140%);
      -webkit-backdrop-filter:blur(20px) saturate(140%);
      border:0.5px solid rgba(255,255,255,.1);border-radius:16px;
      padding:2.1rem 2rem;width:100%;max-width:320px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.brand svg{width:28px;height:28px;flex-shrink:0}
h1{font-size:16px;font-weight:700;letter-spacing:-.2px;
   background:linear-gradient(135deg,#8b5cf6 0%,#a78bfa 40%,#22d3c8 100%);
   -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:11.5px;color:#8b93a8;margin:4px 0 1.6rem}
label{display:block;font-size:11px;color:#8b93a8;margin-bottom:5px;font-weight:500}
input[type=password]{
  width:100%;padding:10px 12px;background:rgba(5,6,11,.7);
  border:0.5px solid rgba(255,255,255,.12);border-radius:9px;
  color:#eef1f8;font-size:13px;outline:none;transition:border .15s}
input[type=password]:focus{border-color:#8b5cf6;box-shadow:0 0 0 2px rgba(139,92,246,.15)}
button{
  margin-top:1rem;width:100%;padding:10px;
  background:linear-gradient(135deg,#8b5cf6 0%,#a78bfa 40%,#22d3c8 100%);
  border:none;border-radius:9px;color:#0c0a17;font-size:13px;font-weight:700;cursor:pointer;
  transition:filter .15s,transform .1s}
button:hover{filter:brightness(1.08)}
button:active{transform:scale(.98)}
button:focus{outline:2px solid #8b5cf6;outline-offset:3px}
.err{
  margin-top:.8rem;padding:9px 11px;
  background:rgba(244,63,94,.1);border:0.5px solid rgba(244,63,94,.3);
  border-radius:8px;font-size:12px;color:#fb7185}
.hint{margin-top:1rem;font-size:11px;color:#454f66;text-align:center}
</style>
</head>
<body>
<main>
<div class="card">
  <div class="brand">
    <svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="MagicBridge">
      <defs><linearGradient id="sg1" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#22d3c8"/>
      </linearGradient></defs>
      <rect x="4.5" y="17" width="6" height="18" rx="3" fill="url(#sg1)"/>
      <rect x="29.5" y="17" width="6" height="18" rx="3" fill="url(#sg1)"/>
      <rect x="4.5" y="17" width="31" height="5" rx="2.5" fill="url(#sg1)"/>
      <path d="M8 17 Q20 3 32 17" stroke="url(#sg1)" stroke-width="2.3" fill="none" stroke-linecap="round"/>
      <circle cx="20" cy="9.3" r="3" fill="url(#sg1)"/>
      <circle cx="20" cy="9.3" r="3" fill="none" stroke="#fff" stroke-opacity=".4" stroke-width=".7"/>
    </svg>
    <h1>MagicBridge</h1>
  </div>
  <p class="sub">Stealth configuration panel</p>
  {% if error %}
  <div class="err" role="alert" aria-live="assertive">{{ error }}</div>
  {% endif %}
  <form method="POST" action="/stealth/login" novalidate>
    <input type="hidden" name="_csrf" value="{{ csrf }}">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="pw"
           autocomplete="current-password" aria-required="true" autofocus>
    <button type="submit">Unlock</button>
  </form>
  <p class="hint">Forgot the password? Reset it from the Pi via SSH.</p>
</div>
</main>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
# HTML — Main dashboard
# ══════════════════════════════════════════════════════════════════════════════
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{{ csrf }}">
<title>MagicBridge — Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#05060b; --sf:#0b0e17; --sf2:#101526;
  --br:#1c2438; --br2:#2a3350;
  --t1:#eef1f8; --t2:#8b93a8; --t3:#454f66;
  --ac:#8b5cf6; --ac-bg:rgba(139,92,246,.12);
  --ok:#10b981; --ok-bg:rgba(16,185,129,.1);
  --wa:#f59e0b; --wa-bg:rgba(245,158,11,.1);
  --er:#f43f5e; --er-bg:rgba(244,63,94,.1);
}
html{font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--t1)}
.sk{position:absolute;top:-999px;left:0;padding:6px 12px;
    background:var(--ac);color:#fff;font-size:12px;z-index:9999;
    border-radius:0 0 6px 0;text-decoration:none}
.sk:focus{top:0}
header{
  display:flex;align-items:center;gap:10px;padding:10px 16px;
  background:var(--sf);border-bottom:0.5px solid var(--br);
  position:sticky;top:0;z-index:20}
.logo{font-size:14px;font-weight:600;letter-spacing:.3px}
.bdg{font-size:10px;padding:2px 8px;border-radius:20px;
     font-weight:500;border:0.5px solid}
.b-ok{background:var(--ok-bg);color:var(--ok);border-color:rgba(76,190,130,.3)}
.b-er{background:var(--er-bg);color:var(--er);border-color:rgba(224,80,80,.3)}
.sbar{
  display:flex;gap:16px;flex-wrap:wrap;padding:6px 16px;
  background:var(--sf2);border-bottom:0.5px solid var(--br);
  font-size:11px;color:var(--t3)}
main{padding:14px 16px;display:grid;gap:14px}
@media(min-width:680px){main{grid-template-columns:1fr 1fr}}
.full{grid-column:1/-1}
.card{background:var(--sf);border:0.5px solid var(--br);border-radius:10px;overflow:hidden}
.ch{padding:10px 14px;border-bottom:0.5px solid var(--br);
    display:flex;align-items:center;gap:8px}
.ch h2{font-size:13px;font-weight:500;flex:1;color:var(--t1)}
.ch .cd{font-size:11px;color:var(--t3)}
.cb{padding:12px 14px}
.field{margin-bottom:10px}
.field:last-child{margin-bottom:0}
.fl{display:block;font-size:11px;color:var(--t3);margin-bottom:3px}
.fd{display:block;font-size:11px;color:var(--t3);margin-top:3px;line-height:1.4;opacity:.8}
.frow{display:flex;gap:7px;align-items:flex-start;flex-wrap:wrap}
input[type=text],input[type=password],select,textarea{
  background:var(--bg);border:0.5px solid var(--br2);border-radius:6px;
  color:var(--t1);font-size:12px;padding:6px 9px;
  outline:none;transition:border .15s;font-family:inherit}
input:focus,select:focus,textarea:focus{
  border-color:var(--ac);box-shadow:0 0 0 2px rgba(74,158,255,.12)}
select{cursor:pointer}
textarea{resize:vertical;min-height:58px;font-family:monospace;
         font-size:10px;width:100%;line-height:1.5}
.btn{
  padding:5px 13px;border-radius:6px;font-size:12px;font-weight:500;
  cursor:pointer;border:0.5px solid var(--br2);
  background:var(--sf2);color:var(--t2);
  transition:background .15s,color .15s;font-family:inherit;line-height:1.4}
.btn:hover{background:var(--br2);color:var(--t1)}
.btn:focus{outline:2px solid var(--ac);outline-offset:2px}
.btn-p{background:var(--ac);border-color:transparent;color:#fff}
.btn-p:hover{opacity:.83;background:var(--ac)}
.btn-d{background:var(--er-bg);border-color:rgba(224,80,80,.3);color:var(--er)}
.btn-d:hover{background:rgba(224,80,80,.18)}
.pills{display:flex;flex-wrap:wrap;gap:5px;margin:5px 0 8px}
.pill{
  padding:3px 10px;border-radius:20px;font-size:11px;
  border:0.5px solid var(--br2);background:transparent;color:var(--t2);
  cursor:pointer;font-family:inherit;transition:all .15s}
.pill:hover{border-color:var(--ac);color:var(--ac)}
.pill.on{border-color:var(--ac);background:var(--ac-bg);color:var(--ac)}
.pill:focus{outline:2px solid var(--ac);outline-offset:2px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;
     vertical-align:middle;margin-right:4px}
.d-ok{background:var(--ok)} .d-er{background:var(--er)} .d-wa{background:var(--wa)}
.log{
  background:var(--bg);border:0.5px solid var(--br);border-radius:6px;
  padding:8px 10px;font-family:monospace;font-size:10px;color:var(--t3);
  height:130px;overflow-y:auto;white-space:pre-wrap;
  word-break:break-all;line-height:1.5;margin-top:4px}
hr{border:none;border-top:0.5px solid var(--br);margin:10px 0}
.ibar{height:2px;background:var(--br);position:sticky;bottom:0}
.ifill{height:100%;background:var(--ac);border-radius:1px;transition:width 1s linear}
#toast{
  position:fixed;bottom:14px;right:14px;
  background:var(--sf);border:0.5px solid var(--br2);border-radius:8px;
  padding:9px 15px;font-size:12px;opacity:0;transition:opacity .25s;
  pointer-events:none;z-index:999;max-width:260px}
#toast.show{opacity:1}
#toast.ok{border-left:3px solid var(--ok);color:var(--ok)}
#toast.er{border-left:3px solid var(--er);color:var(--er)}
</style>
</head>
<body>
<a href="#mc" class="sk">Skip to main content</a>

<header role="banner">
  <span class="logo">MagicBridge</span>
  <span class="bdg b-ok" id="ps" role="status" aria-live="polite">Active</span>
  <nav style="margin-left:auto" aria-label="Panel controls">
    <button class="btn" onclick="lock()" aria-label="Lock and log out">Lock</button>
  </nav>
</header>

<div class="sbar" role="status" aria-live="polite">
  <span id="s-temp">— °C</span>
  <span>Up: <span id="s-up">—</span></span>
  <span>IP: <span id="s-ip">—</span></span>
  <span id="s-ts">Tailscale: —</span>
</div>

<main id="mc" aria-label="Configuration">

<!-- ══ USB Identity ══════════════════════════════════════════════════════════ -->
<section class="card" aria-labelledby="h-usb">
  <div class="ch">
    <h2 id="h-usb">USB identity</h2>
    <span class="cd">How this device appears to the connected computer</span>
  </div>
  <div class="cb">
    <div class="field">
      <span class="fl" id="h-preset">Quick preset</span>
      <span class="fd">Pick a keyboard preset — updates all fields. Click Apply to send.</span>
      <div class="pills" role="group" aria-labelledby="h-preset" id="pills"></div>
      <button class="btn btn-p" onclick="applyPreset()" aria-label="Apply selected USB preset">Apply preset</button>
    </div>
    <hr>
    <div class="frow" style="margin-bottom:7px">
      <div class="field" style="flex:1;min-width:110px">
        <label class="fl" for="u-mfr">Manufacturer</label>
        <input type="text" id="u-mfr" style="width:100%">
      </div>
      <div class="field" style="flex:1;min-width:110px">
        <label class="fl" for="u-prod">Product name</label>
        <input type="text" id="u-prod" style="width:100%">
      </div>
    </div>
    <div class="frow" style="margin-bottom:7px">
      <div class="field" style="flex:1;min-width:70px">
        <label class="fl" for="u-vid">VID</label>
        <input type="text" id="u-vid" placeholder="0x046d" style="width:100%">
      </div>
      <div class="field" style="flex:1;min-width:70px">
        <label class="fl" for="u-pid">PID</label>
        <input type="text" id="u-pid" placeholder="0xc31c" style="width:100%">
      </div>
      <div class="field" style="flex:1;min-width:65px">
        <label class="fl" for="u-bcdusb">bcdUSB</label>
        <input type="text" id="u-bcdusb" value="0x0200" readonly aria-readonly="true"
               style="width:100%;opacity:.45;cursor:default">
      </div>
      <div class="field" style="flex:1;min-width:90px">
        <label class="fl" for="u-ser">Serial</label>
        <input type="text" id="u-ser" style="width:100%">
      </div>
    </div>
    <span class="fd" style="display:block;margin-bottom:8px">
      Computer sees a brief USB reconnect when identity is applied. Persists across reboots.
    </span>
    <div class="frow" style="flex-wrap:wrap;gap:6px">
      <button class="btn btn-p" onclick="applyId()" aria-label="Apply custom USB identity">Apply identity</button>
      <button class="btn" onclick="randSerial()" aria-label="Generate random serial">Random serial</button>
      <button class="btn" id="safe-btn" onclick="safeMode()" aria-label="Toggle safe mode">Safe mode</button>
    </div>
  </div>
</section>

<!-- ══ Network ═══════════════════════════════════════════════════════════════ -->
<section class="card" aria-labelledby="h-net">
  <div class="ch">
    <h2 id="h-net">Network identity</h2>
    <span class="cd">MAC address &amp; remote access</span>
  </div>
  <div class="cb">
    <div class="field">
      <span class="fl" id="h-mac">MAC address</span>
      <span class="fd">Changes the hardware address reported on the network.</span>
      <div class="frow" style="margin-top:6px">
        <select id="net-if" aria-labelledby="h-mac" style="width:80px">
          <option>eth0</option><option>wlan0</option>
        </select>
        <input type="text" id="net-mac" aria-label="MAC address" placeholder="00:1a:2b:xx:xx:xx" style="flex:1">
        <button class="btn" onclick="applyMac()" aria-label="Apply MAC">Apply</button>
        <button class="btn" onclick="randMac()" aria-label="Random MAC">Random</button>
      </div>
      <span class="fd">Applied immediately and persists across reboots via systemd.</span>
      <div id="mac-persist-st" style="font-size:11px;color:var(--t3);margin-top:4px" role="status" aria-live="polite"></div>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-ts">Tailscale — encrypted remote-access tunnel</span>
      <div id="ts-st" role="status" aria-live="polite" style="font-size:12px;color:var(--t3);margin:4px 0 6px">Loading…</div>
      <button class="btn" onclick="tsUp()" aria-label="Reconnect Tailscale">Reconnect</button>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-fn">Tailscale Funnel — public HTTPS access</span>
      <span class="fd">Exposes MagicBridge publicly. Requires Tailscale ≥ 1.34 and Funnel enabled in your tailnet.</span>
      <div id="fn-st" role="status" aria-live="polite" style="font-size:12px;color:var(--t3);margin:4px 0 6px">Loading…</div>
      <div class="frow" style="flex-wrap:wrap;gap:6px">
        <button class="btn btn-p" onclick="funnelOn()" aria-label="Enable Tailscale Funnel">Enable Funnel</button>
        <button class="btn btn-d" onclick="funnelOff()" aria-label="Disable Funnel">Disable</button>
      </div>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-ddns">DuckDNS — free public hostname</span>
      <span class="fd">Points a .duckdns.org domain at your external IP, updated every 5 minutes.</span>
      <div class="frow" style="margin-top:6px;flex-wrap:wrap">
        <input type="text" id="ddns-h" placeholder="myhostname" aria-label="DuckDNS hostname" style="flex:1;min-width:100px">
        <input type="text" id="ddns-t" placeholder="token"       aria-label="DuckDNS token"    style="flex:1;min-width:110px">
        <button class="btn btn-p" onclick="applyDdns()" aria-label="Save DuckDNS settings">Apply</button>
      </div>
      <div id="ddns-st" style="margin-top:4px;font-size:11px;color:var(--t3)" role="status" aria-live="polite"></div>
    </div>
  </div>
</section>

<!-- ══ WiFi ═══════════════════════════════════════════════════════════════ -->
<section class="card" aria-labelledby="h-wifi">
  <div class="ch">
    <h2 id="h-wifi">WiFi</h2>
    <span class="cd">Manage wireless connections</span>
  </div>
  <div class="cb">
    <div class="field">
      <span class="fl">Current status</span>
      <div id="wifi-st" style="font-size:12px;color:var(--t3);margin:4px 0 6px" role="status" aria-live="polite">Checking…</div>
    </div>
    <hr>
    <div class="field">
      <span class="fl">Add network</span>
      <div class="frow" style="margin-top:5px">
        <input type="text" id="wifi-ssid" placeholder="Network name" aria-label="WiFi SSID" style="flex:1">
        <input type="password" id="wifi-pass" placeholder="Password (blank=open)" aria-label="WiFi password" style="flex:1">
        <button class="btn btn-p" onclick="addWifi()" aria-label="Add WiFi network">Add</button>
      </div>
    </div>
    <div class="field">
      <button class="btn" onclick="loadSavedWifi()" aria-label="Refresh saved networks">Refresh saved networks</button>
      <div id="wifi-saved" style="margin-top:8px;font-size:12px;color:var(--t3)"></div>
    </div>
  </div>
</section>

<!-- ══ KVM Activity ══════════════════════════════════════════════════════════ -->
<section class="card" aria-labelledby="h-kvm">
  <div class="ch">
    <h2 id="h-kvm">KVM activity</h2>
    <span class="cd">Who last accessed the remote-control interface</span>
  </div>
  <div class="cb">
    <div id="kvm-last" style="font-size:12px;color:var(--t3);margin-bottom:10px" role="status" aria-live="polite">Checking…</div>
    <span class="fl">Session log (recent)</span>
    <div class="log" id="sess-log" role="log" aria-label="Session log" aria-live="polite"></div>
  </div>
</section>

<!-- ══ System ════════════════════════════════════════════════════════════════ -->
<section class="card" aria-labelledby="h-sys">
  <div class="ch">
    <h2 id="h-sys">System</h2>
    <span class="cd">Device health &amp; controls</span>
  </div>
  <div class="cb">
    <div id="sys-inf" style="font-size:12px;color:var(--t3);margin-bottom:10px" role="status" aria-live="polite">Loading…</div>
    <div class="frow" style="gap:7px;flex-wrap:wrap">
      <button class="btn btn-d" onclick="doReboot()" aria-label="Reboot the Pi">Reboot device</button>
      <button class="btn" onclick="chPw()" aria-label="Change panel password">Change password</button>
    </div>
    <div id="pw-form" style="display:none;margin-top:10px">
      <div class="frow" style="flex-wrap:wrap;gap:7px">
        <input type="password" id="pw-new" placeholder="New password" aria-label="New password" style="flex:1;min-width:120px">
        <input type="password" id="pw-confirm" placeholder="Confirm password" aria-label="Confirm password" style="flex:1;min-width:120px">
        <button class="btn btn-p" onclick="savePw()" aria-label="Save new password">Save</button>
        <button class="btn" onclick="document.getElementById('pw-form').style.display='none'" aria-label="Cancel">Cancel</button>
      </div>
      <div id="pw-st" style="margin-top:5px;font-size:11px;color:var(--t3)" role="status" aria-live="polite"></div>
    </div>
  </div>
</section>

<!-- ══ Config Backup ══════════════════════════════════════════════════════════ -->
<section class="card full" aria-labelledby="h-bk">
  <div class="ch">
    <h2 id="h-bk">Config backup &amp; restore</h2>
    <span class="cd">Save settings before reflashing — restore after reinstall</span>
  </div>
  <div class="cb">
    <span class="fd" style="display:block;margin-bottom:10px">
      Backup saves USB, MAC, DuckDNS, and video settings as JSON. Upload to restore after a fresh install.
    </span>
    <div class="frow" style="flex-wrap:wrap;gap:7px">
      <button class="btn" onclick="dlBackup()" aria-label="Download config backup">Download backup</button>
      <label class="btn" style="cursor:pointer">
        Upload &amp; restore
        <input type="file" accept=".json" style="display:none"
               aria-label="Select backup JSON to restore" onchange="ulRestore(this)">
      </label>
    </div>
  </div>
</section>

<!-- ══ Log Viewer ══════════════════════════════════════════════════════════════ -->
<section class="card full" aria-labelledby="h-log">
  <div class="ch">
    <h2 id="h-log">Logs</h2>
    <span class="cd">View system logs without SSH — last 50 lines</span>
  </div>
  <div class="cb">
    <div class="frow" style="margin-bottom:8px">
      <label for="log-src" class="fl" style="align-self:center;margin:0 4px 0 0">Source</label>
      <select id="log-src" aria-label="Log source">
        <option value="auth">Auth log</option>
        <option value="sessions">Session log</option>
        <option value="nginx">Nginx access</option>
        <option value="nginx-err">Nginx errors</option>
        <option value="system">System log</option>
        <option value="magicbridge">MagicBridge log</option>
      </select>
      <button class="btn" onclick="refreshLogs()" aria-label="Refresh logs">Refresh</button>
    </div>
    <div class="log" id="log-view" role="log" aria-live="polite" aria-label="Log output"></div>
  </div>
</section>

</main>

<div class="ibar" role="progressbar" aria-label="Session idle timer" aria-valuemin="0" aria-valuemax="100">
  <div class="ifill" id="ifill" style="width:100%"></div>
</div>
<div id="toast" role="alert" aria-live="assertive" aria-atomic="true"></div>

<script>
const CSRF  = document.querySelector('meta[name="csrf-token"]').content;
const PROFS = {{ profiles|tojson }};
let selP = 0;

async function api(url, body) {
  const o = {headers: {'X-CSRF-Token': CSRF}};
  if (body !== undefined) {
    o.method = 'POST';
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(body);
  }
  try { return (await fetch(url, o)).json(); }
  catch(e) { return {ok:false, error:String(e)}; }
}

function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = ''; }, 3500);
}

/* ── Idle timer ─────────────────────────────────────────────── */
const IDLE_MS = 28 * 60 * 1000;
let iLast = Date.now();
['mousemove','keydown','click','touchstart'].forEach(
  ev => document.addEventListener(ev, () => { iLast = Date.now(); }, {passive:true}));
setInterval(() => {
  const elapsed = Date.now() - iLast;
  const pct = Math.max(0, 100 - elapsed / IDLE_MS * 100);
  const f = document.getElementById('ifill');
  if (f) { f.style.width = pct + '%'; f.parentElement.setAttribute('aria-valuenow', Math.round(pct)); }
  if (elapsed > IDLE_MS + 120000) lock();
}, 1000);

async function lock() {
  await api('/stealth/api/lock', {});
  location.href = '/stealth/login';
}

/* ── USB profiles ───────────────────────────────────────────── */
function buildPills() {
  const c = document.getElementById('pills');
  c.innerHTML = '';
  PROFS.forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'pill' + (i === selP ? ' on' : '');
    b.setAttribute('aria-pressed', i === selP ? 'true' : 'false');
    b.textContent = p.name;
    b.onclick = () => {
      selP = i; buildPills();
      document.getElementById('u-mfr').value  = p.mfr;
      document.getElementById('u-prod').value = p.prod;
      document.getElementById('u-vid').value  = p.vid;
      document.getElementById('u-pid').value  = p.pid;
    };
    c.appendChild(b);
  });
}

async function applyPreset() {
  const r = await api('/stealth/api/apply', {action:'profile', idx:selP});
  toast(r.ok ? 'Preset applied: '+PROFS[selP].name : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadStatus();
}

async function applyId() {
  const r = await api('/stealth/api/apply', {
    action:'identity',
    mfr: document.getElementById('u-mfr').value,
    prod:document.getElementById('u-prod').value,
    ser: document.getElementById('u-ser').value,
    vid: document.getElementById('u-vid').value,
    pid: document.getElementById('u-pid').value,
  });
  toast(r.ok ? 'Identity applied' : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadStatus();
}

async function randSerial() {
  const r = await api('/stealth/api/randomize');
  if (r.serial) { document.getElementById('u-ser').value = r.serial; toast('Serial: '+r.serial); }
}

async function safeMode() {
  const r = await api('/stealth/api/apply', {action:'safe_mode'});
  toast(r.ok ? (r.safe ? 'Safe mode ON' : 'Safe mode OFF') : 'Error', r.ok?'ok':'er');
  const b = document.getElementById('safe-btn');
  if (b) b.textContent = r.safe ? 'Exit safe mode' : 'Safe mode';
}

/* ── MAC ────────────────────────────────────────────────────── */
async function applyMac() {
  const r = await api('/stealth/api/apply', {
    action:'mac',
    iface: document.getElementById('net-if').value,
    mac:   document.getElementById('net-mac').value,
  });
  toast(r.ok ? 'MAC applied' : (r.error||'Error'), r.ok?'ok':'er');
}

async function randMac() {
  const iface = document.getElementById('net-if').value;
  const r = await api('/stealth/api/apply', {action:'rand_mac', iface});
  if (r.mac) { document.getElementById('net-mac').value = r.mac; toast('MAC: '+r.mac); }
}

/* ── Tailscale ──────────────────────────────────────────────── */
async function loadTs() {
  const r = await api('/stealth/api/tailscale');
  const el = document.getElementById('ts-st');
  const sb = document.getElementById('s-ts');
  if (r.connected) {
    el.innerHTML = '<span class="dot d-ok"></span>Connected · ' + r.ip;
    if (sb) sb.textContent = 'Tailscale: ' + r.ip;
  } else {
    el.innerHTML = '<span class="dot d-er"></span>' + (r.state||'disconnected');
    if (sb) sb.textContent = 'Tailscale: off';
  }
}

async function tsUp() {
  await api('/stealth/api/apply', {action:'ts_up'});
  toast('Reconnecting…');
  setTimeout(loadTs, 4000);
}

async function loadFunnel() {
  const r = await api('/stealth/api/funnel');
  const el = document.getElementById('fn-st');
  if (!el) return;
  if (r.active && r.url)
    el.innerHTML = '<span class="dot d-ok"></span>Active — <a href="'+r.url+'" target="_blank" style="color:var(--ac)">'+r.url+'</a>';
  else if (r.active)
    el.innerHTML = '<span class="dot d-ok"></span>Active (fetching URL…)';
  else
    el.innerHTML = '<span class="dot d-er"></span>Off';
}

async function funnelOn() {
  const r = await api('/stealth/api/apply', {action:'ts_funnel_on'});
  toast(r.ok ? 'Funnel enabling…' : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) setTimeout(loadFunnel, 5000);
}

async function funnelOff() {
  const r = await api('/stealth/api/apply', {action:'ts_funnel_off'});
  toast(r.ok ? 'Funnel disabled' : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) setTimeout(loadFunnel, 2000);
}

/* ── DuckDNS ────────────────────────────────────────────────── */
async function applyDdns() {
  const r = await api('/stealth/api/apply', {
    action:'duckdns',
    host:  document.getElementById('ddns-h').value,
    token: document.getElementById('ddns-t').value,
  });
  const el = document.getElementById('ddns-st');
  el.textContent = r.ok ? 'Updated — IP: '+(r.ip||'?') : (r.error||'Failed');
  el.style.color  = r.ok ? 'var(--ok)' : 'var(--er)';
  toast(r.ok ? 'DuckDNS updated' : 'DuckDNS failed', r.ok?'ok':'er');
}

/* ── Status / Stats ─────────────────────────────────────────── */
async function loadStatus() {
  const r = await api('/stealth/api/status');
  document.getElementById('u-mfr').value    = r.mfr       || '';
  document.getElementById('u-prod').value   = r.prod      || '';
  document.getElementById('u-vid').value    = r.vid       || '';
  document.getElementById('u-pid').value    = r.pid       || '';
  document.getElementById('u-ser').value    = r.ser       || '';
  document.getElementById('u-bcdusb').value = r.bcdUSB    || '0x0200';
  document.getElementById('net-mac').value  = r.mac       || '';
  document.getElementById('ddns-h').value   = r.ddns_host || '';
  const mp = r.mac_persist || {};
  const mps = document.getElementById('mac-persist-st');
  if (mps) {
    const entries = Object.entries(mp).filter(([,v]) => v);
    mps.innerHTML = entries.length
      ? '<span class="dot d-ok"></span>Boot persist: '+entries.map(([i,m])=>i+'→'+m).join(', ')
      : '';
  }
}

async function loadStats() {
  const r = await api('/stealth/api/stats');
  const t = r.temp ? r.temp+' °C' : '—';
  document.getElementById('s-temp').textContent = t;
  document.getElementById('s-up').textContent   = r.uptime || '—';
  document.getElementById('s-ip').textContent   = r.ip     || '—';
  document.getElementById('sys-inf').innerHTML  =
    'CPU: '+t+' &nbsp;·&nbsp; Up: '+(r.uptime||'—')+' &nbsp;·&nbsp; IP: '+(r.ip||'—');
  const kl = document.getElementById('kvm-last');
  kl.innerHTML = r.kvm
    ? '<span class="dot d-ok"></span>Last KVM: '+r.kvm.time+' from '+r.kvm.ip
    : 'No KVM connections logged yet.';
  const sl = document.getElementById('sess-log');
  if (sl) sl.textContent = (r.sess_log||[]).join('\n');
}

/* ── WiFi ───────────────────────────────────────────────────── */
async function loadWifiStatus() {
  const r = await api('/stealth/api/wifi/status');
  const el = document.getElementById('wifi-st');
  if (r.connected)
    el.innerHTML = '<span class="dot d-ok"></span>'+r.ssid+' · '+r.ip+(r.signal?' · '+r.signal+'%':'');
  else
    el.innerHTML = '<span class="dot d-er"></span>Not connected';
}

async function loadSavedWifi() {
  const r = await api('/stealth/api/wifi/saved');
  const el = document.getElementById('wifi-saved');
  if (!r || !r.length) { el.textContent = 'No saved networks.'; return; }
  el.innerHTML = r.map(n =>
    '<div data-net="'+esc(n.name)+'" style="display:flex;align-items:center;gap:8px;margin-bottom:5px">'
    + '<span style="flex:1;color:var(--t2)">'+(n.active?'● ':'')+n.name+'</span>'
    + '<span class="psk-out" style="font-size:10px;color:var(--t3);font-family:monospace"></span>'
    + '<button class="btn" style="font-size:10px;padding:2px 7px" onclick="revealPsk(this)">Show</button>'
    + (n.active ? '' : '<button class="btn" style="font-size:10px;padding:2px 7px" onclick="connectWifi(\''+esc(n.name)+'\')">Connect</button>')
    + '<button class="btn btn-d" style="font-size:10px;padding:2px 7px" onclick="removeWifi(\''+esc(n.name)+'\')">Remove</button>'
    + '</div>'
  ).join('');
}

async function revealPsk(btn) {
  const row = btn.closest('[data-net]');
  const name = row.dataset.net;
  const out = row.querySelector('.psk-out');
  const r = await api('/stealth/api/wifi/psk-auth?name='+encodeURIComponent(name));
  out.textContent = r.ok ? (r.psk || '(open network)') : (r.error || 'Error');
}

async function addWifi() {
  const ssid = document.getElementById('wifi-ssid').value.trim();
  const pass = document.getElementById('wifi-pass').value;
  if (!ssid) { toast('SSID required', 'er'); return; }
  const r = await api('/stealth/api/wifi/add', {ssid, password: pass});
  toast(r.ok ? 'Network saved: '+ssid : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) { document.getElementById('wifi-ssid').value=''; document.getElementById('wifi-pass').value=''; loadSavedWifi(); }
}

async function removeWifi(name) {
  if (!confirm('Remove "'+name+'"?')) return;
  const r = await api('/stealth/api/wifi/remove', {name});
  toast(r.ok ? 'Removed: '+name : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadSavedWifi();
}

async function connectWifi(name) {
  toast('Connecting to '+name+'…', 'ok');
  const r = await api('/stealth/api/wifi/connect', {name});
  toast(r.ok ? 'Connected: '+name : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) { setTimeout(loadWifiStatus, 3000); loadSavedWifi(); }
}

function esc(s) { return String(s).replace(/'/g,"\\'"); }

/* ── Logs ───────────────────────────────────────────────────── */
async function refreshLogs() {
  const src = document.getElementById('log-src').value;
  const r   = await api('/stealth/api/logs?source='+src);
  document.getElementById('log-view').textContent = r.content || '(empty)';
}

/* ── Backup ─────────────────────────────────────────────────── */
function dlBackup() { location.href = '/stealth/api/backup'; }

async function ulRestore(input) {
  const f = input.files[0];
  if (!f) return;
  let d;
  try { d = JSON.parse(await f.text()); } catch { toast('Invalid JSON', 'er'); return; }
  const r = await api('/stealth/api/restore', d);
  toast(r.ok ? 'Restored — reload page' : (r.error||'Error'), r.ok?'ok':'er');
}

/* ── Reboot ─────────────────────────────────────────────────── */
async function doReboot() {
  if (!confirm('Reboot? Active KVM session will be interrupted.')) return;
  await api('/stealth/api/apply-reboot', {});
  toast('Rebooting…');
}

/* ── Change password ────────────────────────────────────────── */
function chPw() {
  document.getElementById('pw-form').style.display = '';
}

async function savePw() {
  const nw = document.getElementById('pw-new').value;
  const cf = document.getElementById('pw-confirm').value;
  const st = document.getElementById('pw-st');
  if (!nw) { st.textContent = 'Password cannot be empty'; return; }
  if (nw !== cf) { st.textContent = 'Passwords do not match'; return; }
  const r = await api('/stealth/api/change-password', {password: nw});
  st.textContent = r.ok ? 'Password changed!' : (r.error||'Error');
  st.style.color = r.ok ? 'var(--ok)' : 'var(--er)';
  if (r.ok) {
    document.getElementById('pw-new').value = '';
    document.getElementById('pw-confirm').value = '';
    setTimeout(() => { document.getElementById('pw-form').style.display='none'; }, 2000);
  }
}

/* ── Init ───────────────────────────────────────────────────── */
buildPills();
loadStatus();
loadStats();
loadTs();
loadFunnel();
loadWifiStatus();
loadSavedWifi();
refreshLogs();
setInterval(loadStats,    30000);
setInterval(loadTs,       20000);
setInterval(loadFunnel,   60000);
setInterval(loadWifiStatus, 30000);
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = _load()
    _ensure_defaults(cfg)
    if request.method == "POST":
        ip = _client_ip()
        if request.form.get("_csrf") != session.get("login_csrf"):
            return render_template_string(LOGIN_HTML, error="Invalid request.", csrf=_fresh_login_csrf()), 400
        _apply_delay(ip)
        pw     = request.form.get("pw", "")
        stored = cfg.get("auth", {}).get("password_hash", "")
        if _check_pw(pw, stored):
            _record_ok(ip)
            session.clear()
            session["ok"]   = True
            session["t"]    = time.time()
            session["csrf"] = secrets.token_hex(32)
            _al.info(f"Login OK from {ip}")
            _log_sess(f"Login from {ip}")
            return redirect(_stealth())
        _record_fail(ip)
        n = _login_fails.get(ip, 0)
        _al.info(f"Failed login from {ip} (attempt {n})")
        return render_template_string(LOGIN_HTML, error="Incorrect password.", csrf=_fresh_login_csrf()), 401
    return render_template_string(LOGIN_HTML, error=None, csrf=_fresh_login_csrf())


@app.route("/")
def index():
    if not _authed(): return redirect(_stealth("login"))
    profiles = [{"name":p["name"],"mfr":p["mfr"],"prod":p["prod"],
                 "vid":p["vid"],"pid":p["pid"]} for p in USB_PROFILES]
    return render_template_string(MAIN_HTML, csrf=session.get("csrf",""), profiles=profiles)


@app.route("/api/status")
def api_status():
    if not _authed(): return jsonify({"error":"auth"}), 401
    cfg = _load()
    return jsonify({
        "mfr":         _usb_r("strings/0x409/manufacturer"),
        "prod":        _usb_r("strings/0x409/product"),
        "ser":         _usb_r("strings/0x409/serialnumber"),
        "vid":         _usb_r("idVendor"),
        "pid":         _usb_r("idProduct"),
        "bcdUSB":      _usb_r("bcdUSB"),
        "mac":         _cur_mac("eth0"),
        "ddns_host":   cfg.get("duckdns", {}).get("host", ""),
        "mac_persist": cfg.get("mac_persist", {}),
    })


@app.route("/api/stats")
def api_stats():
    if not _authed(): return jsonify({"error":"auth"}), 401
    try:
        sl = Path(SESS_LOG).read_text().splitlines()[-20:][::-1]
    except Exception:
        sl = []
    return jsonify({
        "temp":     _cpu_temp(),
        "uptime":   _uptime(),
        "ip":       _local_ip(),
        "kvm":      _kvm_last(),
        "sess_log": sl,
    })


@app.route("/api/tailscale")
def api_tailscale():
    if not _authed(): return jsonify({"error":"auth"}), 401
    return jsonify(_tailscale_status())


@app.route("/api/funnel")
def api_funnel():
    if not _authed(): return jsonify({"error":"auth"}), 401
    return jsonify(_funnel_status())


@app.route("/api/logs")
def api_logs():
    if not _authed(): return jsonify({"error":"auth"}), 401
    src = request.args.get("source", "auth")
    return jsonify({"content": _tail_log(src)})


@app.route("/api/backup")
def api_backup():
    if not _authed(): return redirect(_stealth("login"))
    cfg  = _load()
    safe = {k: v for k, v in cfg.items() if k != "auth"}
    return Response(
        json.dumps(safe, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=magicbridge-config.json"},
    )


@app.route("/api/restore", methods=["POST"])
def api_restore():
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
    d = request.get_json(force=True, silent=True) or {}
    if not isinstance(d, dict):
        return jsonify({"error": "Invalid format"}), 400
    cfg = _load()
    d["auth"] = cfg.get("auth", {})
    _save(d)
    _log_sess(f"Config restored from {_client_ip()}")
    return jsonify({"ok": True})


@app.route("/api/lock", methods=["POST"])
def api_lock():
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
    _log_sess(f"Panel locked by {_client_ip()}")
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/randomize")
def api_randomize():
    if not _authed(): return jsonify({"error":"auth"}), 401
    ser = _rand_serial("MB")
    _usb_w("strings/0x409/serialnumber", ser)
    return jsonify({"ok": True, "serial": ser})


@app.route("/api/change-password", methods=["POST"])
def api_change_password():
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
    d  = request.get_json(force=True, silent=True) or {}
    pw = d.get("password", "").strip()
    if not pw or len(pw) < 4:
        return jsonify({"ok": False, "error": "Password must be at least 4 characters"}), 400
    cfg = _load()
    cfg.setdefault("auth", {})["password_hash"] = _hash_pw(pw)
    _save(cfg)
    _log_sess(f"Password changed by {_client_ip()}")
    return jsonify({"ok": True})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok():  return jsonify({"error":"csrf"}), 403
    d   = request.get_json(force=True, silent=True) or {}
    act = d.get("action", "")
    cfg = _load()
    try:
        if act == "identity":
            _apply_usb(d.get("mfr",""), d.get("prod",""), d.get("ser",""),
                       d.get("vid"), d.get("pid"))
            _log_sess(f"USB identity: {d.get('mfr')} / {d.get('prod')}")
            return jsonify({"ok": True})

        elif act == "profile":
            idx = int(d.get("idx", 0))
            if not 0 <= idx < len(USB_PROFILES):
                return jsonify({"error": "Bad index"}), 400
            p   = USB_PROFILES[idx]
            ser = _rand_serial(p["pfx"])
            _apply_usb(p["mfr"], p["prod"], ser, p["vid"], p["pid"])
            cfg["usb"] = {"profile_idx": idx}
            _save(cfg)
            _log_sess(f"USB profile: {p['name']}")
            return jsonify({"ok": True})

        elif act == "mac":
            iface, mac = d.get("iface","eth0"), d.get("mac","")
            if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
                return jsonify({"error": "Invalid MAC format"}), 400
            _set_mac(iface, mac)
            _persist_mac(iface, mac)
            _log_sess(f"MAC {iface}: {mac}")
            return jsonify({"ok": True})

        elif act == "rand_mac":
            iface = d.get("iface", "eth0")
            mac   = _rand_mac()
            _set_mac(iface, mac)
            _log_sess(f"MAC randomized {iface}: {mac}")
            return jsonify({"ok": True, "mac": mac})

        elif act == "safe_mode":
            in_safe = cfg.get("safe_mode", False)
            if not in_safe:
                _apply_usb(ORIG["manufacturer"], ORIG["product"], ORIG["serial"],
                           ORIG["idVendor"], ORIG["idProduct"])
                cfg["safe_mode"] = True
            else:
                idx = cfg.get("usb", {}).get("profile_idx", 0)
                if 0 <= idx < len(USB_PROFILES):
                    p = USB_PROFILES[idx]
                    _apply_usb(p["mfr"], p["prod"], _rand_serial(p["pfx"]),
                               p["vid"], p["pid"])
                cfg["safe_mode"] = False
            _save(cfg)
            _log_sess(f"Safe mode: {cfg['safe_mode']}")
            return jsonify({"ok": True, "safe": cfg["safe_mode"]})

        elif act == "duckdns":
            host  = d.get("host","").strip()
            token = d.get("token","").strip()
            if not host or not token:
                return jsonify({"error": "Hostname and token required"}), 400
            if _ddns_update(host, token):
                cfg["duckdns"] = {"host": host, "token": token}
                _save(cfg)
                _ddns_cron(host, token)
                ip = _ext_ip()
                _log_sess(f"DuckDNS: {host}.duckdns.org → {ip}")
                return jsonify({"ok": True, "ip": ip})
            return jsonify({"ok": False, "error": "DuckDNS update failed — check hostname and token"})

        elif act == "ts_up":
            subprocess.Popen(["tailscale","up","--accept-routes"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log_sess("Tailscale reconnect triggered")
            return jsonify({"ok": True})

        elif act == "ts_funnel_on":
            subprocess.Popen(["tailscale","funnel","443"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log_sess("Tailscale Funnel enabled :443")
            return jsonify({"ok": True})

        elif act == "ts_funnel_off":
            subprocess.Popen(["tailscale","funnel","--remove"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log_sess("Tailscale Funnel disabled")
            return jsonify({"ok": True})

        else:
            return jsonify({"error": f"Unknown action: {act}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apply-reboot", methods=["POST"])
def api_reboot():
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
    _log_sess(f"Reboot by {_client_ip()}")
    subprocess.Popen(["shutdown","-r","now"])
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# WiFi API (no auth — accessible from main KVM page via nginx /api/wifi/ proxy)
# ══════════════════════════════════════════════════════════════════════════════

def _nm(*args, timeout=15):
    return subprocess.run(["nmcli"] + list(args),
                          capture_output=True, text=True, timeout=timeout)


@app.route("/api/wifi/status")
def api_wifi_status():
    try:
        r = _nm("-t","-f","GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS",
                "device","show","wlan0")
        info: dict = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
        ssid  = info.get("GENERAL.CONNECTION","")
        state = info.get("GENERAL.STATE","")
        ip    = info.get("IP4.ADDRESS[1]","").split("/")[0]
        conn  = "connected" in state.lower() and ssid not in ("","--")
        sig = 0
        try:
            sr = _nm("-t","-f","SIGNAL,SSID","device","wifi","list")
            for sl in sr.stdout.splitlines():
                parts = sl.split(":")
                if len(parts) >= 2 and parts[1].strip() == ssid:
                    sig = int(parts[0]) if parts[0].isdigit() else 0
                    break
        except Exception:
            pass
        return jsonify({"ssid":"" if ssid=="--" else ssid,"connected":conn,"ip":ip,"signal":sig})
    except Exception as e:
        return jsonify({"ssid":"","connected":False,"ip":"","signal":0,"error":str(e)})


@app.route("/api/wifi/saved")
def api_wifi_saved():
    try:
        r = _nm("-t","-f","NAME,TYPE,ACTIVE","connection","show")
        nets = []
        for line in r.stdout.splitlines():
            parts = line.rsplit(":",2)
            if len(parts) >= 2 and parts[-2] == "802-11-wireless":
                nets.append({"name":parts[0],"active":(parts[-1].lower()=="yes") if len(parts)>2 else False})
        return jsonify(nets)
    except Exception:
        return jsonify([])


@app.route("/api/wifi/scan")
def api_wifi_scan():
    try:
        r = _nm("-t","-f","SSID,SIGNAL,SECURITY","device","wifi","list","--rescan","yes",timeout=22)
        nets, seen = [], set()
        for line in r.stdout.splitlines():
            parts = line.rsplit(":",2)
            if len(parts) < 3: continue
            ssid = parts[0].strip()
            if not ssid or ssid=="--" or ssid in seen: continue
            seen.add(ssid)
            sig = int(parts[1]) if parts[1].isdigit() else 0
            sec = parts[2].strip()
            nets.append({"ssid":ssid,"signal":sig,"secure":bool(sec and sec!="--")})
        return jsonify(sorted(nets, key=lambda x:-x["signal"]))
    except Exception:
        return jsonify([])


@app.route("/api/wifi/add", methods=["POST"])
def api_wifi_add():
    d    = request.get_json(force=True, silent=True) or {}
    ssid = (d.get("ssid") or "").strip()
    pwd  = (d.get("password") or "").strip()
    prio = int(d.get("priority", 100))
    if not ssid: return jsonify({"ok":False,"error":"SSID required"}), 400
    if not re.match(r"^[ -~]{1,32}$", ssid): return jsonify({"ok":False,"error":"Invalid SSID"}), 400
    _nm("connection","delete",ssid,timeout=5)
    cmd = ["connection","add","type","wifi","ifname","wlan0",
           "con-name",ssid,"ssid",ssid,"connection.autoconnect","yes",
           "connection.autoconnect-priority",str(prio)]
    if pwd: cmd += ["wifi-sec.key-mgmt","wpa-psk","wifi-sec.psk",pwd]
    r = _nm(*cmd,timeout=12)
    if r.returncode == 0:
        _log_sess(f"WiFi saved: {ssid}")
        return jsonify({"ok":True})
    return jsonify({"ok":False,"error":r.stderr.strip() or r.stdout.strip()})


@app.route("/api/wifi/remove", methods=["POST"])
def api_wifi_remove():
    d    = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok":False,"error":"name required"}), 400
    r = _nm("connection","delete",name,timeout=10)
    if r.returncode == 0:
        _log_sess(f"WiFi removed: {name}")
        return jsonify({"ok":True})
    return jsonify({"ok":False,"error":r.stderr.strip()})


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    d    = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok":False,"error":"name required"}), 400
    r = _nm("connection","up",name,timeout=25)
    if r.returncode == 0:
        _log_sess(f"WiFi connect: {name}")
        return jsonify({"ok":True})
    err = (r.stderr or r.stdout).strip()
    return jsonify({"ok":False,"error":err[:120]})


# ══════════════════════════════════════════════════════════════════════════════
# Stealth-panel-proxied WiFi routes (called from stealth panel JS)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/wifi/status-auth")
def api_wifi_status_auth():
    if not _authed(): return jsonify({"error":"auth"}), 401
    return api_wifi_status()


@app.route("/api/wifi/psk-auth")
def api_wifi_psk_auth():
    """Authenticated saved-WiFi-password reveal (stealth panel only)."""
    if not _authed(): return jsonify({"error":"auth"}), 401
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "psk": ""})
    r = _nm("-s", "-t", "-f", "802-11-wireless-security.psk",
            "connection", "show", name, timeout=8)
    psk = r.stdout.strip().split(":")[-1] if r.returncode == 0 else ""
    return jsonify({"ok": True, "psk": psk})


# ══════════════════════════════════════════════════════════════════════════════
# Boot
# ══════════════════════════════════════════════════════════════════════════════

_boot()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7777, debug=False, use_reloader=False)
