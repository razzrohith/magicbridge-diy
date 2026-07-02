#!/usr/bin/env python3
"""
MagicBridge Main KVM Server

aiohttp HTTP + WebSocket server on 127.0.0.1:8080
nginx proxies all external traffic here.

Routes:
  GET  /          → KVM web UI (index.html)
  GET  /ws        → WebSocket (keyboard/mouse input)
  GET  /api/status           → system status JSON
  GET  /api/devices          → list V4L2 capture devices
  GET  /api/stream/settings  → current stream settings
  POST /api/stream/settings  → update stream settings (quality, resolution, fps)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path

from aiohttp import web, WSMsgType
import aiohttp

try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

# Local modules (same directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hid   import HIDKeyboard, HIDMouse
from video import VideoManager

# Config
CONFIG_PATH = "/etc/magicbridge/config.json"
WEB_ROOT    = "/opt/magicbridge/web"
HOST        = "127.0.0.1"
PORT        = 8080
VERSION     = "1.0.0"

# Session auth is independent from /stealth/: separate password, separate
# secret, separate cookie/session. Logging into one doesn't unlock the
# other, so a leaked main-page password can't reach the admin panel.
SESSION_COOKIE  = "mb_sess"
SESSION_TIMEOUT = 1800  # 30 min idle, matches stealth-dashboard.py
DEFAULT_PASSWORD = "magicbridge"

# Logging
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(name)-24s %(levelname)s  %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("magicbridge")


# Auth helpers
def _auth_cfg() -> dict:
    try:
        return json.loads(Path(CONFIG_PATH).read_text()).get("auth", {})
    except Exception:
        return {}

def _check_pw(pw: str, stored: str) -> bool:
    if not stored:
        return False
    if _HAS_BCRYPT and stored.startswith("$2"):
        try:
            return _bcrypt.checkpw(pw.encode(), stored.encode())
        except Exception:
            return False
    raw = stored[len("sha256:"):] if stored.startswith("sha256:") else stored
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), raw)

def _make_token(secret: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return ts + "." + sig

def _check_token(token: str, secret: str) -> bool:
    try:
        ts, sig = token.split(".", 1)
        expected = hmac.new(secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return (time.time() - int(ts)) <= SESSION_TIMEOUT
    except Exception:
        return False

def _is_authed(request: web.Request) -> bool:
    secret = _auth_cfg().get("main_secret_key", "")
    if not secret:
        return False
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token) and _check_token(token, secret)

def _ensure_auth_defaults():
    """Bootstrap auth.main_password_hash/main_secret_key if config.json
    doesn't have them yet (e.g. first boot). These are namespaced under
    "main_*" specifically so they never collide with /stealth/'s own
    auth.password_hash/secret_key. The two panels have fully independent
    credentials by design."""
    import secrets as _secrets
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    except Exception:
        cfg = {}
    auth = cfg.setdefault("auth", {})
    changed = False
    if not auth.get("main_password_hash"):
        if _HAS_BCRYPT:
            auth["main_password_hash"] = _bcrypt.hashpw(DEFAULT_PASSWORD.encode(), _bcrypt.gensalt()).decode()
        else:
            auth["main_password_hash"] = "sha256:" + hashlib.sha256(DEFAULT_PASSWORD.encode()).hexdigest()
        changed = True
    if not auth.get("main_secret_key"):
        auth["main_secret_key"] = _secrets.token_hex(32)
        changed = True
    if changed:
        try:
            Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
            log.info("Bootstrapped main-page auth defaults in %s", CONFIG_PATH)
        except Exception as e:
            log.warning("Could not write auth defaults: %s", e)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MagicBridge</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%;background:#05070d;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#f3ecdd}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem;position:relative;overflow:hidden}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 900px 620px at 10% -10%, rgba(240,214,152,.12), transparent 60%),
    radial-gradient(ellipse 760px 560px at 110% 15%, rgba(201,161,92,.14), transparent 60%),
    radial-gradient(ellipse 820px 640px at 50% 120%, rgba(138,106,47,.09), transparent 62%),
    linear-gradient(180deg,#05070d 0%,#080b16 55%,#05070d 100%);}
.card{position:relative;z-index:1;background:rgba(19,26,44,.62);backdrop-filter:blur(20px) saturate(140%);
      -webkit-backdrop-filter:blur(20px) saturate(140%);
      border:0.5px solid rgba(240,214,152,.12);border-radius:16px;
      padding:2.1rem 2rem;width:100%;max-width:320px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.brand svg{width:30px;height:30px;flex-shrink:0}
h1{font-size:17px;font-weight:700;letter-spacing:-.3px;
   background:linear-gradient(135deg,#f0d698 0%,#c9a15c 55%,#8a6a2f 100%);
   -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:11.5px;color:#9a9280;margin:4px 0 1.6rem}
label{display:block;font-size:11px;color:#9a9280;margin-bottom:5px;font-weight:500}
input[type=password]{width:100%;padding:10px 12px;background:rgba(5,7,13,.7);
  border:0.5px solid rgba(240,214,152,.16);border-radius:9px;color:#f3ecdd;font-size:13px;outline:none;
  transition:border-color .15s}
input[type=password]:focus{border-color:#c9a15c}
button{margin-top:1rem;width:100%;padding:10px;
  background:linear-gradient(135deg,#f0d698 0%,#c9a15c 55%,#8a6a2f 100%);
  border:none;border-radius:9px;color:#1a1408;font-size:13px;font-weight:700;cursor:pointer;
  transition:filter .15s,transform .1s}
button:hover{filter:brightness(1.08)}
button:active{transform:scale(.98)}
.err{margin-top:.8rem;padding:9px 11px;background:rgba(244,63,94,.1);
  border:0.5px solid rgba(244,63,94,.3);border-radius:8px;font-size:12px;color:#fb7185}
</style></head><body><main><div class="card">
<div class="brand">
  <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="MagicBridge">
    <defs><linearGradient id="lg1" x1="0" y1="0" x2="100" y2="100" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#f0d698"/><stop offset="100%" stop-color="#8a6a2f"/>
    </linearGradient></defs>
    <path d="M15 40 C15 25 30 20 50 20 C70 20 85 25 85 40 C85 55 72 58 50 58 C28 58 15 55 15 40 Z" fill="none" stroke="url(#lg1)" stroke-width="5"/>
    <path d="M50 20 L50 58" stroke="url(#lg1)" stroke-width="3.4" opacity=".4"/>
    <circle cx="32" cy="40" r="3.4" fill="url(#lg1)"/>
    <circle cx="68" cy="40" r="3.4" fill="url(#lg1)"/>
    <path d="M22 66 Q50 78 78 66" stroke="url(#lg1)" stroke-width="4.2" fill="none" opacity=".5"/>
    <path d="M28 74 Q50 84 72 74" stroke="url(#lg1)" stroke-width="3.4" fill="none" opacity=".3"/>
  </svg>
  <h1>MagicBridge</h1>
</div>
<p class="sub">Sign in to control this device</p>
__ERROR__
<form method="POST" action="/login">
<label for="pw">Password</label>
<input type="password" id="pw" name="pw" autocomplete="current-password" autofocus>
<button type="submit">Unlock</button>
</form></div></main></body></html>"""


