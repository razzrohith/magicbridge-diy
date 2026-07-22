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
import hid
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


# ── TOTP (RFC 6238) ──────────────────────────────────────────────────────────
# Hand-rolled rather than adding pyotp: it is ~20 lines of hmac, and every extra
# dependency is another thing that can be missing on a fresh flash and take the
# login page down with it. Verified against the RFC 6238 SHA-1 test vectors.
#
# ANTI-LOCKOUT is the whole design here - this guards the only way into the
# device, so every path assumes the user WILL lose their phone:
#   * 2FA cannot be enabled without first entering a live code, so a mistyped
#     or mis-scanned secret is caught before it can lock anyone out;
#   * ten single-use recovery codes are issued at setup;
#   * `magicbridge.py --disable-2fa` turns it off from an SSH shell;
#   * a +/-1 step window absorbs ordinary phone/Pi clock drift.
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

def _b32decode(s: str) -> bytes:
    s = "".join(c for c in s.upper() if c in _B32)
    bits = "".join(bin(_B32.index(c))[2:].zfill(5) for c in s)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - 7, 8))

def _totp_at(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = _b32decode(secret_b32)
    msg = counter.to_bytes(8, "big")
    dig = hmac.new(key, msg, hashlib.sha1).digest()
    off = dig[-1] & 0x0F
    code = ((dig[off] & 0x7F) << 24 | dig[off + 1] << 16 |
            dig[off + 2] << 8 | dig[off + 3]) % (10 ** digits)
    return str(code).zfill(digits)

def _totp_verify(secret_b32: str, code: str, window: int = 1) -> bool:
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if not code or not secret_b32:
        return False
    counter = int(time.time()) // 30
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_totp_at(secret_b32, counter + drift), code):
            return True
    return False

def _totp_new_secret() -> str:
    import secrets as _s
    return "".join(_s.choice(_B32) for _ in range(32))

def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").upper().encode()).hexdigest()

def _totp_enabled() -> bool:
    a = _auth_cfg()
    return bool(a.get("totp_enabled")) and bool(a.get("totp_secret"))

def _totp_check_login(code: str) -> bool:
    """Accept a live TOTP code OR consume a single-use recovery code."""
    auth = _auth_cfg()
    if _totp_verify(auth.get("totp_secret", ""), code):
        return True
    h = _recovery_hash(code)
    remaining = list(auth.get("totp_recovery", []))
    if h in remaining:
        remaining.remove(h)                      # single use - burn it
        try:
            cfg = json.loads(Path(CONFIG_PATH).read_text())
            cfg.setdefault("auth", {})["totp_recovery"] = remaining
            Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
            log.warning("2FA: recovery code used (%d left)", len(remaining))
        except Exception as e:
            log.error("2FA: could not burn recovery code: %s", e)
        return True
    return False

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


_PLACEHOLDER_SERIAL = "12AB34CD"

# has_serial/extra_iface reflect what real hardware for each identity
# actually does, verified against real Unifying Receiver USB descriptors:
# a genuine Logitech Unifying Receiver reports NO serial number at all
# (iSerial = 0) and exposes 3 USB interfaces (two idle boot interfaces
# plus one vendor HID++ interface), not just a plain 2-interface
# keyboard+mouse composite device. Microsoft/Dell equivalents are left
# as before (verified descriptor data wasn't available for them), so
# only the Logitech default/profile is corrected here.
_DEFAULT_USB_IDENTITY = {
    "manufacturer": "Logitech",
    "product":      "USB Receiver",   # Unifying Receiver: legitimately has both keyboard+mouse interfaces
    "idVendor":     "0x046d",
    "idProduct":    "0xc52b",
    "has_serial":   False,            # real Unifying Receivers have iSerial = 0
    "extra_iface":  True,             # real ones expose a 3rd (idle) HID interface
}
_OLD_DEFAULT_PRODUCTS = {"USB Keyboard K120"}  # pre-migration defaults, replaced below

def _ensure_usb_defaults():
    """Bootstrap/migrate to a realistic default USB identity, instead of
    requiring someone to open the stealth panel and click a preset button
    first. Handles three cases:
      - Fresh config: replaces the placeholder serial ("12AB34CD") with a
        realistic per-device one (_gen_serial, seeded from this Pi's MAC).
      - Older installs already bootstrapped with the previous default
        (a keyboard-only "USB Keyboard K120" identity, which structurally
        can't have a mouse interface, a giveaway since this gadget always
        exposes one): migrates them to the Unifying Receiver identity,
        which legitimately combines both.
      - Installs already on the Unifying Receiver identity but still
        carrying a serial number: removes it, since a real Unifying
        Receiver reports none (iSerial = 0) and having one is itself a
        mismatch.
    Also applies the result to the live USB gadget immediately, in case
    mb-gadget.sh already applied the old value a few seconds earlier at
    boot. The extra HID interface (for interface-count realism) can only
    be created when the gadget's functions are (re)built, i.e. on next
    reboot / mb-gadget.sh run, not live here."""
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    except Exception:
        cfg = {}
    usb = cfg.setdefault("usb", {})
    current_serial  = usb.get("serial", "")
    current_product = usb.get("product", "")
    needs_serial_fix    = (not current_serial) or (current_serial == _PLACEHOLDER_SERIAL)
    needs_identity_fix  = current_product in _OLD_DEFAULT_PRODUCTS
    needs_serial_removal = (current_product == _DEFAULT_USB_IDENTITY["product"]
                             and current_serial not in ("", None))
    if not needs_serial_fix and not needs_identity_fix and not needs_serial_removal:
        return  # already on the current default, nothing to do
    if needs_identity_fix:
        usb.update(_DEFAULT_USB_IDENTITY)
        usb["serial"] = ""
    elif needs_serial_removal:
        usb["serial"] = ""
        usb.setdefault("has_serial", False)
        usb.setdefault("extra_iface", True)
    else:
        # generic fresh-config bootstrap onto the default identity
        usb.setdefault("manufacturer", _DEFAULT_USB_IDENTITY["manufacturer"])
        usb.setdefault("product", _DEFAULT_USB_IDENTITY["product"])
        usb.setdefault("idVendor", _DEFAULT_USB_IDENTITY["idVendor"])
        usb.setdefault("idProduct", _DEFAULT_USB_IDENTITY["idProduct"])
        usb.setdefault("has_serial", _DEFAULT_USB_IDENTITY["has_serial"])
        usb.setdefault("extra_iface", _DEFAULT_USB_IDENTITY["extra_iface"])
        usb["serial"] = "" if not usb.get("has_serial", True) else _gen_serial(0)
    try:
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
        log.info("USB identity defaults updated (serial=%r, identity_fix=%s)",
                  usb.get("serial"), needs_identity_fix)
    except Exception as e:
        log.warning("Could not persist default USB identity: %s", e)
    try:
        udc = _usb_r("UDC")
        _usb_w("UDC", "")
        _usb_w("strings/0x409/serialnumber", usb.get("serial", ""))
        if needs_identity_fix:
            _usb_w("strings/0x409/manufacturer", usb["manufacturer"])
            _usb_w("strings/0x409/product", usb["product"])
            _usb_w("idVendor", usb["idVendor"])
            _usb_w("idProduct", usb["idProduct"])
        if udc:
            _usb_w("UDC", udc)
    except Exception:
        pass


# Login rate limiting: mirrors stealth-dashboard.py's progressive delay so
# the main KVM login gets the same brute-force protection the admin panel
# has always had. Per-IP, in-memory, resets on a successful login.
_login_fails: dict = {}

def _login_client_ip(request: web.Request) -> str:
    return request.headers.get("X-Real-IP") or request.remote or "?"

async def _apply_login_delay(ip: str):
    n = _login_fails.get(ip, 0)
    if n > 0:
        await asyncio.sleep(min(n, 10))

def _record_login_fail(ip: str):
    _login_fails[ip] = _login_fails.get(ip, 0) + 1

def _record_login_ok(ip: str):
    _login_fails.pop(ip, None)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MagicBridge</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%;background:#040911;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#f1fbff}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem;position:relative;overflow:hidden}
/* Same gradient-mesh + scanline backdrop as the main app, so this doesn't
   feel like a different product from the thing you're about to unlock. */
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 900px 620px at 12% -8%,  rgba(34,211,238,.10), transparent 60%),
    radial-gradient(ellipse 760px 560px at 108% 18%, rgba(14,165,196,.11), transparent 60%),
    radial-gradient(ellipse 820px 640px at 46% 118%, rgba(11,122,148,.10), transparent 62%),
    linear-gradient(180deg,#040911 0%,#060c17 55%,#040911 100%);
  animation:drift 16s ease-in-out infinite alternate;}
@keyframes drift{0%{filter:hue-rotate(0deg) brightness(1)}100%{filter:hue-rotate(8deg) brightness(1.06)}}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.5;
  background-image:
    repeating-linear-gradient(180deg, rgba(120,220,255,.025) 0px, rgba(120,220,255,.025) 1px, transparent 1px, transparent 3px),
    linear-gradient(rgba(120,220,255,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(120,220,255,.035) 1px, transparent 1px);
  background-size:auto,42px 42px,42px 42px;}