async def login_handler(request: web.Request) -> web.Response:
    if request.method == "POST":
        try:
            data = await request.post()
            pw = str(data.get("pw", ""))
        except Exception:
            pw = ""
        auth = _auth_cfg()
        if pw and _check_pw(pw, auth.get("main_password_hash", "")):
            secret = auth.get("main_secret_key", "")
            resp = web.HTTPFound("/")
            if secret:
                resp.set_cookie(SESSION_COOKIE, _make_token(secret),
                                 max_age=SESSION_TIMEOUT, httponly=True,
                                 secure=True, samesite="Lax", path="/")
            return resp
        html = LOGIN_HTML.replace("__ERROR__", '<div class="err">Incorrect password.</div>')
        return web.Response(text=html, content_type="text/html", status=401)
    html = LOGIN_HTML.replace("__ERROR__", "")
    return web.Response(text=html, content_type="text/html")


async def logout_handler(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def api_change_password(request: web.Request) -> web.Response:
    """POST /api/auth/change-password: change the MAIN KVM page password.
    Requires the current password (not just an active session) so a
    left-open browser tab can't be used to silently lock everyone else out."""
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    current = str(d.get("current", ""))
    new_pw  = str(d.get("new", ""))
    if len(new_pw) < 4:
        return web.json_response({"ok": False, "error": "New password must be at least 4 characters"}, status=400)

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    except Exception:
        cfg = {}
    auth = cfg.setdefault("auth", {})
    if not _check_pw(current, auth.get("main_password_hash", "")):
        return web.json_response({"ok": False, "error": "Current password is incorrect"}, status=403)

    if _HAS_BCRYPT:
        auth["main_password_hash"] = _bcrypt.hashpw(new_pw.encode(), _bcrypt.gensalt()).decode()
    else:
        auth["main_password_hash"] = "sha256:" + hashlib.sha256(new_pw.encode()).hexdigest()

    try:
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    log.info("Main-page password changed")
    return web.json_response({"ok": True})


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/login" or request.path.startswith("/static/"):
        return await handler(request)
    if not _is_authed(request):
        if request.path == "/ws" or request.path.startswith("/api/"):
            return web.json_response({"ok": False, "error": "auth"}, status=401)
        return web.HTTPFound("/login")
    return await handler(request)


# Global HID + Video instances
keyboard = HIDKeyboard("/dev/hidg0")
mouse    = HIDMouse("/dev/hidg1")
video    = VideoManager()

# Connected WebSocket clients (for count tracking)
_ws_clients: set = set()


# WebSocket: keyboard / mouse input handler


# -- Session / access logging -----------------------------------------------
import json as _jlog, datetime as _dt_log
_SESS_LOG = "/opt/magicbridge/data/sessions.json"

def _sess_log(sid, ip, ua, event, duration=None):
    try:
        import os as _oss
        _oss.makedirs("/opt/magicbridge/data", exist_ok=True)
        try:
            data = _jlog.loads(open(_SESS_LOG).read())
        except Exception:
            data = []
        data.append({
            "id":       sid,
            "ip":       ip,
            "ua":       ua[:200],
            "event":    event,
            "time":     _dt_log.datetime.now().isoformat(),
            "duration": duration,
        })
        data = data[-500:]
        open(_SESS_LOG, "w").write(_jlog.dumps(data, indent=2))
    except Exception:
        pass

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    ip = request.headers.get("X-Real-IP") or request.remote or "?"
    ua = request.headers.get("User-Agent", "")
    sid = _sec.token_hex(8)
    t0 = time.time()
    _ws_clients.add(ws)
    log.info("WS connect  from %s  (total: %d)", ip, len(_ws_clients))
    _sess_log(sid, ip, ua, "connect")

    loop = asyncio.get_running_loop()

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    d = json.loads(msg.data)
                    t = d.get("type", "")

                    if t == "keydown":
                        keyboard.key_down(d.get("code", ""))

                    elif t == "keyup":
                        keyboard.key_up(d.get("code", ""))

                    elif t == "release_all":
                        keyboard.release_all()
                        mouse.release_all()

                    elif t == "combo":
                        codes = list(d.get("codes", []))
                        if codes:
                            loop.run_in_executor(None, lambda c=codes: keyboard.combo(c))

                    elif t == "mousemove":
                        dx = int(d.get("dx", 0))
                        dy = int(d.get("dy", 0))
                        if dx or dy:
                            mouse.move(dx, dy)

                    elif t == "mousedown":
                        mouse.button_down(int(d.get("button", 0)))

                    elif t == "mouseup":
                        mouse.button_up(int(d.get("button", 0)))

                    elif t == "wheel":
                        mouse.scroll(int(d.get("dy", 0)))

                    elif t == "ping":
                        import json as _j
                        await ws.send_str(_j.dumps({"type":"pong","t":d.get("t",0)}))

                    elif t == "paste":
                        text  = str(d.get("text", ""))
                        delay = float(d.get("delay", 0.013))
                        delay = max(0.003, min(0.15, delay))
                        if text:
                            loop.run_in_executor(None, lambda tx=text,dl=delay: keyboard.send_text(tx,dl))

                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    pass

            elif msg.type == WSMsgType.ERROR:
                log.debug("WS error from %s: %s", ip, ws.exception())
                break

    finally:
        _ws_clients.discard(ws)
        keyboard.release_all()
        mouse.release_all()
        dur = int(time.time() - t0)
        log.info("WS disconnect %s  (total: %d, dur: %ds)", ip, len(_ws_clients), dur)
        _sess_log(sid, ip, ua, "disconnect", duration=dur)

    return ws


# HTTP handlers

async def index_handler(request: web.Request) -> web.Response:
    path = Path(WEB_ROOT) / "index.html"
    if path.exists():
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return web.Response(
        text="<h2>MagicBridge</h2><p>index.html missing from " + WEB_ROOT + "</p>",
        content_type="text/html",
        status=500,
    )


async def api_status(request: web.Request) -> web.Response:
    """Overall system status."""
    def _gather():
        import subprocess
        uptime = ""
        try:
            s = int(float(Path("/proc/uptime").read_text().split()[0]))
            d, r = divmod(s, 86400); h, r = divmod(r, 3600); m = r // 60
            uptime = "".join([f"{d}d " if d else "", f"{h}h " if h else "", f"{m}m"])
        except Exception:
            pass
        temp = None
        try:
            temp = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
        except Exception:
            pass
        local_ip = ""
        try:
            local_ip = subprocess.run(["hostname", "-I"], capture_output=True,
                                       text=True).stdout.strip().split()[0]
        except Exception:
            pass
        return uptime, temp, local_ip

    loop = asyncio.get_running_loop()
    uptime, temp, local_ip = await loop.run_in_executor(None, _gather)

    tailscale_ip = ""
    tailscale_up = False
    try:
        import subprocess as _sp
        _r = _sp.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if _r.returncode == 0:
            tailscale_ip = _r.stdout.strip().split()[0]
            tailscale_up = bool(tailscale_ip)
    except Exception:
        pass
    return web.json_response({
        "version":    VERSION,
        "clients":    len(_ws_clients),
        "hid_kb":     os.path.exists("/dev/hidg0"),
        "hid_ms":     os.path.exists("/dev/hidg1"),
        "stream":     video.status(),
        "uptime":     uptime,
        "temp_c":     temp,
        "local_ip":     local_ip,
        "tailscale_ip": tailscale_ip,
        "tailscale_up": tailscale_up,
    })


async def api_devices(request: web.Request) -> web.Response:
    loop = asyncio.get_running_loop()
    devs = await loop.run_in_executor(None, video.detect_devices)
    return web.json_response(devs)


async def api_stream_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        loop = asyncio.get_running_loop()
        st = await loop.run_in_executor(None, video.status)
        return web.json_response(st)

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    resolution = d.get("resolution")
    fps        = d.get("fps")
    quality    = d.get("quality")
    device     = d.get("device")
    mode       = d.get("mode")

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, lambda: video.start(
        device=device, resolution=resolution,
        fps=fps, quality=quality, mode=mode,
    ))

    # Persist settings to config.json
    if ok:
        try:
            cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
            cfg.setdefault("video", {}).update({
                k: v for k, v in {
                    "device": device, "resolution": resolution,
                    "fps": fps, "quality": quality, "mode": mode,
                }.items() if v is not None
            })
            Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass

    return web.json_response({"ok": ok, "status": video.status()})


# App factory



async def api_power(request):
    """Reboot or shutdown the Pi."""
    import subprocess
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    action = str(d.get("action", ""))
    if action == "shutdown":
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        return web.json_response({"ok": True, "action": "shutdown"})
    if action == "reboot":
        subprocess.Popen(["sudo", "reboot"])
        return web.json_response({"ok": True, "action": "reboot"})
    return web.json_response({"ok": False, "error": "unknown action"}, status=400)



async def api_tailscale_get(request):
    """GET /api/tailscale: check install status, connected state, login URL."""
    import subprocess, shutil, json as _json, re as _re2
    installed = bool(shutil.which("tailscale"))
    if not installed:
        return web.json_response({
            "installed": False, "connected": False,
            "ip": "", "login_url": "",
            "status": "not_installed",
            "install_cmd": "curl -fsSL https://tailscale.com/install.sh | sh"
        })
    connected = False; ip = ""; login_url = ""; backend = "unknown"
    try:
        r = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            st = _json.loads(r.stdout)
            backend = st.get("BackendState", "unknown")
            if backend == "Running":
                connected = True
                try:
                    ri = subprocess.run(["tailscale", "ip", "-4"],
                                        capture_output=True, text=True, timeout=3)
                    if ri.returncode == 0:
                        ip = ri.stdout.strip().split()[0]
                except Exception:
                    pass
            elif backend in ("NeedsLogin", "NoState", "Stopped"):
                try:
                    rl = subprocess.run(
                        ["tailscale", "up", "--timeout=2s"],
                        capture_output=True, text=True, timeout=5
                    )
                    out2 = rl.stdout + rl.stderr
                    m2 = _re2.search(r"https://login\.tailscale\.com/\S+", out2)
                    if m2:
                        login_url = m2.group(0).rstrip(".")
                except Exception:
                    pass
        else:
            backend = "error"
    except Exception:
        backend = "error"
    return web.json_response({
        "installed": True, "connected": connected,
        "ip": ip, "login_url": login_url,
        "status": backend
    })