.card{position:relative;z-index:1;background:rgba(13,23,40,.66);backdrop-filter:blur(20px) saturate(140%);
      -webkit-backdrop-filter:blur(20px) saturate(140%);
      border:1px solid rgba(140,220,255,.16);border-radius:18px;
      padding:2.3rem 2.1rem 2rem;width:100%;max-width:328px;
      box-shadow:0 20px 60px rgba(0,0,0,.6), 0 0 0 1px rgba(34,211,238,.04) inset;
      animation:rise .5s cubic-bezier(.16,1,.3,1)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.brand{display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:1.7rem}
.brand-icon{width:52px;height:52px;margin-bottom:12px;position:relative}
.brand-icon svg{width:100%;height:100%;filter:drop-shadow(0 0 14px rgba(34,211,238,.45))}
.brand-icon::after{content:'';position:absolute;inset:-8px;border-radius:50%;
  background:radial-gradient(circle,rgba(34,211,238,.22),transparent 70%);
  animation:pulse 2.6s ease-in-out infinite;z-index:-1}
@keyframes pulse{0%,100%{opacity:.5;transform:scale(.94)}50%{opacity:1;transform:scale(1.04)}}
h1{font-size:19px;font-weight:700;letter-spacing:-.3px;
   background:linear-gradient(135deg,#8beefc 0%,#22d3ee 55%,#0b7a94 100%);
   -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{display:flex;align-items:center;gap:6px;font-size:11.5px;color:#a9cfe0;margin-top:6px}
.sub .dot{width:6px;height:6px;border-radius:50%;background:#10b981;
  box-shadow:0 0 8px rgba(16,185,129,.7);animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
label{display:block;font-size:10.5px;color:#a9cfe0;margin-bottom:6px;font-weight:600;
  text-transform:uppercase;letter-spacing:.06em}
.pw-wrap{position:relative}
.pw-wrap svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;color:#88a8bd;pointer-events:none}
input[type=password]{width:100%;padding:12px 12px 12px 36px;background:rgba(4,9,17,.7);
  border:1px solid rgba(140,220,255,.16);border-radius:10px;color:#f1fbff;font-size:13.5px;outline:none;
  transition:border-color .15s,box-shadow .15s}
input[type=password]:focus{border-color:#22d3ee;box-shadow:0 0 0 3px rgba(34,211,238,.14)}
input[type=password]::placeholder{color:#6f93a8}
button{margin-top:1.1rem;width:100%;padding:11px;position:relative;overflow:hidden;
  background:linear-gradient(135deg,#8beefc 0%,#22d3ee 55%,#0b7a94 100%);
  border:none;border-radius:10px;color:#04141a;font-size:13.5px;font-weight:700;cursor:pointer;
  letter-spacing:.01em;transition:filter .15s,transform .1s;
  box-shadow:0 4px 20px rgba(34,211,238,.25)}
button:hover{filter:brightness(1.1)}
button:active{transform:scale(.98)}
.err{margin-top:.9rem;padding:10px 12px;background:rgba(244,63,94,.12);
  border:1px solid rgba(244,63,94,.35);border-radius:9px;font-size:12px;color:#fca5b4;
  animation:shake .35s ease}
@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-4px)}
  40%{transform:translateX(4px)}60%{transform:translateX(-3px)}80%{transform:translateX(3px)}}
.foot{margin-top:1.5rem;text-align:center;font-size:10px;color:#6f93a8;letter-spacing:.02em}
</style></head><body><main><div class="card">
<div class="brand">
  <div class="brand-icon">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="MagicBridge">
      <defs><linearGradient id="lg1" x1="0" y1="0" x2="100" y2="100" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#8beefc"/><stop offset="55%" stop-color="#22d3ee"/><stop offset="100%" stop-color="#0b7a94"/>
      </linearGradient></defs>
      <path d="M15 40 C15 25 30 20 50 20 C70 20 85 25 85 40 C85 55 72 58 50 58 C28 58 15 55 15 40 Z" fill="none" stroke="url(#lg1)" stroke-width="5"/>
      <path d="M50 20 L50 58" stroke="url(#lg1)" stroke-width="3.4" opacity=".4"/>
      <circle cx="32" cy="40" r="3.4" fill="url(#lg1)"/>
      <circle cx="68" cy="40" r="3.4" fill="url(#lg1)"/>
      <path d="M22 66 Q50 78 78 66" stroke="url(#lg1)" stroke-width="4.2" fill="none" opacity=".5"/>
      <path d="M28 74 Q50 84 72 74" stroke="url(#lg1)" stroke-width="3.4" fill="none" opacity=".3"/>
    </svg>
  </div>
  <h1>MagicBridge</h1>
  <div class="sub"><span class="dot"></span>System online - sign in to take control</div>
</div>
__ERROR__
<form method="POST" action="/login">
<label for="pw">Password</label>
<div class="pw-wrap">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
  <input type="password" id="pw" name="pw" placeholder="Enter password" autocomplete="current-password" autofocus>
</div>
__TOTP__
<button type="submit">Unlock MagicBridge</button>
</form>
<div class="foot">Self-hosted KVM-over-IP · this device only</div>
</div></main></body></html>"""


def _login_page(error: str = "", status: int = 200) -> web.Response:
    """Render the login page, showing the 2FA field only when 2FA is on."""
    html = LOGIN_HTML.replace(
        "__ERROR__", f'<div class="err">{error}</div>' if error else "")
    if _totp_enabled():
        html = html.replace("__TOTP__",
            '<label for="code">2FA code</label>'
            '<div class="pw-wrap">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/></svg>'
            '<input type="text" id="code" name="code" placeholder="6-digit code (or recovery code)" '
            'autocomplete="one-time-code" inputmode="text"></div>')
    else:
        html = html.replace("__TOTP__", "")
    return web.Response(text=html, content_type="text/html", status=status)


async def login_handler(request: web.Request) -> web.Response:
    if request.method == "POST":
        ip = _login_client_ip(request)
        await _apply_login_delay(ip)
        try:
            data = await request.post()
            pw = str(data.get("pw", ""))
        except Exception:
            pw = ""
        try:
            code = str(data.get("code", ""))
        except Exception:
            code = ""
        auth = _auth_cfg()
        if pw and _check_pw(pw, auth.get("main_password_hash", "")):
            # Password is right; if 2FA is on it still has to clear the second
            # factor. Deliberately ONE form rather than a two-step flow with
            # server-side pending state - a half-authenticated state is exactly
            # where an auth bug becomes a bypass or a lockout.
            if _totp_enabled() and not _totp_check_login(code):
                _record_login_fail(ip)
                log.info("2FA failed from %s", ip)
                return _login_page("Incorrect or expired 2FA code.", status=401)
            _record_login_ok(ip)
            secret = auth.get("main_secret_key", "")
            resp = web.HTTPFound("/")
            if secret:
                resp.set_cookie(SESSION_COOKIE, _make_token(secret),
                                 max_age=SESSION_TIMEOUT, httponly=True,
                                 secure=True, samesite="Lax", path="/")
            log.info("Login OK from %s", ip)
            return resp
        _record_login_fail(ip)
        log.info("Failed login from %s (attempt %d)", ip, _login_fails.get(ip, 0))
        return _login_page("Incorrect password.", status=401)
    return _login_page()


async def logout_handler(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


def _save_auth(mutate) -> str:
    """Read config, mutate cfg['auth'] via callback, write back. '' = ok."""
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text())
    except Exception:
        cfg = {}
    mutate(cfg.setdefault("auth", {}))
    try:
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH + ".new"
        Path(tmp).write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
        return ""
    except Exception as e:
        return str(e)


async def api_2fa(request: web.Request) -> web.Response:
    """GET  -> {enabled, recovery_left}
    POST action=setup   -> new secret + otpauth URI + recovery codes (NOT enabled yet)
         action=enable  -> requires a WORKING code for the pending secret
         action=disable -> requires current password AND a working code
    """
    if request.method == "GET":
        a = _auth_cfg()
        return web.json_response({"ok": True, "enabled": _totp_enabled(),
                                  "pending": bool(a.get("totp_pending")),
                                  "recovery_left": len(a.get("totp_recovery", []))})
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    action = str(d.get("action", ""))
    auth = _auth_cfg()

    if action == "setup":
        # Store as PENDING. Nothing about login changes until 'enable' proves a
        # real code works - so a mis-scanned QR can never lock anyone out.
        import secrets as _s
        secret = _totp_new_secret()
        codes = ["-".join([_s.token_hex(2).upper(), _s.token_hex(2).upper()]) for _ in range(10)]
        err = _save_auth(lambda a: a.update({
            "totp_pending": secret,
            "totp_recovery_pending": [_recovery_hash(c) for c in codes]}))
        if err:
            return web.json_response({"ok": False, "error": err}, status=500)
        label = "MagicBridge"
        # socket is not imported at module scope; and the hostname is the
        # spoofed DESKTOP-XXXXXXX one, which is exactly what should appear in
        # the authenticator app - it must not read "MagicBridge Pi" on a phone.
        import socket as _sock
        uri = (f"otpauth://totp/{label}:{_sock.gethostname()}?secret={secret}"
               f"&issuer={label}&algorithm=SHA1&digits=6&period=30")
        # Recovery codes are returned ONCE, in the clear, and only their hashes
        # are stored - same reason password hashes exist.
        return web.json_response({"ok": True, "secret": secret, "uri": uri,
                                  "recovery": codes})

    if action == "enable":
        pending = auth.get("totp_pending", "")
        if not pending:
            return web.json_response({"ok": False, "error": "Run setup first"}, status=400)
        if not _totp_verify(pending, str(d.get("code", ""))):
            return web.json_response(
                {"ok": False, "error": "That code doesn't match - 2FA NOT enabled. "
                                       "Check your phone's clock and try again."}, status=403)
        err = _save_auth(lambda a: (a.update({
            "totp_secret": pending, "totp_enabled": True,
            "totp_recovery": a.get("totp_recovery_pending", [])}),
            a.pop("totp_pending", None), a.pop("totp_recovery_pending", None)))
        if err:
            return web.json_response({"ok": False, "error": err}, status=500)
        log.warning("2FA ENABLED")
        return web.json_response({"ok": True, "enabled": True})

    if action == "disable":
        # Password AND a code: an unattended browser tab must not be able to
        # silently strip the second factor.
        if not _check_pw(str(d.get("password", "")), auth.get("main_password_hash", "")):
            return web.json_response({"ok": False, "error": "Password is incorrect"}, status=403)
        if _totp_enabled() and not _totp_check_login(str(d.get("code", ""))):
            return web.json_response({"ok": False, "error": "2FA code is incorrect"}, status=403)
        err = _save_auth(lambda a: (a.update({"totp_enabled": False}),
                                    a.pop("totp_secret", None),
                                    a.pop("totp_recovery", None),
                                    a.pop("totp_pending", None),
                                    a.pop("totp_recovery_pending", None)))
        if err:
            return web.json_response({"ok": False, "error": err}, status=500)
        log.warning("2FA disabled")
        return web.json_response({"ok": True, "enabled": False})

    return web.json_response({"ok": False, "error": "unknown action"}, status=400)


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

    # Rotate the session secret so any other already-issued session cookie
    # (anywhere else this password was used) stops validating immediately.
    # _is_authed() re-reads main_secret_key from disk on every request, so
    # this takes effect on the very next request from any other session.
    # A fresh token signed with the new secret is issued below so the
    # browser making this change doesn't get logged out too.
    import secrets as _secrets
    new_secret = _secrets.token_hex(32)
    auth["main_secret_key"] = new_secret

    try:
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    log.info("Main-page password changed, other sessions invalidated")
    resp = web.json_response({"ok": True})
    resp.set_cookie(SESSION_COOKIE, _make_token(new_secret),
                     max_age=SESSION_TIMEOUT, httponly=True,
                     secure=True, samesite="Lax", path="/")
    return resp


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

# Apply whatever target-keyboard-layout was saved last time, so a reboot
# doesn't silently fall back to the "us" default for someone who set it to
# something else. See hid.py's CHAR_MAPS comment for why this matters -
# wrong layout means garbled paste/AI-typed text, not a crash, so this is
# easy to miss without checking on every boot.
try:
    _boot_cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    hid.set_layout(_boot_cfg.get("keyboard", {}).get("layout", "us"))
    # Match the live HIDMouse report format to whatever mouse mode the gadget
    # was built with at boot (mb-gadget.sh reads the same usb.mouse_mode).
    mouse.set_absolute(_boot_cfg.get("usb", {}).get("mouse_mode", "relative") == "absolute")
except Exception:
    pass

# Connected WebSocket clients (for count tracking)
_ws_clients: set = set()
# Per-connection metadata (ip + connect time), keyed by the ws object, so the
# top bar can show WHO is connected / controlling right now and for how long.
# Populated/cleared alongside _ws_clients in ws_handler. Read from the async
# handler only (single event loop) - no lock needed.
_ws_info: dict = {}

# Last time a real (human-driven, via the WS connection) keyboard/mouse
# event was seen. The jiggler checks this so it never fights an active
# KVM session - it only nudges the mouse during genuine idle time.
_last_real_input = [0.0]


# ---------------------------------------------------------------------------
# Mouse jiggler: keeps the target screen awake with small mouse movements
# when nothing else is happening. Deliberately NOT on a fixed timer or a
# fixed path - every jiggle randomizes the wait, direction, step count and
# per-step timing, and occasionally throws in an extra-long pause, so the
# cadence and shape don't look machine-generated to anything analyzing
# cursor movement (a real hand never moves at a perfectly steady interval
# in a perfectly straight line). Each jiggle nets back to exactly where it
# started, so the cursor never wanders off across the screen over time.
# ---------------------------------------------------------------------------
import random as _jrand
import math as _jmath

JIGGLER_STYLES = {
    "minimal": {
        "label":                "Minimal - stays usable while enabled",
        "interval":             (45, 90),     # seconds between jiggles
        "distance":             (1, 2),       # px per jiggle (round trip)
        "substeps":             (1, 2),
        "substep_delay":        (0.005, 0.02),
        "pause_after_real_input": 3,          # seconds
    },
    "slow": {
        "label":                "Slow & subtle",
        "interval":             (20, 40),
        "distance":             (2, 5),
        "substeps":             (2, 3),
        "substep_delay":        (0.01, 0.04),
        "pause_after_real_input": 5,
    },
    "moderate": {
        "label":                "Moderate",
        "interval":             (8, 20),
        "distance":             (5, 15),
        "substeps":             (2, 4),
        "substep_delay":        (0.01, 0.05),
        "pause_after_real_input": 8,
    },
    "fast": {
        "label":                "Fast & obvious",
        "interval":             (3, 8),
        "distance":             (15, 40),
        "substeps":             (3, 6),
        "substep_delay":        (0.008, 0.03),
        "pause_after_real_input": 10,
    },
}
JIGGLER_DEFAULT_STYLE = "moderate"


class MouseJiggler:
    """Background task that nudges the mouse to keep the remote screen/
    session active. Runs continuously once started; whether it actually
    moves the mouse is gated by .enabled and by recent real input."""

    def __init__(self, mouse_dev: "HIDMouse"):
        self._mouse = mouse_dev
        self.enabled = False
        self.style = JIGGLER_DEFAULT_STYLE
        self._task = None

    def configure(self, enabled: bool, style: str):
        if style not in JIGGLER_STYLES:
            style = JIGGLER_DEFAULT_STYLE
        self.enabled = bool(enabled)
        self.style = style

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "style":   self.style,
            "styles":  {k: v["label"] for k, v in JIGGLER_STYLES.items()},
        }

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        loop = asyncio.get_running_loop()
        while True:
            cfg = JIGGLER_STYLES.get(self.style, JIGGLER_STYLES[JIGGLER_DEFAULT_STYLE])
            wait = _jrand.uniform(*cfg["interval"])
            # ~12% of the time, stretch the wait well beyond the normal
            # range - a real person doesn't touch the mouse at a steady
            # cadence, and a purely periodic signal is the easiest thing
            # for any kind of movement analysis to flag.
            if _jrand.random() < 0.12:
                wait *= _jrand.uniform(1.6, 3.0)
            await asyncio.sleep(wait)

            if not self.enabled:
                continue
            if time.time() - _last_real_input[0] < cfg["pause_after_real_input"]:
                continue  # don't fight an active KVM session

            try:
                await loop.run_in_executor(None, self._jiggle, cfg)
            except Exception:
                log.debug("jiggler move failed", exc_info=True)

    def _jiggle(self, cfg):
        """One full jiggle: move a short, randomly-angled distance away,
        pause briefly like a hand hesitating, then walk back to exactly
        where it started (zero net displacement)."""
        angle = _jrand.uniform(0, 2 * _jmath.pi)
        dist  = _jrand.uniform(*cfg["distance"])
        tx = round(_jmath.cos(angle) * dist)
        ty = round(_jmath.sin(angle) * dist)
        if tx == 0 and ty == 0:
            return
        self._walk(tx, ty, cfg)
        time.sleep(_jrand.uniform(0.05, 0.35))
        self._walk(-tx, -ty, cfg)

    def _walk(self, total_dx: int, total_dy: int, cfg):
        """Splits one leg of the movement into a random number of uneven
        sub-steps with randomized micro-delays between them, instead of
        one abrupt jump - closer to how a hand actually moves. The last
        sub-step always takes whatever remains, so rounding never leaves
        the walk short of its exact target."""
        n = _jrand.randint(*cfg["substeps"])
        rem_x, rem_y = total_dx, total_dy
        for i in range(n):
            last = (i == n - 1)
            if last:
                sx, sy = rem_x, rem_y
            else:
                frac = _jrand.uniform(0.3, 0.7)
                sx = round(rem_x * frac)
                sy = round(rem_y * frac)
            if sx or sy:
                self._mouse.move(sx, sy)
            rem_x -= sx
            rem_y -= sy
            if not last:
                time.sleep(_jrand.uniform(*cfg["substep_delay"]))


jiggler = MouseJiggler(mouse)


# ---------------------------------------------------------------------------
# HID connect-only-during-active-use: optionally unbinds the USB gadget from
# its UDC (the Pi disappears from the target's USB bus entirely) after an
# idle period with no connected KVM session, and rebinds the instant a new
# session connects. Off by default - opt-in.
#
# Why: a HID device that stays enumerated 24/7, including at 3am with nobody
# using it, is itself a fingerprint an always-on presence is more
# noticeable than one that's only there while actually in use.
#
# Interaction with the mouse jiggler: enabling both isn't harmful, just
# pointless while HID is unbound - jiggler's mouse writes fail silently
# (it already tolerates HID errors, see MouseJiggler._loop) until the next
# real session reconnects the gadget. If you rely on jiggler to keep a
# target screen awake between sessions, keep this feature off, or expect
# jiggler to go quiet whenever it's disconnected you.
# ---------------------------------------------------------------------------
class HidAutoDisconnect:
    def __init__(self):
        self.enabled = False
        self.idle_minutes = 15
        self._task = None
        self._udc_name = None

    def configure(self, enabled: bool, idle_minutes):
        self.enabled = bool(enabled)
        try:
            self.idle_minutes = max(1, min(180, int(idle_minutes)))
        except (TypeError, ValueError):
            self.idle_minutes = 15

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "idle_minutes": self.idle_minutes,
            "connected": self._is_bound(),
        }

    def _is_bound(self) -> bool:
        return bool(_usb_r("UDC"))

    def _discover_udc(self) -> str:
        if self._udc_name:
            return self._udc_name
        try:
            entries = sorted(os.listdir("/sys/class/udc"))
            if entries:
                self._udc_name = entries[0]
        except Exception:
            pass
        return self._udc_name or ""

    def ensure_connected(self):
        """Call at the start of a new WS session. Rebinds the gadget if it's
        currently unbound; cheap no-op otherwise. Safe to call from a
        thread-pool executor (blocking file I/O + a brief settle sleep)."""
        if not self.enabled or self._is_bound():
            return
        udc = self._discover_udc()
        if not udc:
            log.warning("hid-autodisconnect: no UDC found, cannot rebind")
            return
        _usb_w("UDC", udc)
        log.info("hid-autodisconnect: rebound gadget to %s (new session)", udc)
        time.sleep(0.35)  # brief settle so /dev/hidg* is ready for the caller

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while True:
            await asyncio.sleep(30)
            if not self.enabled:
                continue
            if _ws_clients:
                continue  # a session is active, never disconnect mid-use
            if not self._is_bound():
                continue  # already disconnected
            idle_for = time.time() - _last_real_input[0]
            if idle_for < self.idle_minutes * 60:
                continue
            try:
                _usb_w("UDC", "")
                log.info("hid-autodisconnect: unbound gadget after %dm idle", self.idle_minutes)
            except Exception:
                log.warning("hid-autodisconnect: unbind failed", exc_info=True)


hid_autodc = HidAutoDisconnect()


# WebSocket: keyboard / mouse input handler


# -- Session / access logging -----------------------------------------------
# ANONYMITY (MAGICBRIDGE_SYSTEM.md §2 "Data at rest"): auth/session logs are
# RAM-only (tmpfs), so connection IPs / User-Agents / timestamps never touch the
# SD card and vanish on power-loss. This log lives in the same tmpfs mount nginx
# and stealth-dashboard.py already use (/var/log/magicbridge-ram), NOT under
# /opt/magicbridge (which is ext4 on the SD card). Writing it to /opt persisted
# weeks of connection history to disk — a real violation pulling the card would
# expose. Wiped on reboot by design; that's the intended privacy behaviour.
import json as _jlog, datetime as _dt_log
_SESS_LOG_DIR = "/var/log/magicbridge-ram"
_SESS_LOG = f"{_SESS_LOG_DIR}/magicbridge-main-sessions.json"
_SESS_LOG_RETENTION_DAYS = 30  # entries older than this are dropped automatically

def _sess_log(sid, ip, ua, event, duration=None):
    try:
        import os as _oss
        _oss.makedirs(_SESS_LOG_DIR, exist_ok=True)
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
        # Time-based purge, in addition to the count cap below. Entries with
        # a missing/unparsable timestamp are kept rather than silently
        # dropped, since a parse failure isn't evidence the entry is stale.
        cutoff = _dt_log.datetime.now() - _dt_log.timedelta(days=_SESS_LOG_RETENTION_DAYS)
        def _fresh(entry):
            try:
                return _dt_log.datetime.fromisoformat(entry.get("time", "")) >= cutoff
            except Exception:
                return True
        data = [e for e in data if _fresh(e)]
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
    _ws_info[ws] = {"ip": ip, "since": t0, "ua": ua}
    log.info("WS connect  from %s  (total: %d)", ip, len(_ws_clients))
    _sess_log(sid, ip, ua, "connect")

    loop = asyncio.get_running_loop()
    if hid_autodc.enabled:
        # Blocking file I/O + a brief settle sleep - runs off the event loop
        # so one reconnecting client doesn't stall every other connection.
        await loop.run_in_executor(None, hid_autodc.ensure_connected)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    d = json.loads(msg.data)
                    t = d.get("type", "")

                    if t in ("keydown", "keyup", "mousemove", "mousemove_abs",
                             "mousedown", "mouseup", "wheel", "scroll",
                             "combo", "paste"):
                        _last_real_input[0] = time.time()

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

                    elif t == "mousemove_abs":
                        # Absolute pointer: x,y are 0..32767 across the screen.
                        # No-op unless the gadget is in absolute mode (mouse
                        # ignores it), so a stale client can't send garbage.
                        mouse.move_abs(int(d.get("x", 0)), int(d.get("y", 0)))

                    elif t == "mousedown":
                        mouse.button_down(int(d.get("button", 0)))

                    elif t == "mouseup":
                        mouse.button_up(int(d.get("button", 0)))

                    elif t in ("wheel", "scroll"):
                        # Frontend sends "scroll"; "wheel" kept for compatibility.
                        # (These were mismatched before, so scrolling did nothing.)
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
        _ws_info.pop(ws, None)
        keyboard.release_all()
        mouse.release_all()
        dur = int(time.time() - t0)
        log.info("WS disconnect %s  (total: %d, dur: %ds)", ip, len(_ws_clients), dur)
        _sess_log(sid, ip, ua, "disconnect", duration=dur)

    return ws


# HTTP handlers

def _decode_throttled(bits: int) -> dict:
    """Decode vcgencmd get_throttled's bitmask into plain states.

    Bit meanings per Raspberry Pi firmware docs:
      0  under-voltage NOW           16  under-voltage has occurred since boot
      1  arm freq capped NOW         17  arm freq capped has occurred
      2  currently throttled        18  throttling has occurred
      3  soft temp limit active NOW 19  soft temp limit has occurred
    """
    uv_now         = bool(bits & (1 << 0))
    cap_now        = bool(bits & (1 << 1))
    throttled_now  = bool(bits & (1 << 2))
    temp_now       = bool(bits & (1 << 3))
    uv_ever        = bool(bits & (1 << 16))
    cap_ever       = bool(bits & (1 << 17))
    throttled_ever = bool(bits & (1 << 18))
    temp_ever      = bool(bits & (1 << 19))

    if uv_now:
        state = "under_voltage"
    elif throttled_now or temp_now:
        state = "throttled"
    elif uv_ever or throttled_ever or temp_ever or cap_ever:
        state = "was_throttled"
    else:
        state = "ok"

    return {
        "state": state,
        "uv_now": uv_now, "uv_ever": uv_ever,
        "throttled_now": throttled_now, "throttled_ever": throttled_ever,
        "temp_limit_now": temp_now, "temp_limit_ever": temp_ever,
        "freq_capped_now": cap_now, "freq_capped_ever": cap_ever,
    }


def _gather_power_health() -> dict:
    """Everything here shells out or hits /proc - always call via
    run_in_executor, never directly from an async handler (same rule as
    the AI Agent's _fetch_json below).

    pmic_read_adc is best-effort: not all Pi 4 firmware/EEPROM versions
    register that vcgencmd command (confirmed missing on the current dev
    unit - it returns 'Command not registered'). get_throttled is the
    reliable source for the actual health signal and is always used;
    pmic_read_adc just adds a numeric volts/amps reading when available.
    """
    import subprocess

    power = {"state": "unknown"}
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
        if "=" in out:
            bits = int(out.split("=", 1)[1].strip(), 16)
            power = _decode_throttled(bits)
    except Exception:
        pass

    volts_in = amps_in = watts_in = None
    try:
        out = subprocess.run(["vcgencmd", "pmic_read_adc"], capture_output=True,
                              text=True, timeout=3).stdout
        v = a = None
        for line in out.splitlines():
            u = line.strip().upper()
            if "5V" in u and "VOLT(" in u:
                try: v = float(line.strip().split("=", 1)[1].rstrip("Vv"))
                except Exception: pass
            elif "5V" in u and "CURRENT(" in u:
                try: a = float(line.strip().split("=", 1)[1].rstrip("Aa"))
                except Exception: pass
        volts_in, amps_in = v, a
        if v is not None and a is not None:
            watts_in = round(v * a, 2)
    except Exception:
        pass

    clock_mhz = None
    try:
        out = subprocess.run(["vcgencmd", "measure_clock", "arm"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
        if "=" in out:
            clock_mhz = round(int(out.split("=", 1)[1]) / 1_000_000)
    except Exception:
        pass

    load1 = load_pct = None
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        ncpu = os.cpu_count() or 4
        load_pct = round(min(load1 / ncpu, 1.0) * 100, 1)
    except Exception:
        pass

    mem_used_pct = mem_used_gb = mem_total_gb = None
    try:
        mem_total_kb = mem_avail_kb = None
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        if mem_total_kb:
            mem_total_gb = round(mem_total_kb / 1_048_576, 2)
            if mem_avail_kb is not None:
                mem_used_pct = round((1 - mem_avail_kb / mem_total_kb) * 100, 1)
                mem_used_gb = round((mem_total_kb - mem_avail_kb) / 1_048_576, 2)
    except Exception:
        pass

    services = {}
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "magicbridge", "stealth-dashboard"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().splitlines()
        for name, state in zip(["magicbridge", "stealth-dashboard"], out):
            services[name] = (state.strip() == "active")
    except Exception:
        pass

    return {
        "power": power,
        "volts_in": volts_in, "amps_in": amps_in, "watts_in": watts_in,
        "clock_mhz": clock_mhz,
        "load1": load1, "load_pct": load_pct,
        "mem_used_pct": mem_used_pct, "mem_used_gb": mem_used_gb, "mem_total_gb": mem_total_gb,
        "services": services,
    }


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

    def _gather_tailscale():
        tailscale_ip = ""
        tailscale_up = False
        try:
            import subprocess as _sp, json as _json_ts
            # Check the actual backend state, not just whether "tailscale ip"
            # returns something. Tailscale can still report a cached IP via
            # "tailscale ip -4" even while the backend is fully stopped, which
            # made this disagree with /api/tailscale's (correct) check.
            _r = _sp.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3)
            if _r.returncode == 0:
                _st = _json_ts.loads(_r.stdout)
                tailscale_up = _st.get("BackendState") == "Running"
                if tailscale_up:
                    _ri = _sp.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
                    if _ri.returncode == 0:
                        tailscale_ip = _ri.stdout.strip().split()[0]
        except Exception:
            pass
        return tailscale_ip, tailscale_up

    loop = asyncio.get_running_loop()
    uptime, temp, local_ip = await loop.run_in_executor(None, _gather)
    health = await loop.run_in_executor(None, _gather_power_health)
    # video.status() shells out to v4l2-ctl per detected device (and, since
    # this session, arecord -l for audio detection) - genuinely blocking,
    # same class of bug as the AI agent's urlopen call fixed earlier. Run it
    # off the event loop like everything else here rather than adding a new
    # blocking call to the one still-synchronous spot in this handler.
    stream_status = await loop.run_in_executor(None, video.status)
    tailscale_ip, tailscale_up = await loop.run_in_executor(None, _gather_tailscale)

    # Who is connected right now (top-bar "viewers" chip). Duration is derived
    # live from the stored connect time. IPs are the controlling user's own
    # devices - this is the admin looking at their own device, not a leak.
    _now = time.time()
    viewers = sorted(
        ({"ip": v.get("ip", "?"), "secs": int(_now - v.get("since", _now)),
          "ua": v.get("ua", "")[:180]}
         for v in list(_ws_info.values())),
        key=lambda x: x["secs"], reverse=True,
    )
    # What the target sees as the attached monitor (EDID identity). Uses the
    # stealth-panel override if set, else the realistic default the base EDID
    # advertises (Dell P2419H). Shown next to the USB identity in the UI.
    try:
        _edid = json.loads(Path(CONFIG_PATH).read_text()).get("edid", {})
    except Exception:
        _edid = {}
    _dtype = (stream_status or {}).get("device_type")
    if _dtype == "usb":
        # A USB HDMI dongle presents its OWN fixed EDID to the target, UPSTREAM
        # of the Pi - we can neither spoof it nor read it back from here. Report
        # that honestly rather than falsely claiming the Dell identity, which
        # only the C790/CSI path (writable TC358743 EDID) can actually enforce.
        display = {
            "name":    "USB capture dongle",
            "mfr":     "",
            "serial":  "",
            "spoofed": False,
            "note":    "USB dongle shows its own fixed EDID to the target - not spoofable. Use the C790/CSI board for the Dell monitor identity.",
        }
    else:
        display = {
            "name":    _edid.get("product_name") or "DELL P2419H",
            "mfr":     _edid.get("mfr") or "DEL",
            "serial":  _edid.get("serial") or "",
            "spoofed": True,
            "note":    "",
        }

    return web.json_response({
        "version":    VERSION,
        "clients":    len(_ws_clients),
        "viewers":    viewers,
        "display":    display,
        "hid_kb":     os.path.exists("/dev/hidg0"),
        "hid_ms":     os.path.exists("/dev/hidg1"),
        "stream":     stream_status,
        "uptime":     uptime,
        "temp_c":     temp,
        "local_ip":     local_ip,
        "tailscale_ip": tailscale_ip,
        "tailscale_up": tailscale_up,
        "power":        health["power"],
        "volts_in":     health["volts_in"],
        "amps_in":      health["amps_in"],
        "watts_in":     health["watts_in"],
        "clock_mhz":    health["clock_mhz"],
        "load1":        health["load1"],
        "load_pct":     health["load_pct"],
        "mem_used_pct": health["mem_used_pct"],
        "mem_used_gb":  health["mem_used_gb"],
        "mem_total_gb": health["mem_total_gb"],
        "services":     health["services"],
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

    stream_status = await loop.run_in_executor(None, video.status)
    return web.json_response({"ok": ok, "status": stream_status})


# OLED status-display settings. Stored in the same shared CONFIG_PATH under
# an "oled" key so it follows the exact same read/merge/write pattern as the
# "video" section above. oled.py (a separate process/service) reads this
# same file directly and hot-reloads on mtime change - no restart needed
# for changes made here to take effect on the physical panel.
OLED_DEFAULTS = {
    "enabled": True,              # master on/off - False blanks the panel and
                                   # stops rendering, without touching the rest
                                   # of the saved layout config
    "line1_mode": "app",          # app | hostname | custom
    "line1_custom": "",
    "line2_mode": "ip",           # ip | tailscale | custom | blank
    "line2_custom": "",
    "line3_show_temp": True,
    "line3_show_uptime": True,
    "line3_show_service": True,
    "line3_show_stream": True,
    "line3_custom_enabled": False,
    "line3_custom": "",
    # 4th line is opt-in and off by default - the panel is only 32px tall and
    # already uses every pixel row for 3 lines at the normal font size, so
    # turning this on makes oled.py switch ALL lines to a smaller font to
    # fit 4 rows (see oled.py FONT_SIZE_SMALL). Not just an extra row "for
    # free" - the UI must warn about this before it's enabled.
    "line4_enabled": False,
    "line4_mode": "blank",        # blank | hostname | tailscale | custom
    "line4_custom": "",
    "refresh_sec": 2,
}


async def api_oled_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        oled_cfg = dict(OLED_DEFAULTS)
        oled_cfg.update(cfg.get("oled", {}))
        return web.json_response({"ok": True, "config": oled_cfg, "defaults": OLED_DEFAULTS})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    # Only accept known keys - anything else is silently dropped rather than
    # stored, so a bad/old client can't wedge unexpected data into config.json.
    allowed = set(OLED_DEFAULTS.keys())
    incoming = {k: v for k, v in d.items() if k in allowed}

    if "refresh_sec" in incoming:
        try:
            incoming["refresh_sec"] = max(1, min(30, int(incoming["refresh_sec"])))
        except Exception:
            incoming.pop("refresh_sec", None)
    for k in ("line1_custom", "line2_custom", "line3_custom", "line4_custom"):
        if k in incoming and incoming[k] is not None:
            incoming[k] = str(incoming[k])[:21]

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        merged = dict(OLED_DEFAULTS)
        merged.update(cfg.get("oled", {}))
        merged.update(incoming)
        cfg["oled"] = merged
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    return web.json_response({"ok": True, "config": merged})


async def api_oled_reset(request: web.Request) -> web.Response:
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        cfg["oled"] = dict(OLED_DEFAULTS)
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not reset: " + str(e)}, status=500)
    return web.json_response({"ok": True, "config": dict(OLED_DEFAULTS)})


# Wake-on-LAN. Stored under a "wol" key in the same shared CONFIG_PATH,
# following the identical read/merge/write pattern as OLED_DEFAULTS above.
# Sending the magic packet needs no saved state on the Pi beyond the MAC -
# it's a fire-and-forget UDP broadcast, not a persistent connection.
WOL_DEFAULTS = {
    "mac": "",
    "broadcast": "255.255.255.255",
    "port": 9,
}


def _wol_normalize_mac(raw: str) -> str:
    """Accepts AA:BB:CC:DD:EE:FF, AA-BB-CC-DD-EE-FF or bare hex; returns
    colon-separated uppercase form, or raises ValueError if not a MAC."""
    cleaned = str(raw).strip().replace("-", ":").replace(".", ":")
    parts = cleaned.split(":") if ":" in cleaned else [cleaned[i:i+2] for i in range(0, len(cleaned), 2)]
    hexonly = "".join(parts)
    if len(hexonly) != 12 or any(c not in "0123456789abcdefABCDEF" for c in hexonly):
        raise ValueError("Not a valid MAC address")
    hexonly = hexonly.upper()
    return ":".join(hexonly[i:i+2] for i in range(0, 12, 2))


def _wol_send_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    import socket as _socket
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()


async def api_wol_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        wol_cfg = dict(WOL_DEFAULTS)
        wol_cfg.update(cfg.get("wol", {}))
        return web.json_response({"ok": True, "config": wol_cfg, "defaults": WOL_DEFAULTS})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    incoming = {}
    if "mac" in d:
        raw = str(d.get("mac") or "").strip()
        if raw == "":
            incoming["mac"] = ""
        else:
            try:
                incoming["mac"] = _wol_normalize_mac(raw)
            except ValueError:
                return web.json_response({"ok": False, "error": "Invalid MAC address format"}, status=400)
    if "broadcast" in d:
        incoming["broadcast"] = str(d.get("broadcast") or WOL_DEFAULTS["broadcast"])[:64]
    if "port" in d:
        try:
            incoming["port"] = max(1, min(65535, int(d["port"])))
        except Exception:
            pass

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        merged = dict(WOL_DEFAULTS)
        merged.update(cfg.get("wol", {}))
        merged.update(incoming)
        cfg["wol"] = merged
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    return web.json_response({"ok": True, "config": merged})


async def api_wol_wake(request: web.Request) -> web.Response:
    """POST /api/wol/wake: send the magic packet to the saved MAC."""
    cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    wol_cfg = dict(WOL_DEFAULTS)
    wol_cfg.update(cfg.get("wol", {}))
    mac = wol_cfg.get("mac", "")
    if not mac:
        return web.json_response({"ok": False, "error": "No MAC address saved yet"}, status=400)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _wol_send_packet, mac, wol_cfg.get("broadcast", "255.255.255.255"), wol_cfg.get("port", 9))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not send packet: " + str(e)}, status=500)
    return web.json_response({"ok": True, "mac": mac})


def _write_wol_cron(schedules) -> None:
    """Rewrite /etc/cron.d/mb-wol from the saved schedules. Each is
    {time:'HH:MM', days:'*'|'1-5'|'0,6', enabled}. No schedules -> remove the
    file. cron.d entries need the user field (root) and each line a newline."""
    path = "/etc/cron.d/mb-wol"
    body = ["# MagicBridge scheduled Wake-on-LAN (managed by the web UI)\n",
            "SHELL=/bin/sh\nPATH=/usr/bin:/bin\n"]
    n = 0
    for s in (schedules or []):
        if not isinstance(s, dict) or not s.get("enabled", True):
            continue
        t = str(s.get("time", "")).strip()
        if ":" not in t:
            continue
        try:
            hh, mm = (int(x) for x in t.split(":", 1))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            continue
        days = str(s.get("days", "*")).strip() or "*"
        if any(c not in "0123456789,-*" for c in days):   # cron day field only
            days = "*"
        body.append(f"{mm} {hh} * * {days} root /usr/bin/python3 "
                    f"/opt/magicbridge/core/magicbridge.py --send-wol >/dev/null 2>&1\n")
        n += 1
    try:
        if n == 0:
            if os.path.exists(path):
                os.remove(path)
            return
        Path(path).write_text("".join(body))
        os.chmod(path, 0o644)
    except Exception as e:
        log.warning("could not write WoL cron: %s", e)


async def api_wol_schedule(request: web.Request) -> web.Response:
    """GET/POST /api/wol/schedule: manage recurring Wake-on-LAN times (cron)."""
    cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    wol = cfg.get("wol", {})
    if request.method == "GET":
        return web.json_response({"ok": True, "schedules": wol.get("schedules", []),
                                  "mac": wol.get("mac", "")})
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    scheds = d.get("schedules", [])
    if not isinstance(scheds, list) or len(scheds) > 20:
        return web.json_response({"ok": False, "error": "invalid schedules"}, status=400)
    wol["schedules"] = scheds
    cfg["wol"] = wol
    try:
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_wol_cron, scheds)
    return web.json_response({"ok": True, "schedules": scheds})


# Target keyboard layout. See hid.py's CHAR_MAPS comment for the underlying
# reason this exists: HID usage codes are physical-position, not character,
# so the paste/AI-typed-text feature needs to know what layout the TARGET OS
# is set to or it types the wrong characters. Only "us" is a verified table
# right now (see hid.get_layout_names()) - the setting exists so this is a
# one-line config change later, not a code change, once another layout is
# actually verified and added to CHAR_MAPS.
KEYBOARD_DEFAULTS = {"layout": "us"}


async def api_keyboard_settings(request: web.Request) -> web.Response:
    if request.method == "GET":
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        kb_cfg = dict(KEYBOARD_DEFAULTS)
        kb_cfg.update({k: v for k, v in cfg.get("keyboard", {}).items() if k in KEYBOARD_DEFAULTS})
        return web.json_response({"ok": True, "config": kb_cfg, "available_layouts": hid.get_layout_names()})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    layout = str(d.get("layout", "")).strip()
    if layout and layout not in hid.get_layout_names():
        return web.json_response({"ok": False, "error": f"Unknown layout: {layout}"}, status=400)

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        kb_cfg = cfg.setdefault("keyboard", {})
        if layout:
            kb_cfg["layout"] = layout
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    if layout:
        hid.set_layout(layout)

    return web.json_response({"ok": True, "config": dict(KEYBOARD_DEFAULTS, **kb_cfg)})


# App factory



async def api_power(request):
    """Reboot or shutdown the Pi."""
    import subprocess
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    action = str(d.get("action", ""))
    cmd = {"shutdown": ["sudo", "shutdown", "-h", "now"],
           "reboot":   ["sudo", "reboot"]}.get(action)
    if not cmd:
        return web.json_response({"ok": False, "error": "unknown action"}, status=400)
    # Refuse to halt/reboot while install.sh is still running. Learned the hard
    # way: a shutdown landed mid-upgrade, which can leave dpkg half-configured
    # and the unit half-updated - and the OLED froze on "Upgrading" (a halted Pi
    # keeps the panel powered but stops driving it), which looks like a hang and
    # invites a power pull on top of it. Overridable with force:true so a wedged
    # upgrade can never permanently trap the unit.
    if _upd_running() and not bool(d.get("force")):
        return web.json_response({
            "ok": False, "busy": "update",
            "error": "An upgrade is still running - halting now can corrupt it. "
                     "Wait for it to finish, or resend with force to override.",
        }, status=409)
    # Popen fire-and-forget reported ok:True even when the command failed, so a
    # broken sudo looked exactly like a successful shutdown. systemd returns
    # from `shutdown`/`reboot` as soon as logind accepts the request, so a real
    # run() is cheap - and if the box goes down first the client just loses the
    # connection, which the UI already treats as the expected success path.
    log.info("power: %s requested", action)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": True, "action": action})   # going down
    except Exception as e:
        log.error("power: %s failed to launch: %s", action, e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:200] or f"rc={p.returncode}"
        log.error("power: %s FAILED: %s", action, err)
        return web.json_response({"ok": False, "error": err}, status=500)
    return web.json_response({"ok": True, "action": action})



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


LOCKDOWN_SH = "/usr/local/bin/mb-lockdown.sh"

async def api_network_lockdown(request):
    """GET/POST /api/network/lockdown: Tailscale-only access toggle.

    When enabled, ports 80/443 (the whole web UI and API surface behind
    nginx) only accept connections arriving via the tailscale0 interface.
    SSH is never touched by the underlying script, and this handler refuses
    to enable lockdown unless Tailscale is confirmed connected first, so
    there's no path to locking yourself out of the device entirely.
    Defaults to off; a fresh install stays LAN-reachable until you turn
    this on deliberately from the System tab.
    """
    import subprocess
    if request.method == "GET":
        try:
            r = subprocess.run([LOCKDOWN_SH, "status"], capture_output=True, text=True, timeout=5)
            state = r.stdout.strip()
        except Exception:
            state = "off"
        return web.json_response({"ok": True, "tailscale_only": state == "on"})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    enable = bool(d.get("enable", False))

    if enable:
        # Never allow enabling this unless Tailscale is actually connected,
        # this exact check is what prevents the lockout scenario.
        try:
            r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
            st = json.loads(r.stdout)
            if st.get("BackendState") != "Running":
                return web.json_response({
                    "ok": False,
                    "error": "Tailscale isn't connected yet. Connect it first (Network tab), "
                             "then enable Tailscale-only access, otherwise you'd lock yourself out."
                }, status=400)
        except Exception:
            return web.json_response({
                "ok": False,
                "error": "Could not confirm Tailscale is connected. Connect it first before enabling this."
            }, status=400)

    action = "on" if enable else "off"
    try:
        r = subprocess.run([LOCKDOWN_SH, action], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
        out = (r.stdout + r.stderr)[:300]
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        cfg.setdefault("network", {})["tailscale_only"] = enable
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass

    log.info("Network lockdown %s", "enabled" if enable else "disabled")
    return web.json_response({"ok": ok, "tailscale_only": enable, "out": out})


import secrets as _sec

_USB_DIR = "/sys/kernel/config/usb_gadget/g1"
# These are real wireless keyboard+mouse combo receiver dongles, chosen
# deliberately over single-purpose keyboard models: the gadget always
# exposes both a keyboard AND a mouse HID interface under one identity, and
# a keyboard-only product (e.g. a plain K120) never legitimately has a
# mouse interface, an easy structural tell. A combo receiver dongle is
# supposed to present exactly this way, so it isn't a giveaway.
# has_serial/extra_iface: only the Logitech entry is verified against a
# real device's descriptor (iSerial=0, 3 interfaces). Microsoft/Dell are
# left with a serial and the plain 2-interface layout since that couldn't
# be verified - better to leave them unchanged than guess.
_USB_PROFILES = [
    {"name":"Logitech Unifying Receiver", "mfr":"Logitech",  "prod":"USB Receiver",               "vid":"0x046d","pid":"0xc52b","pfx":"LGK","has_serial":False,"extra_iface":True},
    {"name":"Microsoft Dual Receiver",    "mfr":"Microsoft", "prod":"Microsoft USB Dual Receiver", "vid":"0x045e","pid":"0x0800","pfx":"MSK","has_serial":True, "extra_iface":False},
    {"name":"Dell Wireless Combo",        "mfr":"Dell",      "prod":"Dell Wireless Keyboard and Mouse Combo", "vid":"0x413c","pid":"0x2513","pfx":"DEL","has_serial":True, "extra_iface":False},
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
    else:
        return "".join(rng.choices("0123456789ABCDEF", k=12))

async def api_identity(request):
    import time as _t
    if request.method == "GET":
        return web.json_response({
            "product":      _usb_r("strings/0x409/product")      or "USB Receiver",
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
        ser = _gen_serial(idx) if p.get("has_serial", True) else ""
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
                                           "serial": ser, "idVendor": p["vid"], "idProduct": p["pid"],
                                           "has_serial": p.get("has_serial", True),
                                           "extra_iface": p.get("extra_iface", False)})
        _save_cfg(cfg)
        return web.json_response({"ok": True, "product": p["prod"], "serial": ser or "—"})
    return web.json_response({"ok": False, "error": "unknown action"}, status=400)


async def api_jiggler(request):
    """GET /api/jiggler: current enabled/style + available styles.
    POST /api/jiggler: {"enabled": bool, "style": str} - updates live and persists."""
    if request.method == "GET":
        return web.json_response({"ok": True, **jiggler.status()})
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    style = str(d.get("style", jiggler.style))
    if style not in JIGGLER_STYLES:
        return web.json_response({"ok": False, "error": "unknown style"}, status=400)
    enabled = bool(d.get("enabled", jiggler.enabled))
    jiggler.configure(enabled, style)
    cfg = _load_cfg()
    cfg["jiggler"] = {"enabled": enabled, "style": style}
    _save_cfg(cfg)
    return web.json_response({"ok": True, **jiggler.status()})


async def api_hid_autodisconnect(request):
    """GET /api/hid-autodisconnect: current enabled/idle_minutes/connected state.
    POST /api/hid-autodisconnect: {"enabled": bool, "idle_minutes": int} -
    updates live and persists."""
    if request.method == "GET":
        return web.json_response({"ok": True, **hid_autodc.status()})
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    enabled = bool(d.get("enabled", hid_autodc.enabled))
    idle_minutes = d.get("idle_minutes", hid_autodc.idle_minutes)
    hid_autodc.configure(enabled, idle_minutes)
    cfg = _load_cfg()
    cfg["hid_autodisconnect"] = {"enabled": enabled, "idle_minutes": hid_autodc.idle_minutes}
    _save_cfg(cfg)
    return web.json_response({"ok": True, **hid_autodc.status()})


_GADGET_SH = "/usr/local/bin/mb-gadget.sh"

async def api_mouse_mode(request):
    """GET: current USB mouse mode. POST {"mode": "relative"|"absolute"}:
    persist it, rebuild the USB gadget so the mouse HID descriptor matches, and
    switch the live HIDMouse report format. Relative is the default and the
    stealthiest (a real receiver mouse is relative); absolute is opt-in (see the
    anonymity note in mb-gadget.sh)."""
    import subprocess as _sp
    if request.method == "GET":
        cfg = _load_cfg()
        mode = cfg.get("usb", {}).get("mouse_mode", "relative")
        return web.json_response({"ok": True, "mode": mode})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    mode = str(d.get("mode", "")).strip().lower()
    if mode not in ("relative", "absolute"):
        return web.json_response({"ok": False, "error": "mode must be 'relative' or 'absolute'"}, status=400)

    cfg = _load_cfg()
    cfg.setdefault("usb", {})["mouse_mode"] = mode
    _save_cfg(cfg)

    # Rebuild the gadget with the matching mouse descriptor. --rebuild forces a
    # teardown even though the gadget is currently bound. Runs off the event
    # loop (it unbinds/rebinds the UDC and sleeps briefly).
    loop = asyncio.get_running_loop()
    def _rebuild():
        return _sp.run([_GADGET_SH, "--rebuild"], capture_output=True, text=True, timeout=25)
    rebuilt = False
    try:
        r = await loop.run_in_executor(None, _rebuild)
        rebuilt = (r.returncode == 0)
        if not rebuilt:
            log.warning("mouse-mode gadget rebuild failed: %s", (r.stdout + r.stderr)[-300:])
    except Exception as e:
        log.warning("mouse-mode gadget rebuild error: %s", e)

    # Align the live report format with the (now rebuilt) descriptor.
    mouse.set_absolute(mode == "absolute")
    log.info("Mouse mode set to %s (gadget rebuilt=%s)", mode, rebuilt)

    return web.json_response({
        "ok": rebuilt, "mode": mode, "rebuilt": rebuilt,
        "error": None if rebuilt else "Gadget rebuild failed — mode saved, will apply on next boot",
    })


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







# --- Update sizing: incremental (fast, copy changed runtime files) vs full ----
_UPD_INSTALL_DIR = "/opt/magicbridge"
# repo-relative runtime file -> (live destination, service to restart | None |
# "nginx-reload"). A change limited to these (or docs/static/build tooling) is an
# Stable name for the detached full-upgrade unit, so "is an upgrade running?"
# is answerable by anything that needs to know (api_power refuses to halt).
_UPD_UNIT = "mb-selfupdate"

# What is actually DEPLOYED, which is not the same as what the repo clone is at.
# `git pull` runs BEFORE install.sh, so an installer that fails or is interrupted
# leaves the clone advanced and nothing deployed - and comparing HEAD to origin
# then reports "Up to date" forever, with no way to retry from the UI. Seen for
# real: a shutdown landed mid-install.sh, the clone sat at the new commit, and
# the running code had none of it. install.sh stamps this file as its LAST step
# (success only); the incremental path stamps it after copying. Compare THIS to
# origin, never HEAD.
_DEPLOYED_FILE = "/etc/magicbridge/.deployed-commit"

def _deployed_commit():
    try:
        with open(_DEPLOYED_FILE) as fh:
            return fh.read().strip()
    except Exception:
        return ""

def _set_deployed(sha):
    try:
        os.makedirs(os.path.dirname(_DEPLOYED_FILE), exist_ok=True)
        tmp = _DEPLOYED_FILE + ".new"
        with open(tmp, "w") as fh:
            fh.write((sha or "").strip() + "\n")
        os.replace(tmp, _DEPLOYED_FILE)
    except Exception as e:
        log.error("could not stamp deployed commit: %s", e)

def _upd_running():
    """True while a full upgrade (install.sh) is still in flight."""
    try:
        import subprocess as _s
        return _s.run(["systemctl", "is-active", "--quiet", _UPD_UNIT],
                      timeout=5).returncode == 0
    except Exception:
        return False   # never let a broken probe block a legitimate shutdown

# INCREMENTAL update; anything structural forces a FULL install.sh run.
_UPD_FILE_MAP = {
    "src/core/magicbridge.py":            (_UPD_INSTALL_DIR + "/core/magicbridge.py", "magicbridge"),
    "src/core/hid.py":                    (_UPD_INSTALL_DIR + "/core/hid.py", "magicbridge"),
    "src/core/video.py":                  (_UPD_INSTALL_DIR + "/core/video.py", "magicbridge"),
    "src/core/oled.py":                   (_UPD_INSTALL_DIR + "/core/oled.py", "mb-oled"),
    "src/web/index.html":                 (_UPD_INSTALL_DIR + "/web/index.html", None),
    "src/dashboard/stealth-dashboard.py": (_UPD_INSTALL_DIR + "/dashboard/stealth-dashboard.py", "stealth-dashboard"),
    "src/provision/mb-setup-ui.py":       (_UPD_INSTALL_DIR + "/provision/mb-setup-ui.py", None),
    "src/core/mb-gadget.sh":              ("/usr/local/bin/mb-gadget.sh", None),
    "src/core/mb-hdmi-init.sh":           ("/usr/local/bin/mb-hdmi-init.sh", None),
    "src/core/mb-mdns-alias.sh":          ("/usr/local/bin/mb-mdns-alias.sh", None),
    "src/core/mb-lockdown.sh":            ("/usr/local/bin/mb-lockdown.sh", None),
    "src/core/mb-firstboot.sh":           ("/usr/local/bin/mb-firstboot.sh", None),
    "src/core/mb-secret-reset.sh":        ("/usr/local/bin/mb-secret-reset.sh", None),
    "src/core/mb-power-test.sh":          ("/usr/local/bin/mb-power-test.sh", None),
    "src/provision/mb-provision.sh":      ("/usr/local/bin/mb-provision.sh", None),
    "src/nginx/magicbridge.conf":         ("/etc/nginx/sites-available/magicbridge", "nginx-reload"),
    "src/edid/mb-edid-1080p50.hex":       (_UPD_INSTALL_DIR + "/edid/mb-edid-1080p50.hex", None),
}
# Non-runtime paths: changing only these needs no deploy (docs, build-host tools).
# NOTE .ps1 is handled separately below - those are Windows helper scripts that
# run on the OPERATOR'S laptop and never touch the Pi, so letting one fall
# through to the "unknown file" branch would force a pointless full reinstall
# here (mb-rescue.ps1 did exactly that).
_UPD_IGNORE_PREFIXES = ("docs/", "src/provision/build-image.sh")
_UPD_IGNORE_FILES    = ("README.md", ".gitignore", "LICENSE", "NOTICE", "CLAUDE.md",
                        "MAGICBRIDGE_HANDBOOK.md")

def _upd_classify(changed):
    """'full' or 'incremental' for a list of repo-relative changed paths.
    Structural changes (install.sh, any *.service, the Janus installer, or a
    runtime file we don't know how to place) force a full install.sh run."""
    for f in changed:
        if f == "install.sh" or f.endswith(".service") or f == "src/install_janus_webrtc.sh":
            return "full"
        if f in _UPD_FILE_MAP or f.startswith("src/web/static/"):
            continue
        if f.startswith(_UPD_IGNORE_PREFIXES) or f in _UPD_IGNORE_FILES:
            continue
        if f.endswith(".ps1"):
            continue    # Windows-host helper; nothing to deploy on the Pi
        return "full"   # unknown runtime file - let the installer handle it
    return "incremental"

def _upd_incremental(changed, repo_dir):
    """Copy just the changed runtime files to their live paths. Returns
    (copied, services_to_restart, reload_nginx)."""
    import shutil as _sh
    copied, restarts, reload_nginx = [], set(), False
    for f in changed:
        if f in _UPD_FILE_MAP:
            dst, svc = _UPD_FILE_MAP[f]
            src = os.path.join(repo_dir, f)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _sh.copyfile(src, dst)
                if dst.startswith("/usr/local/bin"):
                    os.chmod(dst, 0o755)
                copied.append(f)
                if svc == "nginx-reload": reload_nginx = True
                elif svc: restarts.add(svc)
        elif f.startswith("src/web/static/"):
            s = os.path.join(repo_dir, "src/web/static")
            if os.path.isdir(s):
                _sh.copytree(s, _UPD_INSTALL_DIR + "/web/static", dirs_exist_ok=True)
                if "static/" not in copied: copied.append("static/")
    return copied, restarts, reload_nginx


async def api_update(request):
    """POST /api/update: pull from GitHub and redeploy.

    Picks the smallest safe update: an INCREMENTAL copy of only the changed
    runtime files (+ restart just the affected service) when nothing structural
    changed, or a FULL idempotent install.sh run when it did (deps, config.txt
    overlays, services, new files). action="update" auto-picks; pass
    {"mode":"full"} to force a reinstall.
    """
    import subprocess as _sp
    import shutil as _shutil

    REPO_DIR = "/opt/magicbridge-repo"
    # Canonical repo URL. Was "razzrohith/MagicBridge.git" (the pre-2026-07-17
    # name) which only still works via GitHub's rename-redirect — a real risk:
    # the moment anyone claims the freed-up old name, this updater would clone
    # THEIR code and copy+restart it (supply-chain RCE). Pin the current name.
    REPO_URL = "https://github.com/razzrohith/magicbridge-diy.git"
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
        # Existing clone: self-heal its origin to the canonical URL, in case it
        # was first cloned from the old (redirected) repo name. Idempotent.
        _sp.run(["git", "-C", REPO_DIR, "remote", "set-url", "origin", REPO_URL],
                capture_output=True, text=True, timeout=10)
        return True, ""

    cloned_ok, clone_out = _ensure_clone()
    if not cloned_ok:
        return web.json_response({"ok": False, "error": "clone failed: " + clone_out})

    if action == "status":
        ver = _sp.run(["git", "-C", REPO_DIR, "log", "--oneline", "-1"],
                     capture_output=True, text=True, timeout=10)
        branch = _sp.run(["git", "-C", REPO_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=10)
        # A real (not --dry-run) fetch, so origin/<branch> actually reflects
        # what's on GitHub right now, then compare HEAD against it directly -
        # gives an unambiguous yes/no instead of parsing git's raw fetch text.
        _sp.run(["git", "-C", REPO_DIR, "fetch", "--quiet"],
               capture_output=True, text=True, timeout=20)
        remote_hash = _sp.run(["git", "-C", REPO_DIR, "rev-parse", f"origin/{BRANCH}"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        # Baseline is what is DEPLOYED, not what the clone is at (see
        # _DEPLOYED_FILE). Fall back to HEAD only if the stamp is missing or
        # names a commit this clone doesn't have (e.g. after a force-push).
        local_hash = _deployed_commit()
        deploy_unknown = False
        if not local_hash or _sp.run(["git", "-C", REPO_DIR, "cat-file", "-e", local_hash + "^{commit}"],
                                     capture_output=True, timeout=10).returncode != 0:
            deploy_unknown = True
            local_hash = _sp.run(["git", "-C", REPO_DIR, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        behind = _sp.run(["git", "-C", REPO_DIR, "rev-list", "--count", f"{local_hash}..origin/{BRANCH}"],
                        capture_output=True, text=True, timeout=10)
        try:
            commits_behind = int(behind.stdout.strip() or 0)
        except ValueError:
            commits_behind = 0
        update_available = bool(local_hash and remote_hash
                                 and local_hash != remote_hash and commits_behind > 0)
        changed = []
        if update_available:
            _diff = _sp.run(["git", "-C", REPO_DIR, "diff", "--name-only",
                             f"{local_hash}..origin/{BRANCH}"], capture_output=True, text=True, timeout=10)
            changed = [x for x in _diff.stdout.split("\n") if x.strip()]
        mode = _upd_classify(changed) if update_available else "none"
        # Clone already at origin but nothing recorded as deployed = a previous
        # install died after the pull. "Up to date" would be a lie and would
        # leave no way to retry, so offer the reinstall that actually fixes it.
        if deploy_unknown and not update_available:
            update_available, mode = True, "full"
            out = "Deployment unverified - reinstall to be sure the code is live"
        else:
            out = ("Up to date" if not update_available else
                   (f"Quick update: {commits_behind} commit(s), {len(changed)} file(s)"
                    if mode == "incremental" else
                    f"Full upgrade: {commits_behind} commit(s) (structural changes)"))
        return web.json_response({
            "ok": True,
            "version": ver.stdout.strip(),
            "branch": branch.stdout.strip(),
            "update_available": update_available,
            "commits_behind": commits_behind,
            "mode": mode,                 # "incremental" | "full" | "none"
            "changed": len(changed),
            "deploy_unknown": deploy_unknown,
            "out": out,
        })

    # action == "update"
    # Fetch so origin reflects GitHub, see what changed, and size the update.
    _sp.run(["git", "-C", REPO_DIR, "fetch", "--quiet"], capture_output=True, text=True, timeout=25)
    # Diff from what is DEPLOYED, not from HEAD - otherwise an install that died
    # after the pull looks like "Already up to date" and can never be retried.
    _base = _deployed_commit()
    _deploy_unknown = not _base or _sp.run(
        ["git", "-C", REPO_DIR, "cat-file", "-e", _base + "^{commit}"],
        capture_output=True, timeout=10).returncode != 0
    if _deploy_unknown:
        _base = _sp.run(["git", "-C", REPO_DIR, "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=10).stdout.strip()
    _diff = _sp.run(["git", "-C", REPO_DIR, "diff", "--name-only", f"{_base}..origin/{BRANCH}"],
                    capture_output=True, text=True, timeout=10)
    changed = [x for x in _diff.stdout.split("\n") if x.strip()]
    if not changed and not _deploy_unknown:
        return web.json_response({"ok": True, "out": "Already up to date", "restarted": False})

    forced = str(d.get("mode", "")).strip().lower()
    # Nothing recorded as deployed -> we cannot know what is live, so reinstall.
    mode = (forced if forced in ("full", "incremental")
            else ("full" if _deploy_unknown else _upd_classify(changed)))

    # Pull to latest (the source tree for both paths).
    r = _sp.run(["git", "-C", REPO_DIR, "pull", "--ff-only"], capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return web.json_response({"ok": False, "out": out, "restarted": False})

    if mode == "full":
        # Structural change: run the idempotent installer DETACHED (own transient
        # unit) so it applies deps/config.txt/services/new files and survives the
        # magicbridge restart it triggers. OLED shows progress; log -> update.log.
        upd_log = "/var/log/magicbridge-update.log"
        _full_sh = (
            "mkdir -p /run/magicbridge; "
            "printf '@UPDATING Upgrading' > /run/magicbridge/oled-status; "   # animated on the OLED
            f"bash {REPO_DIR}/install.sh >{upd_log} 2>&1; RC=$?; "
            "if [ $RC -eq 0 ]; then printf 'MagicBridge\\nUpdated!\\nrestarting...' > /run/magicbridge/oled-status; "
            "else printf 'MagicBridge\\nUpdate FAILED\\nsee log' > /run/magicbridge/oled-status; fi; "
            "sleep 4; rm -f /run/magicbridge/oled-status"
        )
        try:
            # Named unit (not an anonymous run-*.service) so OTHER code can ask
            # "is an upgrade in flight?" - api_power refuses to halt the Pi while
            # this is active. Halting mid-install.sh can leave dpkg half-configured
            # and the unit half-updated. --collect reaps it even if it failed, so
            # the fixed name never blocks a later retry.
            r2 = _sp.run(["systemd-run", "--collect", "--unit", _UPD_UNIT,
                          "--description", "MagicBridge self-update",
                          "/bin/bash", "-c", _full_sh], capture_output=True, text=True, timeout=20)
            if r2.returncode != 0:
                return web.json_response({"ok": False, "restarted": False,
                    "out": out + "\ncould not launch installer: " + (r2.stdout + r2.stderr)[-300:]})
        except Exception as e:
            return web.json_response({"ok": False, "restarted": False,
                "out": out + "\ninstaller launch error: " + str(e)})
        return web.json_response({
            "ok": True, "mode": "full", "out": out, "restarted": True,
            "note": ("Full upgrade: reconciling code + structure in the background; "
                     "services restart, reconnect in ~1-2 min. Log: " + upd_log),
        })

    # INCREMENTAL: copy only the changed runtime files, restart only what's
    # affected. Copies run here (fast); restarts run detached (with OLED) so a
    # magicbridge restart doesn't kill this response mid-flight.
    try:
        copied, restarts, reload_nginx = _upd_incremental(changed, REPO_DIR)
    except Exception as e:
        return web.json_response({"ok": False, "restarted": False,
            "out": out + "\nincremental copy failed: " + str(e)})
    # Copies succeeded -> this commit really is deployed. Stamp it, or the next
    # status check falls back to HEAD and the "unverified" prompt never clears.
    _set_deployed(_sp.run(["git", "-C", REPO_DIR, "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10).stdout.strip())
    cmds = []
    if reload_nginx: cmds.append("nginx -t && systemctl reload nginx")
    for svc in ("stealth-dashboard", "mb-oled", "magicbridge"):
        if svc in restarts: cmds.append("systemctl restart " + svc)
    _incr_sh = ("mkdir -p /run/magicbridge; "
                "printf '@UPDATING Quick update' > /run/magicbridge/oled-status; "  # animated on the OLED
                "sleep 3; "                       # let the animation play even on a no-restart change
                + ("; ".join(cmds) + "; " if cmds else "")
                + "printf 'MagicBridge\\nUpdated!\\n:)' > /run/magicbridge/oled-status; "
                "sleep 2; rm -f /run/magicbridge/oled-status")
    try:
        _sp.run(["systemd-run", "--collect", "--description", "MagicBridge quick-update",
                 "/bin/bash", "-c", _incr_sh], capture_output=True, text=True, timeout=15)
    except Exception as e:
        return web.json_response({"ok": False, "restarted": False,
            "out": out + "\nrestart launch error: " + str(e)})
    _svcs = sorted(restarts) + (["nginx"] if reload_nginx else [])
    return web.json_response({
        "ok": True, "mode": "incremental", "out": out, "restarted": bool(cmds),
        "copied": copied,
        "note": ("Quick update - " + (", ".join(copied) if copied else "docs only, nothing to deploy")
                 + (". Restarting: " + ", ".join(_svcs) if _svcs else "")
                 + (". Reconnect in a few seconds." if "magicbridge" in restarts else ".")),
    })


async def api_stealth_logs(request):
    """GET /api/stealth/logs: access log (stealth dashboard only)."""
    try:
        data = _jlog.loads(open(_SESS_LOG).read())
    except Exception:
        data = []
    return web.json_response({"ok": True, "sessions": list(reversed(data[-200:]))})


# AI settings: provider/model/cloud-enabled prefs + per-provider API keys,
# stored server-side in the shared CONFIG_PATH under an "ai" key. Keys are
# write-only from the browser's perspective - GET only ever returns a
# per-provider boolean (keys_set), never the raw value - closing the old gap
# where every provider's key sat in localStorage (readable by any same-
# origin XSS, or by anyone who pulls the SD card and reads the browser
# profile). The key itself is still plaintext in config.json for now; full
# at-rest encryption of config.json is a separate, larger piece of work
# (see the data-at-rest phase) and deliberately not duplicated here.
AI_DEFAULTS = {
    "provider": "openrouter",
    "model": "",
    "cloud_enabled": True,   # explicit opt-out gate - off disables all cloud AI calls
}

# Product-level kill switch for the whole AI Agent feature, independent of
# the user-facing "cloud_enabled" preference above. Set to True to bring the
# feature back - this is the single flag that controls it everywhere
# (frontend Agent tab is hidden via a matching constant in index.html, and
# every /api/ai/* route below 403s while this is False, so the feature is
# unreachable even by calling the API directly, not just hidden in the UI).
AGENT_FEATURE_ENABLED = False


async def api_ai_settings(request: web.Request) -> web.Response:
    if not AGENT_FEATURE_ENABLED:
        return web.json_response({"ok": False, "error": "AI Agent is disabled in this build."}, status=403)
    if request.method == "GET":
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        ai_cfg = dict(AI_DEFAULTS)
        ai_cfg.update({k: v for k, v in cfg.get("ai", {}).items() if k in AI_DEFAULTS})
        keys = cfg.get("ai", {}).get("keys", {})
        keys_set = {p: bool(keys.get(p)) for p in ("openrouter", "claude", "openai", "gemini")}
        return web.json_response({"ok": True, "config": ai_cfg, "keys_set": keys_set})

    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    incoming = {k: v for k, v in d.items() if k in AI_DEFAULTS}
    if "cloud_enabled" in incoming:
        incoming["cloud_enabled"] = bool(incoming["cloud_enabled"])

    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        ai_cfg = cfg.setdefault("ai", {})
        merged = dict(AI_DEFAULTS)
        merged.update({k: v for k, v in ai_cfg.items() if k in AI_DEFAULTS})
        merged.update(incoming)
        ai_cfg.update(merged)
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)

    keys = cfg.get("ai", {}).get("keys", {})
    keys_set = {p: bool(keys.get(p)) for p in ("openrouter", "claude", "openai", "gemini")}
    return web.json_response({"ok": True, "config": merged, "keys_set": keys_set})


async def api_ai_key(request: web.Request) -> web.Response:
    """POST /api/ai/key: set (or clear, if key is empty) one provider's API
    key. Write-only - there is no GET that returns the raw value."""
    if not AGENT_FEATURE_ENABLED:
        return web.json_response({"ok": False, "error": "AI Agent is disabled in this build."}, status=403)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    provider = str(d.get("provider", "")).strip()
    key = str(d.get("key", "")).strip()
    if provider not in ("openrouter", "claude", "openai", "gemini"):
        return web.json_response({"ok": False, "error": "Unknown provider"}, status=400)
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
        ai_cfg = cfg.setdefault("ai", {})
        keys = ai_cfg.setdefault("keys", {})
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return web.json_response({"ok": False, "error": "Could not save: " + str(e)}, status=500)
    return web.json_response({"ok": True, "has_key": bool(key)})


async def api_ai_run(request):
    """POST /api/ai/run: proxy a natural-language command into a KVM action sequence."""
    if not AGENT_FEATURE_ENABLED:
        return web.json_response({"ok": False, "error": "AI Agent is disabled in this build."}, status=403)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    cfg = json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}
    ai_cfg = dict(AI_DEFAULTS)
    ai_cfg.update({k: v for k, v in cfg.get("ai", {}).items() if k in AI_DEFAULTS})
    if not ai_cfg.get("cloud_enabled", True):
        return web.json_response({"ok": False, "error": "Cloud AI is turned off in settings."})

    provider = d.get("provider", ai_cfg.get("provider", "openrouter"))
    model    = d.get("model", "").strip()
    command  = d.get("command", "").strip()
    # The key is looked up server-side from config.json - the browser no
    # longer sends it on every request (see AI_DEFAULTS comment above).
    api_key  = cfg.get("ai", {}).get("keys", {}).get(provider, "").strip()
    if not api_key:
        return web.json_response({"ok": False, "error": f"No API key saved for {provider} - add one in AI Agent settings."})
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

    def _fetch_json(req):
        # Runs in a thread executor (see run_in_executor calls below), NOT
        # directly on the event loop. urllib.request.urlopen is a blocking
        # call; the AI providers this hits can take several seconds (up to
        # the 30s timeout) to respond, and aiohttp is single-threaded, so
        # calling this straight from the coroutine used to freeze the WHOLE
        # server - every other connected client's keyboard/mouse input over
        # /ws, video status polling, everything - for the entire duration of
        # one person's AI request. Moving just this blocking part off the
        # event loop fixes that without restructuring the rest of the
        # per-provider logic below.
        with _ur.urlopen(req, timeout=30) as r:
            return _jj.loads(r.read())

    loop = asyncio.get_event_loop()
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
            raw = await loop.run_in_executor(None, _fetch_json, req)
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
            raw = await loop.run_in_executor(None, _fetch_json, req)
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
            raw = await loop.run_in_executor(None, _fetch_json, req)
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
            raw = await loop.run_in_executor(None, _fetch_json, req)
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
    app.router.add_get("/api/auth/2fa",  api_2fa)
    app.router.add_post("/api/auth/2fa", api_2fa)

    app.router.add_get("/",                      index_handler)
    app.router.add_get("/ws",                    ws_handler)
    app.router.add_get("/api/status",            api_status)
    app.router.add_get("/api/devices",           api_devices)
    app.router.add_get("/api/stream/settings",   api_stream_settings)
    app.router.add_post("/api/stream/settings",  api_stream_settings)
    app.router.add_get("/api/oled/settings",   api_oled_settings)
    app.router.add_post("/api/oled/settings",  api_oled_settings)
    app.router.add_post("/api/oled/reset",     api_oled_reset)
    app.router.add_get("/api/wol/settings",    api_wol_settings)
    app.router.add_post("/api/wol/settings",   api_wol_settings)
    app.router.add_post("/api/wol/wake",       api_wol_wake)
    app.router.add_get("/api/wol/schedule",    api_wol_schedule)
    app.router.add_post("/api/wol/schedule",   api_wol_schedule)
    app.router.add_get("/api/keyboard/settings",  api_keyboard_settings)
    app.router.add_post("/api/keyboard/settings", api_keyboard_settings)
    app.router.add_post("/api/power",     api_power)
    app.router.add_get("/api/identity",  api_identity)
    app.router.add_post("/api/identity", api_identity)
    app.router.add_get("/api/jiggler",  api_jiggler)
    app.router.add_post("/api/jiggler", api_jiggler)
    app.router.add_get("/api/hid-autodisconnect",  api_hid_autodisconnect)
    app.router.add_post("/api/hid-autodisconnect", api_hid_autodisconnect)
    app.router.add_get("/api/mouse-mode",  api_mouse_mode)
    app.router.add_post("/api/mouse-mode", api_mouse_mode)
    app.router.add_get("/api/networks",   api_wifi)
    app.router.add_post("/api/networks",  api_wifi)
    app.router.add_get("/api/tailscale",  api_tailscale_get)
    app.router.add_post("/api/tailscale", api_tailscale)
    app.router.add_get("/api/network/lockdown",  api_network_lockdown)
    app.router.add_post("/api/network/lockdown", api_network_lockdown)
    app.router.add_post("/api/update", api_update)
    app.router.add_get("/api/stealth/logs",  api_stealth_logs)
    app.router.add_get("/api/ai/run",         api_ai_run)
    app.router.add_post("/api/ai/run",        api_ai_run)
    app.router.add_get("/api/ai/settings",    api_ai_settings)
    app.router.add_post("/api/ai/settings",   api_ai_settings)
    app.router.add_post("/api/ai/key",        api_ai_key)

    # Serve static files if present (CSS, JS, images, etc.)
    web_static = Path(WEB_ROOT) / "static"
    if web_static.exists():
        app.router.add_static("/static", str(web_static), show_index=False)

    return app


# Main entry point

async def main():
    log.info("MagicBridge v%s starting…", VERSION)

    _ensure_auth_defaults()
    _ensure_usb_defaults()

    # Load config
    cfg = {}
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text())
        log.info("Config loaded from %s", CONFIG_PATH)
    except FileNotFoundError:
        log.info("No config at %s, using defaults", CONFIG_PATH)
    except Exception as e:
        log.warning("Config load error: %s, using defaults", e)

    # Reapply the persisted Tailscale-only lockdown state, iptables rules
    # don't survive a reboot on their own, so this has to happen every boot.
    if cfg.get("network", {}).get("tailscale_only"):
        try:
            import subprocess as _sp_boot
            _sp_boot.run([LOCKDOWN_SH, "on"], capture_output=True, timeout=10)
            log.info("Network lockdown reapplied on boot")
        except Exception as e:
            log.warning("Could not reapply network lockdown: %s", e)

    # Mouse jiggler: load persisted enabled/style, then start the background
    # task regardless (it's a no-op loop when disabled, so a later live
    # enable via the UI doesn't need a service restart to take effect).
    jc = cfg.get("jiggler", {})
    jiggler.configure(bool(jc.get("enabled", False)), jc.get("style", JIGGLER_DEFAULT_STYLE))
    jiggler.start()
    log.info("Jiggler ready (enabled=%s, style=%s)", jiggler.enabled, jiggler.style)

    # HID connect-only-during-active-use: same load-then-always-start
    # pattern as the jiggler above, off by default.
    hc = cfg.get("hid_autodisconnect", {})
    hid_autodc.configure(bool(hc.get("enabled", False)), hc.get("idle_minutes", 15))
    hid_autodc.start()
    log.info("HID auto-disconnect ready (enabled=%s, idle_minutes=%s)",
             hid_autodc.enabled, hid_autodc.idle_minutes)

    # Start video stream
    vc  = cfg.get("video", {})
    loop = asyncio.get_running_loop()
    log.info("Starting video stream…")
    ok = await loop.run_in_executor(None, lambda: video.start(
        device     = vc.get("device"),
        resolution = vc.get("resolution", "1920x1080"),
        fps        = int(vc.get("fps", 30)),
        quality    = int(vc.get("quality", 80)),
        # "auto": video.start() detects the capture hardware and picks the
        # pipeline — C790/CSI -> H.264+WebRTC (preferred), USB dongle -> MJPEG.
        # Falls back to mjpeg on its own if the CSI/Janus path can't start, so
        # this is safe on any hardware combination.
        mode       = vc.get("mode", "auto"),
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
        jiggler.stop()
        hid_autodc.stop()
        keyboard.release_all()
        mouse.release_all()
        video.stop()
        await runner.cleanup()


if __name__ == "__main__":
    import sys as _sys
    if "--disable-2fa" in _sys.argv:
        # LOCKOUT ESCAPE HATCH. 2FA guards the only way into this device; if the
        # phone is lost, wiped, or its clock drifts badly, and the recovery codes
        # are gone too, this is the way back in. Requires root on the box (it
        # writes /etc/magicbridge/config.json), which is the right bar: someone
        # with an SSH shell already owns the device.
        try:
            _p = Path(CONFIG_PATH)
            _cfg = json.loads(_p.read_text()) if _p.exists() else {}
            _a = _cfg.setdefault("auth", {})
            _was = bool(_a.get("totp_enabled"))
            _a["totp_enabled"] = False
            for _k in ("totp_secret", "totp_recovery", "totp_pending", "totp_recovery_pending"):
                _a.pop(_k, None)
            _tmp = CONFIG_PATH + ".new"
            Path(_tmp).write_text(json.dumps(_cfg, indent=2))
            os.replace(_tmp, CONFIG_PATH)
            os.chmod(CONFIG_PATH, 0o600)
            print("2FA disabled." if _was else "2FA was not enabled; nothing to do.")
            print("Password login still applies. Restart: systemctl restart magicbridge")
        except Exception as _e:
            print("could not disable 2FA:", _e)
            _sys.exit(1)
        _sys.exit(0)
    if "--send-wol" in _sys.argv:
        # Entry point for the scheduled-WoL cron job: read the saved target MAC
        # and fire one magic packet, then exit. No server, no event loop.
        try:
            _c = (json.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).exists() else {}).get("wol", {})
            _mac = _wol_normalize_mac(_c.get("mac", ""))
            _wol_send_packet(_mac, _c.get("broadcast", "255.255.255.255"), int(_c.get("port", 9)))
            print("scheduled WoL sent to", _mac)
        except Exception as _e:
            print("scheduled WoL error:", _e)
        _sys.exit(0)
    asyncio.run(main())