async def api_tailscale(request):
    """POST /api/tailscale: install/up/down/login/logout."""
    import subprocess, shutil, re as _re3
    installed = bool(shutil.which("tailscale"))
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    action = str(d.get("action", ""))

    if action == "install":
        if installed:
            return web.json_response({"ok": True, "msg": "already installed"})
        r = subprocess.run(
            ["bash", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
            capture_output=True, text=True, timeout=120
        )
        return web.json_response({"ok": r.returncode == 0, "out": (r.stdout+r.stderr)[-600:]})

    if not installed:
        return web.json_response({"ok": False, "error": "tailscale_not_installed",
                                  "install_cmd": "curl -fsSL https://tailscale.com/install.sh | sh"})

    if action == "login":
        r = subprocess.run(
            ["tailscale", "up", "--timeout=3s", "--accept-routes"],
            capture_output=True, text=True, timeout=6
        )
        out3 = r.stdout + r.stderr
        m3 = _re3.search(r"https://login\.tailscale\.com/\S+", out3)
        login_url = m3.group(0).rstrip(".") if m3 else ""
        return web.json_response({
            "ok": True, "login_url": login_url,
            "out": out3[-300:], "connected": r.returncode == 0
        })

    if action == "up":
        authkey = str(d.get("authkey", ""))
        cmd = ["tailscale", "up", "--accept-routes"]
        if authkey:
            cmd += ["--authkey", authkey]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        out4 = r.stdout + r.stderr
        m4 = _re3.search(r"https://login\.tailscale\.com/\S+", out4)
        login_url = m4.group(0).rstrip(".") if m4 else ""
        return web.json_response({
            "ok": r.returncode == 0, "login_url": login_url, "out": out4[-300:]
        })

    if action == "down":
        r = subprocess.run(["tailscale", "down"], capture_output=True, text=True, timeout=10)
        return web.json_response({"ok": r.returncode == 0})

    if action == "logout":
        r = subprocess.run(["tailscale", "logout"], capture_output=True, text=True, timeout=10)
        return web.json_response({"ok": r.returncode == 0})

    return web.json_response({"ok": False, "error": "unknown action"}, status=400)



import secrets as _sec

_USB_DIR = "/sys/kernel/config/usb_gadget/g1"
_USB_PROFILES = [
    {"name":"Logitech K120",        "mfr":"Logitech",   "prod":"USB Keyboard K120",    "vid":"0x046d","pid":"0xc31c","pfx":"LGK"},
    {"name":"Microsoft Wired 600",  "mfr":"Microsoft",  "prod":"Wired Keyboard 600",   "vid":"0x045e","pid":"0x0750","pfx":"MSK"},
    {"name":"Dell KB216",           "mfr":"Dell",       "prod":"KB216 Wired Keyboard", "vid":"0x413c","pid":"0x2003","pfx":"DEL"},
    {"name":"HP KU-0316",           "mfr":"HP",         "prod":"KU-0316 Keyboard",     "vid":"0x03f0","pid":"0x0224","pfx":"HPK"},
    {"name":"Apple Magic Keyboard", "mfr":"Apple Inc.", "prod":"Magic Keyboard",       "vid":"0x05ac","pid":"0x0267","pfx":"APL"},
]

def _usb_r(rel):
    try: return open(f"{_USB_DIR}/{rel}").read().strip()
    except: return ""

def _usb_w(rel, val):
    try:
        with open(f"{_USB_DIR}/{rel}", "w") as f: f.write(val + "\n")
    except: pass

def _load_cfg():
    try:
        import json as _j
        return _j.loads(open("/etc/magicbridge/config.json").read())
    except: return {}

def _save_cfg(d):
    import json as _j
    try:
        with open("/etc/magicbridge/config.json", "w") as f:
            f.write(_j.dumps(d, indent=2))
    except: pass


def _gen_serial(profile_idx):
    """Generate a deterministic, realistic-looking USB serial for this device."""
    import random as _rr, hashlib as _hh
    try:
        mac = open("/sys/class/net/wlan0/address").read().strip().replace(":","")
    except Exception:
        try:
            mac = open("/sys/class/net/eth0/address").read().strip().replace(":","")
        except Exception:
            mac = "dca632c49b00"
    seed = int(_hh.md5((mac + str(profile_idx)).encode()).hexdigest()[:8], 16)
    rng = _rr.Random(seed)
    if profile_idx == 0:   # Logitech: YYMMcode+5digits
        yr = rng.randint(19, 23); mo = rng.randint(1, 12)
        return "%02d%02dLK%05d" % (yr, mo, rng.randint(10000, 99999))
    elif profile_idx == 1: # Microsoft: numeric 12 digits
        return "780%09d" % rng.randint(100000000, 999999999)
    elif profile_idx == 2: # Dell: CN0 + 13 alphanumeric
        ch = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
        return "CN0" + "".join(rng.choices(ch, k=13))
    elif profile_idx == 3: # HP: CN + 10 digits
        return "CN0" + "".join(rng.choices("0123456789", k=10))
    elif profile_idx == 4: # Apple: 12 uppercase alphanumeric
        ch = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(rng.choices(ch, k=12))
    else:
        return "".join(rng.choices("0123456789ABCDEF", k=12))

async def api_identity(request):
    import time as _t
    if request.method == "GET":
        return web.json_response({
            "product":      _usb_r("strings/0x409/product")      or "USB Keyboard K120",
            "manufacturer": _usb_r("strings/0x409/manufacturer") or "Logitech",
            "serial":       _usb_r("strings/0x409/serialnumber") or "—",
            "vid":          _usb_r("idVendor"),
            "pid":          _usb_r("idProduct"),
            "profiles":     _USB_PROFILES,
        })
    try: d = await request.json()
    except: return web.json_response({"ok": False, "error": "bad json"}, status=400)
    action = d.get("action", "")
    if action == "profile":
        idx = int(d.get("idx", 0))
        if idx < 0 or idx >= len(_USB_PROFILES):
            return web.json_response({"ok": False, "error": "bad idx"}, status=400)
        p = _USB_PROFILES[idx]
        ser = _gen_serial(idx)
        udc = _usb_r("UDC")
        _usb_w("UDC", "")
        _t.sleep(0.3)
        _usb_w("strings/0x409/manufacturer", p["mfr"])
        _usb_w("strings/0x409/product",      p["prod"])
        _usb_w("strings/0x409/serialnumber", ser)
        _usb_w("idVendor",  p["vid"])
        _usb_w("idProduct", p["pid"])
        _t.sleep(0.3)
        if udc: _usb_w("UDC", udc)
        cfg = _load_cfg()
        cfg.setdefault("usb", {}).update({"manufacturer": p["mfr"], "product": p["prod"],
                                           "serial": ser, "idVendor": p["vid"], "idProduct": p["pid"]})
        _save_cfg(cfg)
        return web.json_response({"ok": True, "product": p["prod"], "serial": ser})
    return web.json_response({"ok": False, "error": "unknown action"}, status=400)


async def api_wifi(request):
    import subprocess as _sp, json as _j
    if request.method == "GET":
        r = _sp.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                    capture_output=True, text=True, timeout=8)
        networks = []
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and ("wireless" in parts[1].lower() or "wifi" in parts[1].lower()):
                networks.append(parts[0])
        # also get current connection
        r2 = _sp.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"],
                     capture_output=True, text=True, timeout=8)
        active = ""
        for line in r2.stdout.splitlines():
            if line.startswith("yes:"):
                active = line[4:].strip()
                break
        return web.json_response({"networks": networks, "active": active})
    try: d = await request.json()
    except: return web.json_response({"ok": False, "error": "bad json"}, status=400)
    action = d.get("action", "")
    if action == "add":
        ssid = d.get("ssid", "").strip()
        psk  = d.get("psk",  "").strip()
        if not ssid:
            return web.json_response({"ok": False, "error": "no ssid"})
        if psk and len(psk) < 8:
            return web.json_response({"ok": False, "error": "Password must be at least 8 characters"})
        cmd = ["nmcli", "connection", "add", "type", "wifi",
               "con-name", ssid, "ssid", ssid,
               "connection.autoconnect", "yes",
               "connection.autoconnect-priority", "10"]
        if psk:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        r = _sp.run(cmd, capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        out = (r.stdout + r.stderr)[:300]
        if not ok and "already" in out.lower():
            if psk:
                r2 = _sp.run(["nmcli", "connection", "modify", ssid,
                               "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk],
                              capture_output=True, text=True, timeout=10)
            else:
                r2 = _sp.run(["nmcli", "connection", "modify", ssid,
                               "wifi-sec.key-mgmt", "none"],
                              capture_output=True, text=True, timeout=10)
            ok = r2.returncode == 0
            out = (r2.stdout + r2.stderr)[:300]
        return web.json_response({"ok": ok, "out": out})
    if action == "remove":
        name = d.get("name", "").strip()
        r = _sp.run(["nmcli", "connection", "delete", name],
                    capture_output=True, text=True, timeout=10)
        return web.json_response({"ok": r.returncode == 0})
    if action == "connect":
        name = d.get("name", "").strip()
        r = _sp.run(["nmcli", "connection", "up", name],
                    capture_output=True, text=True, timeout=20)
        return web.json_response({"ok": r.returncode == 0, "out": (r.stdout + r.stderr)[:200]})
    return web.json_response({"ok": False, "error": "unknown action"}, status=400)







async def api_update(request):
    """POST /api/update: pull the latest code from GitHub and redeploy.

    /opt/magicbridge is a flat runtime directory, not a git repo. install.sh
    populates it by copying files out of a real clone at /opt/magicbridge-repo.
    This handler pulls that clone, then re-runs the same file-copy step
    install.sh uses to deploy into /opt/magicbridge.
    """
    import subprocess as _sp
    import shutil as _shutil

    REPO_DIR = "/opt/magicbridge-repo"
    REPO_URL = "https://github.com/razzrohith/MagicBridge.git"
    BRANCH = "main"
    INSTALL_DIR = "/opt/magicbridge"

    d = {}
    try: d = await request.json()
    except: pass
    action = d.get("action", "update")

    def _ensure_clone():
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            r = _sp.run(["git", "clone", "--branch", BRANCH, REPO_URL, REPO_DIR],
                        capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        return True, ""

    cloned_ok, clone_out = _ensure_clone()
    if not cloned_ok:
        return web.json_response({"ok": False, "error": "clone failed: " + clone_out})

    if action == "status":
        ver = _sp.run(["git", "-C", REPO_DIR, "log", "--oneline", "-1"],
                     capture_output=True, text=True, timeout=10)
        branch = _sp.run(["git", "-C", REPO_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=10)
        fetch = _sp.run(["git", "-C", REPO_DIR, "fetch", "--dry-run"],
                       capture_output=True, text=True, timeout=20)
        return web.json_response({
            "ok": True,
            "version": ver.stdout.strip(),
            "branch": branch.stdout.strip(),
            "out": (fetch.stdout + fetch.stderr).strip() or "Up to date"
        })

    # action == "update"
    r = _sp.run(["git", "-C", REPO_DIR, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).strip()
    ok = r.returncode == 0
    if not ok:
        return web.json_response({"ok": False, "out": out, "restarted": False})

    if "Already up to date" in out:
        return web.json_response({"ok": True, "out": out, "restarted": False})

    try:
        # Mirror install.sh's file layout exactly (flat copy, no "src/" prefix,
        # no .git, /opt/magicbridge stays a plain runtime dir)
        for sub in ("core", "web", "dashboard", "provision"):
            os.makedirs(os.path.join(INSTALL_DIR, sub), exist_ok=True)

        copy_pairs = [
            (f"{REPO_DIR}/src/core/magicbridge.py",  f"{INSTALL_DIR}/core/magicbridge.py"),
            (f"{REPO_DIR}/src/core/hid.py",           f"{INSTALL_DIR}/core/hid.py"),
            (f"{REPO_DIR}/src/core/video.py",         f"{INSTALL_DIR}/core/video.py"),
            (f"{REPO_DIR}/src/web/index.html",        f"{INSTALL_DIR}/web/index.html"),
            (f"{REPO_DIR}/src/dashboard/stealth-dashboard.py", f"{INSTALL_DIR}/dashboard/stealth-dashboard.py"),
            (f"{REPO_DIR}/src/provision/mb-setup-ui.py", f"{INSTALL_DIR}/provision/mb-setup-ui.py"),
            (f"{REPO_DIR}/src/core/mb-gadget.sh",      "/usr/local/bin/mb-gadget.sh"),
            (f"{REPO_DIR}/src/provision/mb-provision.sh", "/usr/local/bin/mb-provision.sh"),
            (f"{REPO_DIR}/src/nginx/magicbridge.conf", "/etc/nginx/sites-available/magicbridge"),
        ]
        for src, dst in copy_pairs:
            if os.path.isfile(src):
                _shutil.copyfile(src, dst)

        static_src = f"{REPO_DIR}/src/web/static"
        if os.path.isdir(static_src):
            _shutil.copytree(static_src, f"{INSTALL_DIR}/web/static", dirs_exist_ok=True)

        os.chmod("/usr/local/bin/mb-gadget.sh", 0o755)
        os.chmod("/usr/local/bin/mb-provision.sh", 0o755)
    except Exception as e:
        return web.json_response({"ok": False, "out": out + "\ncopy step failed: " + str(e), "restarted": False})

    # Validate nginx config before reloading, never reload on a broken config
    nginx_test = _sp.run(["nginx", "-t"], capture_output=True, text=True, timeout=10)
    nginx_ok = nginx_test.returncode == 0
    if nginx_ok:
        _sp.run(["systemctl", "reload", "nginx"], capture_output=True, timeout=10)

    # Restart the app services only. Deliberately skip mb-gadget.service so an
    # active USB HID session to the target PC isn't interrupted by an update.
    _sp.run(["systemctl", "restart", "stealth-dashboard.service"], capture_output=True, timeout=10)
    _sp.run(["systemctl", "restart", "magicbridge.service"], capture_output=True, timeout=10)

    return web.json_response({
        "ok": True,
        "out": out,
        "restarted": True,
        "nginx_ok": nginx_ok,
        "nginx_out": (nginx_test.stdout + nginx_test.stderr).strip(),
    })


async def api_stealth_logs(request):
    """GET /api/stealth/logs: access log (stealth dashboard only)."""
    try:
        data = _jlog.loads(open(_SESS_LOG).read())
    except Exception:
        data = []
    return web.json_response({"ok": True, "sessions": list(reversed(data[-200:]))})


async def api_ai_run(request):
    """POST /api/ai/run: proxy a natural-language command into a KVM action sequence."""
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    provider = d.get("provider", "openrouter")
    api_key  = d.get("key", "").strip()
    model    = d.get("model", "").strip()
    command  = d.get("command", "").strip()
    if not api_key:
        return web.json_response({"ok": False, "error": "No API key provided"})
    if not command:
        return web.json_response({"ok": False, "error": "No command provided"})

    # System prompt describes the JSON action format without embedding literal braces
    # (avoids Python string escaping issues in generated code)
    system_prompt = (
        "You control a Windows PC via KVM (keyboard and mouse only). "
        "Respond ONLY with valid JSON, no extra text before or after. "
        "Top-level key is 'actions', a list of action objects. "
        "Action type 'combo': simultaneous key press. Fields: type, codes (list of strings). "
        "Action type 'key': single key tap. Fields: type, code (string). "
        "Action type 'paste': type text. Fields: type, text (string), delay (float, seconds per char, default 0.07). "
        "Action type 'wait': pause. Fields: type, ms (integer milliseconds). "
        "Valid key codes: MetaLeft, ControlLeft, ShiftLeft, AltLeft, "
        "KeyA-KeyZ, Digit0-Digit9, Return, Escape, Tab, Space, "
        "Backspace, Delete, F1-F12, ArrowUp, ArrowDown, ArrowLeft, ArrowRight. "
        "Example: to open Run dialog type Win+R then type notepad then press Enter. "
        "Output JSON ONLY."
    )

    import urllib.request as _ur, json as _jj
    try:
        if provider in ("openrouter", "claude_or"):
            if not model: model = "anthropic/claude-haiku-4-5"
            payload = _jj.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": command}
                ],
                "temperature": 0.1
            }).encode()
            req = _ur.Request(
                "https://openrouter.ai/api/v1/chat/completions", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_key,
                         "HTTP-Referer": "https://magicbridge.local",
                         "X-Title": "MagicBridge"})
            with _ur.urlopen(req, timeout=30) as r:
                raw = _jj.loads(r.read())
            text = raw["choices"][0]["message"]["content"].strip()

        elif provider == "gemini":
            if not model: model = "gemini-1.5-flash"
            payload = _jj.dumps({
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": command}]}],
                "generationConfig": {"temperature": 0.1}
            }).encode()
            req = _ur.Request(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + model + ":generateContent?key=" + api_key,
                data=payload, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=30) as r:
                raw = _jj.loads(r.read())
            text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()

        elif provider == "openai":
            if not model: model = "gpt-4o-mini"
            payload = _jj.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": command}
                ],
                "temperature": 0.1
            }).encode()
            req = _ur.Request(
                "https://api.openai.com/v1/chat/completions", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_key})
            with _ur.urlopen(req, timeout=30) as r:
                raw = _jj.loads(r.read())
            text = raw["choices"][0]["message"]["content"].strip()

        elif provider == "claude":
            if not model: model = "claude-haiku-4-5-20251001"
            payload = _jj.dumps({
                "model": model, "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": command}]
            }).encode()
            req = _ur.Request(
                "https://api.anthropic.com/v1/messages", data=payload,
                headers={"Content-Type": "application/json",
                         "x-api-key": api_key,
                         "anthropic-version": "2023-06-01"})
            with _ur.urlopen(req, timeout=30) as r:
                raw = _jj.loads(r.read())
            text = raw["content"][0]["text"].strip()

        else:
            return web.json_response({"ok": False, "error": "Unknown provider: " + provider})

        import re as _rai
        # Extract JSON object from AI response
        m = _rai.search(r'[{].*[}]', text, _rai.DOTALL)
        actions_obj = _jj.loads(m.group() if m else text)
        return web.json_response({"ok": True, "actions": actions_obj.get("actions", []), "raw": text[:500]})

    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:300]})


def build_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024, middlewares=[auth_middleware])

    app.router.add_get("/login",  login_handler)
    app.router.add_post("/login", login_handler)
    app.router.add_post("/logout", logout_handler)
    app.router.add_post("/api/auth/change-password", api_change_password)

    app.router.add_get("/",                      index_handler)
    app.router.add_get("/ws",                    ws_handler)
    app.router.add_get("/api/status",            api_status)
    app.router.add_get("/api/devices",           api_devices)
    app.router.add_get("/api/stream/settings",   api_stream_settings)
    app.router.add_post("/api/stream/settings",  api_stream_settings)
    app.router.add_post("/api/power",     api_power)
    app.router.add_get("/api/identity",  api_identity)
    app.router.add_post("/api/identity", api_identity)
    app.router.add_get("/api/networks",   api_wifi)
    app.router.add_post("/api/networks",  api_wifi)
    app.router.add_get("/api/tailscale",  api_tailscale_get)
    app.router.add_post("/api/tailscale", api_tailscale)
    app.router.add_post("/api/update", api_update)
    app.router.add_get("/api/stealth/logs",  api_stealth_logs)
    app.router.add_get("/api/ai/run",         api_ai_run)
    app.router.add_post("/api/ai/run",        api_ai_run)

    # Serve static files if present (CSS, JS, images, etc.)
    web_static = Path(WEB_ROOT) / "static"
    if web_static.exists():
        app.router.add_static("/static", str(web_static), show_index=False)

    return app


# Main entry point

async def main():
    log.info("MagicBridge v%s starting…", VERSION)

    _ensure_auth_defaults()

    # Load config
    cfg = {}
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text())
        log.info("Config loaded from %s", CONFIG_PATH)
    except FileNotFoundError:
        log.info("No config at %s, using defaults", CONFIG_PATH)
    except Exception as e:
        log.warning("Config load error: %s, using defaults", e)

    # Start video stream
    vc  = cfg.get("video", {})
    loop = asyncio.get_running_loop()
    log.info("Starting video stream…")
    ok = await loop.run_in_executor(None, lambda: video.start(
        device     = vc.get("device"),
        resolution = vc.get("resolution", "1920x1080"),
        fps        = int(vc.get("fps", 30)),
        quality    = int(vc.get("quality", 80)),
        mode       = vc.get("mode", "mjpeg"),
    ))
    if ok:
        log.info("Stream started: %s", video.status())
        video.start_watchdog()
    else:
        log.warning("Stream not started, no capture device or streamer found")

    # Start HTTP server
    app    = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT, reuse_address=True)
    await site.start()
    log.info("HTTP+WS listening on %s:%d", HOST, PORT)

    # Run until killed
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down…")
        keyboard.release_all()
        mouse.release_all()
        video.stop()
        await runner