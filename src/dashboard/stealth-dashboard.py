#!/usr/bin/env python3
"""MagicBridge Stealth Dashboard
   bcrypt auth · CSRF protection · WCAG AA UI
   USB identity · MAC spoofing · WiFi · Tailscale · DuckDNS · logs · backup
"""
import json, os, re, subprocess, secrets, time, datetime, hashlib, hmac, logging
from pathlib import Path
from flask import (Flask, jsonify, request, render_template_string,
                   session, redirect, Response)

# bcrypt (preferred) with SHA-256 fallback
try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

# App
app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

# Constants
CONFIG_PATH     = "/etc/magicbridge/config.json"
USB_DIR         = "/sys/kernel/config/usb_gadget/g1"
GADGET_SH       = "/usr/local/bin/mb-gadget.sh"
RAM_LOG_DIR     = "/var/log/magicbridge-ram"   # tmpfs mount, wiped on reboot/power-loss
AUTH_LOG        = f"{RAM_LOG_DIR}/magicbridge-auth.log"
SESS_LOG        = f"{RAM_LOG_DIR}/magicbridge-sessions.log"
SESSION_TIMEOUT = 1800   # 30 min idle

# NOTE: the stealth panel and the main KVM page used to share one session
# cookie (logging into either unlocked both). They now have fully independent
# passwords and sessions by design. A compromised main-page password no
# longer exposes the admin panel. This panel uses Flask's own session only.

# Default USB identity: a real Logitech Unifying Receiver identity, not a
# single-purpose keyboard model. The gadget always exposes both a keyboard
# AND a mouse HID interface, and a keyboard-only product (like the old
# K120 default) never legitimately has a mouse interface, an easy
# structural tell. A combo receiver dongle is supposed to look exactly
# like this, so it isn't one. Serial is generated per-device (see
# _gen_default_serial) rather than a fixed value, so "safe mode" never
# reverts to an obviously-placeholder serial.
ORIG = {
    "manufacturer": "Logitech",
    "product":      "USB Receiver",
    "idVendor":     "0x046d",
    "idProduct":    "0xc52b",
    "has_serial":   False,   # real Unifying Receivers report iSerial = 0
    "extra_iface":  True,    # real ones expose a 3rd (idle) HID interface
}

def _gen_default_serial() -> str:
    """Realistic Logitech-style serial (YYMMcode+5digits), deterministic
    from this Pi's own MAC address so every device gets a different value
    instead of a shared placeholder. Same format and same seed as
    magicbridge.py's _gen_serial(0) for the K120 profile, so this always
    matches whatever the main service already bootstrapped for this Pi."""
    import random as _rr, hashlib as _hh
    try:
        mac = Path("/sys/class/net/wlan0/address").read_text().strip().replace(":", "")
    except Exception:
        try:
            mac = Path("/sys/class/net/eth0/address").read_text().strip().replace(":", "")
        except Exception:
            mac = "dca632c49b00"
    seed = int(_hh.md5((mac + "0").encode()).hexdigest()[:8], 16)
    rng = _rr.Random(seed)
    yr = rng.randint(19, 23); mo = rng.randint(1, 12)
    return "%02d%02dLK%05d" % (yr, mo, rng.randint(10000, 99999))


def _gen_profile_serial(idx: int, pfx: str) -> str:
    """Deterministic per-Pi, per-profile serial. Same reasoning as
    _gen_default_serial() above, generalized to any USB_PROFILES entry:
    a fixed placeholder serial baked into every MagicBridge unit would link
    them to each other, but regenerating a fresh random serial on every
    single 'Apply preset' click is its own tell (a real device's serial
    doesn't change every time you plug it in). Seeding from this Pi's own
    MAC plus the profile index gives a value that's stable across repeated
    applies of the same profile, differs between profiles, and differs
    between physical Pis. _rand_serial()/api_randomize stay available as an
    explicit opt-in for someone who actually wants to force a new one."""
    import random as _rr, hashlib as _hh
    try:
        mac = Path("/sys/class/net/wlan0/address").read_text().strip().replace(":", "")
    except Exception:
        try:
            mac = Path("/sys/class/net/eth0/address").read_text().strip().replace(":", "")
        except Exception:
            mac = "dca632c49b00"
    seed = int(_hh.md5((mac + str(idx)).encode()).hexdigest()[:8], 16)
    rng = _rr.Random(seed)
    return pfx + "".join(rng.choice("0123456789ABCDEF") for _ in range(8))

# Real wireless keyboard+mouse combo receiver dongles, chosen deliberately
# over single-purpose keyboard models (see the ORIG comment above for why).
# has_serial/extra_iface/verified: only the Logitech entry is verified
# against a real device's descriptor (iSerial=0, 3 interfaces incl. one idle
# vendor HID interface) - that unit was physically available to inspect.
# Microsoft/Dell VID:PID pairs are real (045e:0800 matches Microsoft's own
# published driver ID for the "USB Dual Receiver"; 413c:2513 matches Dell's
# vendor block for wireless combo receivers), researched against public
# driver/USB-ID databases, but the exact interface count/serial presence for
# those two is still unconfirmed without the physical dongles to inspect -
# "verified": False flags that gap so the UI can surface it instead of
# presenting all three presets as equally battle-tested.
USB_PROFILES = [
    {"name":"Logitech Unifying Receiver", "mfr":"Logitech",  "prod":"USB Receiver",               "vid":"0x046d","pid":"0xc52b","pfx":"LGK","has_serial":False,"extra_iface":True, "verified":True},
    {"name":"Microsoft Dual Receiver",    "mfr":"Microsoft", "prod":"Microsoft USB Dual Receiver", "vid":"0x045e","pid":"0x0800","pfx":"MSK","has_serial":True, "extra_iface":False,"verified":False},
    {"name":"Dell Wireless Combo",        "mfr":"Dell",      "prod":"Dell Wireless Keyboard and Mouse Combo", "vid":"0x413c","pid":"0x2513","pfx":"DEL","has_serial":True, "extra_iface":False,"verified":False},
]

# EDID / display-identity presets. Identity-only (mfr PNP id / product name /
# product id) - deliberately NOT paired with real donor timings, per
# EDID_CLONING_WORKFLOW.md section 1: the Pi 4's 2-CSI-lane TC358743 path
# tops out at 1080p50, so these presets always apply on top of the
# Pi-4-safe base blob (edid_base_file below), never a wholesale monitor
# EDID clone. "serial_prefix" mirrors USB_PROFILES' pfx pattern - the
# actual serial is randomized per-apply from this prefix.
EDID_PROFILES = [
    {"name": "Generic Dell 24\"",   "mfr": "DEL", "product_name": "DELL P2419H", "product_id": 0xA06B, "serial_prefix": "DL"},
    {"name": "Generic LG 27\"",     "mfr": "GSM", "product_name": "LG ULTRAWIDE", "product_id": 0x5A20, "serial_prefix": "LG"},
    {"name": "Generic Samsung 24\"", "mfr": "SAM", "product_name": "SAMSUNG",     "product_id": 0x412D, "serial_prefix": "SM"},
]

EDID_DEFAULTS = {
    "enabled": False,
    "profile_idx": None,
    "mfr": "", "product_name": "", "product_id": 0, "serial": "",
    "base_file": "/etc/magicbridge/tc358743-edid.hex",
    "applied_file": "/etc/magicbridge/tc358743-edid-identity.hex",
}

LOG_SOURCES = {
    "auth":      AUTH_LOG,
    "sessions":  SESS_LOG,
    "nginx":     "/var/log/nginx/access.log",
    "nginx-err": "/var/log/nginx/error.log",
    "system":    "/var/log/syslog",
    "magicbridge": "/var/log/magicbridge.log",
}

# Auth logging
# Defensive mkdir: normally RAM_LOG_DIR is a tmpfs mount created at boot via
# fstab, but if the service starts before the mount (or on a fresh deploy
# before reboot), fall back to a plain directory on disk rather than
# silently losing the FileHandler below.
try:
    Path(RAM_LOG_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    pass
_al = logging.getLogger("magicbridge.stealth")
_al.setLevel(logging.INFO)
try:
    _fh = logging.FileHandler(AUTH_LOG)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _al.addHandler(_fh)
except Exception:
    pass

# Progressive login delay
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

# Password helpers
def _hash_pw(pw: str) -> str:
    if _HAS_BCRYPT:
        return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    return "sha256:" + hashlib.sha256(pw.encode()).hexdigest()

def _check_pw(pw: str, stored: str) -> bool:
    if _HAS_BCRYPT and stored.startswith("$2"):
        return _bcrypt.checkpw(pw.encode(), stored.encode())
    raw = stored.removeprefix("sha256:")
    return hashlib.sha256(pw.encode()).hexdigest() == raw

# Config helpers
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
    _purge_old_logs_if_due()

# CSRF
def _csrf_ok() -> bool:
    tok = (request.headers.get("X-CSRF-Token") or request.form.get("_csrf", ""))
    return tok == session.get("csrf")

def _fresh_login_csrf() -> str:
    t = secrets.token_hex(32)
    session["login_csrf"] = t
    return t

# Auth helpers
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

# USB helpers
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

def _apply_usb(mfr: str, prod: str, ser: str, vid: str = None, pid: str = None,
                has_serial: bool = None, extra_iface: bool = None):
    """Apply USB identity to live configfs and persist to config.json.
    has_serial/extra_iface are persisted (when given) so mb-gadget.sh
    builds the right structure on next reboot; the serial number itself
    always takes effect live, but interface count can only change when
    the gadget's functions are rebuilt (reboot / mb-gadget.sh re-run)."""
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
    if has_serial is not None:  usb["has_serial"]  = has_serial
    if extra_iface is not None: usb["extra_iface"] = extra_iface
    _save(cfg)

def _rand_serial(pfx: str = "MB") -> str:
    return pfx + secrets.token_hex(4).upper()

# EDID / display identity helpers
def _edid_video_device() -> str:
    """Return the tc358743/C790 video device node if one is enumerated,
    else ''. Never raises - the whole EDID feature must stay a graceful
    no-op on the current MS2109-only setup (which has no such device, and
    wouldn't support EDID writes even if it did - see workflow doc §0)."""
    try:
        out = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True,
                              text=True, timeout=5).stdout
    except Exception:
        return ""
    m = re.search(r"tc358743[^\n]*\n\s*(/dev/video\d+)", out, re.IGNORECASE)
    return m.group(1) if m else ""


def _edid_hw_ready(cfg: dict) -> tuple[bool, str]:
    """(ready, reason). ready=True only if BOTH a tc358743 device node is
    present AND a base EDID blob exists on disk. Either missing is a normal,
    expected state right now (C790 hasn't arrived) - not an error."""
    dev = _edid_video_device()
    if not dev:
        return False, "No TC358743/C790 capture device detected (MS2109 doesn't support EDID writes)."
    edid_cfg = dict(EDID_DEFAULTS)
    edid_cfg.update(cfg.get("edid", {}))
    base_file = edid_cfg.get("base_file", EDID_DEFAULTS["base_file"])
    if not Path(base_file).exists():
        return False, f"No base EDID installed at {base_file} yet (see EDID_CLONING_WORKFLOW.md §3)."
    return True, dev


def _edid_numeric_serial(serial_str: str) -> int:
    """EDID's base-block serial (bytes 12-15) is a 32-bit int, but we want a
    human-readable serial string like the USB profiles use (e.g. 'DL3F9A2B').
    Derive a stable 32-bit value from the string via crc32 - NOT Python's
    hash(), which is per-process randomized (PYTHONHASHSEED) and would give
    a different EDID serial every service restart."""
    import zlib
    return zlib.crc32(serial_str.encode()) & 0xFFFFFFFF


def _apply_edid(mfr: str, product_name: str, product_id: int, serial: str,
                 profile_idx=None) -> dict:
    """Persist the identity to config.json always; only touch real hardware
    (write the overlaid EDID + reload via v4l2-ctl) when both the C790 and
    a base blob are present. Never raises - callers get back a dict with
    ok/applied_live/reason rather than a 500."""
    cfg = _load()
    edid_cfg = dict(EDID_DEFAULTS)
    edid_cfg.update(cfg.get("edid", {}))
    edid_cfg.update({
        "enabled": True, "mfr": mfr, "product_name": product_name,
        "product_id": product_id, "serial": serial,
    })
    if profile_idx is not None:
        edid_cfg["profile_idx"] = profile_idx
    cfg["edid"] = edid_cfg
    _save(cfg)

    ready, reason = _edid_hw_ready(cfg)
    if not ready:
        return {"ok": True, "applied_live": False, "reason": reason}

    dev = reason  # _edid_hw_ready returns the device node as `reason` when ready
    try:
        import mb_edidconf
    except ImportError as ex:
        return {"ok": True, "applied_live": False, "reason": f"mb_edidconf module not installed: {ex}"}
    try:
        result = mb_edidconf.apply_identity_to_file(
            edid_cfg["base_file"], edid_cfg["applied_file"],
            mfr_pnp=mfr, product_id=product_id, serial=_edid_numeric_serial(serial),
            product_name=product_name, ensure_basic_audio=True,
        )
        subprocess.run(["v4l2-ctl", "-d", dev, f"--set-edid=file={edid_cfg['applied_file']}",
                        "--fix-edid-checksums"], capture_output=True, text=True, timeout=10, check=True)
        return {"ok": True, "applied_live": True, "device": dev, "detail": result}
    except mb_edidconf.EdidError as ex:
        return {"ok": True, "applied_live": False, "reason": f"EDID build error: {ex}"}
    except Exception as ex:
        return {"ok": True, "applied_live": False, "reason": f"v4l2-ctl load failed: {ex}"}

# Network helpers
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

# Common-vendor OUI prefixes for MAC spoofing, so the device reads as a
# real laptop/TV vendor on a router's client list or a network scan instead
# of the same fixed 00:1a:2b prefix every time. Verified against the IEEE
# OUI registry (each is a real MA-L allocation for that company) rather
# than guessed - a made-up prefix that doesn't resolve to the claimed
# vendor would look worse than no spoofing at all.
MAC_PROFILES = [
    {"name": "Dell",    "oui": [0x00, 0x14, 0x22]},
    {"name": "HP",      "oui": [0x9C, 0x7B, 0xEF]},
    {"name": "Samsung", "oui": [0x64, 0x1B, 0x2F]},
]


def _rand_mac(oui=None) -> str:
    b = list(oui) if oui else [0x00, 0x1a, 0x2b]
    b += [secrets.randbits(8) for _ in range(3)]
    return ":".join(f"{x:02x}" for x in b)

def _tailscale_status() -> dict:
    try:
        r = subprocess.run(["tailscale","status","--json"],
                           capture_output=True, text=True, timeout=4)
        d = json.loads(r.stdout)
        connected = d.get("BackendState") == "Running"
        # Tailscale can still list a reserved TailscaleIPs entry even while
        # the backend is fully stopped, so only surface the IP when actually
        # connected (matches the same fix applied to magicbridge.py's
        # /api/status).
        ip = (d.get("TailscaleIPs") or [""])[0] if connected else ""
        return {
            "connected": connected,
            "ip":        ip,
            "state":     d.get("BackendState", "unknown"),
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

# Onboard LEDs. ACT (green, SD-card activity) and PWR (red, always-on) are
# the two physical LEDs on a Pi 4. Toggling is done two ways: live via sysfs
# (immediate, no reboot) AND via a small systemd oneshot unit that re-applies
# the saved state at boot (LED sysfs nodes reset to their default trigger on
# every boot, so the live-only write wouldn't survive a power cycle). This
# avoids touching /boot/firmware/config.txt dtparam overlays entirely - those
# also work but their active-low polarity varies by board revision, and
# getting it backwards would leave an LED stuck on instead of off. Writing
# trigger=none + brightness=0 directly is unambiguous on any revision.
LED_PATHS = ["/sys/class/leds/ACT", "/sys/class/leds/PWR"]
LED_SERVICE_PATH = "/etc/systemd/system/mb-led.service"


def _apply_leds(enabled: bool) -> dict:
    """Best-effort live toggle (never raises - a missing/renamed LED sysfs
    path on some board revision shouldn't break the rest of the apply call),
    plus persistence: config.json + a systemd unit that re-applies at boot."""
    results = {}
    for path in LED_PATHS:
        p = Path(path)
        try:
            if enabled:
                (p / "trigger").write_text("mmc0" if "ACT" in path else "default-on")
            else:
                (p / "trigger").write_text("none")
                (p / "brightness").write_text("0")
            results[path] = "ok"
        except Exception as e:
            results[path] = f"skipped: {e}"

    cfg = _load()
    cfg["leds_enabled"] = enabled
    _save(cfg)

    try:
        exec_lines = "\n".join(
            f'ExecStart=/bin/bash -c "echo none > {path}/trigger 2>/dev/null || true; '
            f'echo 0 > {path}/brightness 2>/dev/null || true"'
            for path in LED_PATHS
        ) if not enabled else "ExecStart=/bin/true"
        svc = (
            "[Unit]\nDescription=MagicBridge onboard LED state (persists across reboot)\n"
            "After=sysinit.target\n\n"
            "[Service]\nType=oneshot\n"
            f"{exec_lines}\n"
            "RemainAfterExit=yes\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        Path(LED_SERVICE_PATH).write_text(svc)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "enable", "mb-led"], capture_output=True)
    except Exception as e:
        results["_service"] = f"failed: {e}"

    return {"ok": True, "enabled": enabled, "detail": results}


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

# Log retention: both AUTH_LOG and SESS_LOG grow forever otherwise. This
# purges lines older than the retention window, checked at most once a day
# (on boot and opportunistically at login) rather than on every write.
LOG_RETENTION_DAYS = 30
_last_log_purge = 0.0

def _purge_old_log_lines(path: str, days: int = LOG_RETENTION_DAYS):
    """Drop lines older than `days` from a plain-text log file. Handles both
    ISO timestamps (session log) and Python logging's default asctime format
    (auth log, "YYYY-MM-DD HH:MM:SS,mmm"). Lines whose timestamp can't be
    parsed are kept rather than risk discarding a real entry."""
    try:
        if not os.path.isfile(path):
            return
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        kept = []
        for line in lines:
            m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})[.,]?\d*", line)
            if m:
                try:
                    ts = datetime.datetime.fromisoformat(m.group(1).replace(" ", "T"))
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
            kept.append(line)
        if len(kept) != len(lines):
            with open(path, "w") as f:
                f.writelines(kept)
    except Exception:
        pass

def _purge_old_logs_if_due():
    global _last_log_purge
    now = time.time()
    if now - _last_log_purge < 86400:   # at most once a day
        return
    _last_log_purge = now
    _purge_old_log_lines(AUTH_LOG)
    _purge_old_log_lines(SESS_LOG)

# DuckDNS
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

# HTML: Login
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MagicBridge Stealth</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%;background:#02040a;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#d8f6ff}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem;position:relative;overflow:hidden}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 900px 620px at 8% -10%, rgba(0,229,255,.15), transparent 60%),
    radial-gradient(ellipse 760px 560px at 110% 15%, rgba(176,38,255,.12), transparent 60%),
    radial-gradient(ellipse 820px 640px at 50% 120%, rgba(0,229,255,.07), transparent 62%),
    linear-gradient(180deg,#02040a 0%,#05101a 55%,#02040a 100%);}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.4;
  background:
    repeating-linear-gradient(0deg, rgba(0,229,255,.05) 0px, rgba(0,229,255,.05) 1px, transparent 1px, transparent 3px),
    repeating-linear-gradient(90deg, rgba(0,229,255,.03) 0px, rgba(0,229,255,.03) 1px, transparent 1px, transparent 46px),
    repeating-linear-gradient(0deg, rgba(0,229,255,.03) 0px, rgba(0,229,255,.03) 1px, transparent 1px, transparent 46px);}
.crt-sweep{position:fixed;left:0;right:0;top:-2px;height:2px;z-index:2;pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(0,229,255,.6),transparent);
  box-shadow:0 0 14px rgba(0,229,255,.55)}
@media (prefers-reduced-motion: no-preference){.crt-sweep{animation:sweep 5s linear infinite}}
@keyframes sweep{0%{top:-2px}100%{top:100%}}
.card{position:relative;z-index:1;background:rgba(6,13,22,.68);backdrop-filter:blur(20px) saturate(140%);
      -webkit-backdrop-filter:blur(20px) saturate(140%);
      border:1px solid rgba(0,229,255,.25);border-radius:6px;
      padding:2.1rem 2rem;width:100%;max-width:320px;
      box-shadow:0 0 0 1px rgba(0,229,255,.04),0 0 44px rgba(0,229,255,.14),0 20px 60px rgba(0,0,0,.6)}
.card::before,.card::after{content:'';position:absolute;width:16px;height:16px;pointer-events:none;
  border:2px solid #00e5ff;filter:drop-shadow(0 0 4px rgba(0,229,255,.7))}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.brand svg{width:28px;height:28px;flex-shrink:0;filter:drop-shadow(0 0 6px rgba(0,229,255,.55))}
h1{font:700 16px/1 ui-monospace,"SF Mono","Cascadia Code","Roboto Mono",monospace;
   letter-spacing:1.5px;text-transform:uppercase;
   background:linear-gradient(135deg,#00e5ff 0%,#7cf2ff 45%,#b026ff 100%);
   -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
   text-shadow:0 0 22px rgba(0,229,255,.3)}
.sub{font:11.5px/1.4 ui-monospace,"SF Mono","Cascadia Code",monospace;color:#5f8ba3;
     margin:7px 0 1.6rem;letter-spacing:.5px}
.sub::before{content:'// '}
label{display:block;font:600 10.5px/1 ui-monospace,"SF Mono","Cascadia Code",monospace;
      color:#5f8ba3;margin-bottom:7px;letter-spacing:1.2px;text-transform:uppercase}
label::before{content:'> '}
input[type=password]{
  width:100%;padding:10px 12px;background:rgba(2,4,10,.85);
  border:1px solid rgba(0,229,255,.22);border-radius:3px;letter-spacing:3px;
  color:#d8f6ff;font:13px ui-monospace,"SF Mono",monospace;outline:none;transition:border .15s,box-shadow .15s}
input[type=password]:focus{border-color:#00e5ff;
  box-shadow:0 0 0 2px rgba(0,229,255,.18),0 0 18px rgba(0,229,255,.25)}
button{
  margin-top:1rem;width:100%;padding:11px;
  background:linear-gradient(135deg,#00e5ff 0%,#00b8d9 55%,#7c2fff 100%);
  border:none;border-radius:3px;color:#02040a;font:700 12.5px ui-monospace,"SF Mono",monospace;
  letter-spacing:2.5px;text-transform:uppercase;cursor:pointer;
  transition:filter .15s,transform .1s,box-shadow .15s;box-shadow:0 0 22px rgba(0,229,255,.28)}
button:hover{filter:brightness(1.15);box-shadow:0 0 30px rgba(0,229,255,.48)}
button:active{transform:scale(.98)}
button:focus{outline:2px solid #00e5ff;outline-offset:3px}
.err{
  margin-top:.8rem;padding:9px 11px;font:11.5px ui-monospace,"SF Mono",monospace;letter-spacing:.3px;
  background:rgba(255,46,99,.1);border:1px solid rgba(255,46,99,.35);
  border-radius:3px;color:#ff5c82}
.err::before{content:'! '}
.hint{margin-top:1.1rem;font:10.5px ui-monospace,"SF Mono",monospace;color:#5f8ba3;
      text-align:center;letter-spacing:.3px}
</style>
</head>
<body>
<div class="crt-sweep" aria-hidden="true"></div>
<main>
<div class="card">
  <div class="brand">
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="MagicBridge">
      <defs><linearGradient id="sg1" x1="0" y1="0" x2="100" y2="100" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#00e5ff"/><stop offset="100%" stop-color="#b026ff"/>
      </linearGradient></defs>
      <path d="M15 40 C15 25 30 20 50 20 C70 20 85 25 85 40 C85 55 72 58 50 58 C28 58 15 55 15 40 Z" fill="none" stroke="url(#sg1)" stroke-width="5"/>
      <path d="M50 20 L50 58" stroke="url(#sg1)" stroke-width="3.4" opacity=".4"/>
      <circle cx="32" cy="40" r="3.4" fill="url(#sg1)"/>
      <circle cx="68" cy="40" r="3.4" fill="url(#sg1)"/>
      <path d="M22 66 Q50 78 78 66" stroke="url(#sg1)" stroke-width="4.2" fill="none" opacity=".5"/>
      <path d="M28 74 Q50 84 72 74" stroke="url(#sg1)" stroke-width="3.4" fill="none" opacity=".3"/>
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

# HTML: Main dashboard
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{{ csrf }}">
<title>MagicBridge Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#02040a; --sf:#060c16; --sf2:#0a1220;
  --br:#0f2836; --br2:#1c3f52;
  --t1:#d8f6ff; --t2:#6f93a8; --t3:#5a7f99;
  --ac:#00e5ff; --ac-bg:rgba(0,229,255,.12);
  --ac2:#b026ff; --ac2-bg:rgba(176,38,255,.12);
  --ok:#39ff88; --ok-bg:rgba(57,255,136,.1);
  --wa:#ffb020; --wa-bg:rgba(255,176,32,.1);
  --er:#ff2e63; --er-bg:rgba(255,46,99,.1);
  --mono:ui-monospace,"SF Mono","Cascadia Code","Roboto Mono",monospace;
}
html{font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--t1)}
body{position:relative}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 900px 600px at 6% -10%, rgba(0,229,255,.10), transparent 60%),
    radial-gradient(ellipse 760px 560px at 108% 8%, rgba(176,38,255,.08), transparent 60%),
    radial-gradient(ellipse 900px 640px at 50% 118%, rgba(0,229,255,.05), transparent 62%)}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.3;
  background:
    repeating-linear-gradient(90deg, rgba(0,229,255,.025) 0px, rgba(0,229,255,.025) 1px, transparent 1px, transparent 46px),
    repeating-linear-gradient(0deg, rgba(0,229,255,.025) 0px, rgba(0,229,255,.025) 1px, transparent 1px, transparent 46px)}
.crt-sweep{position:fixed;left:0;right:0;top:-2px;height:2px;z-index:30;pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(0,229,255,.5),transparent);
  box-shadow:0 0 14px rgba(0,229,255,.45)}
@media (prefers-reduced-motion: no-preference){.crt-sweep{animation:sweep 7s linear infinite}}
@keyframes sweep{0%{top:-2px}100%{top:100%}}
::selection{background:rgba(0,229,255,.28);color:#fff}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--br2);border-radius:5px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:var(--ac)}
.sk{position:absolute;top:-999px;left:0;padding:6px 12px;
    background:var(--ac);color:#02040a;font-size:12px;z-index:9999;
    border-radius:0 0 6px 0;text-decoration:none;font-weight:700}
.sk:focus{top:0}
header{
  position:relative;z-index:20;display:flex;align-items:center;gap:10px;padding:10px 16px;
  background:var(--sf);border-bottom:1px solid var(--br);
  box-shadow:0 1px 16px rgba(0,229,255,.1);
  position:sticky;top:0}
.logo{font:700 14px/1 var(--mono);letter-spacing:1.6px;text-transform:uppercase;
  background:linear-gradient(135deg,#00e5ff 0%,#7cf2ff 45%,#b026ff 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  text-shadow:0 0 16px rgba(0,229,255,.25)}
.bdg{font:600 10px/1 var(--mono);padding:3px 9px;border-radius:3px;
     letter-spacing:1px;text-transform:uppercase;border:1px solid}
.b-ok{background:var(--ok-bg);color:var(--ok);border-color:rgba(57,255,136,.4);box-shadow:0 0 10px rgba(57,255,136,.25)}
.b-er{background:var(--er-bg);color:var(--er);border-color:rgba(255,46,99,.4);box-shadow:0 0 10px rgba(255,46,99,.25)}
.sbar{
  position:relative;z-index:15;display:flex;gap:16px;flex-wrap:wrap;padding:6px 16px;
  background:var(--sf2);border-bottom:1px solid var(--br);
  font:11px/1.6 var(--mono);color:var(--t3);letter-spacing:.3px}
main{position:relative;z-index:1;padding:14px 16px;display:grid;gap:14px}
@media(min-width:680px){main{grid-template-columns:1fr 1fr}}
.full{grid-column:1/-1}
.card{position:relative;background:rgba(6,12,22,.82);border:1px solid var(--br);border-radius:6px;
      overflow:hidden;box-shadow:0 0 0 1px rgba(0,229,255,.03),0 0 24px rgba(0,229,255,.05)}
.card::before,.card::after{content:'';position:absolute;width:12px;height:12px;z-index:2;pointer-events:none;
  border:2px solid rgba(0,229,255,.65);filter:drop-shadow(0 0 3px rgba(0,229,255,.5))}
.card::before{top:-1px;left:-1px;border-right:none;border-bottom:none}
.card::after{bottom:-1px;right:-1px;border-left:none;border-top:none}
.ch{padding:10px 14px;border-bottom:1px solid var(--br);
    display:flex;align-items:center;gap:8px}
.ch h2{font:600 12px/1 var(--mono);letter-spacing:1px;text-transform:uppercase;flex:1;color:var(--t1)}
.ch h2::before{content:'▸ ';color:var(--ac)}
.ch .cd{font-size:11px;color:var(--t3)}
.cb{padding:12px 14px}
.field{margin-bottom:10px}
.field:last-child{margin-bottom:0}
.fl{display:block;font:600 10.5px/1 var(--mono);color:var(--t3);margin-bottom:5px;letter-spacing:.6px;text-transform:uppercase}
.fd{display:block;font-size:11px;color:var(--t3);margin-top:3px;line-height:1.4;opacity:.8}
.frow{display:flex;gap:7px;align-items:flex-start;flex-wrap:wrap}
input[type=text],input[type=password],select,textarea{
  background:var(--bg);border:1px solid var(--br2);border-radius:4px;
  color:var(--t1);font-size:12px;padding:6px 9px;
  outline:none;transition:border .15s,box-shadow .15s;font-family:inherit}
input:focus,select:focus,textarea:focus{
  border-color:var(--ac);box-shadow:0 0 0 2px rgba(0,229,255,.15),0 0 12px rgba(0,229,255,.2)}
select{cursor:pointer}
textarea{resize:vertical;min-height:58px;font-family:var(--mono);
         font-size:10px;width:100%;line-height:1.5}
.btn{
  padding:5px 13px;border-radius:4px;font:600 11px/1.4 var(--mono);letter-spacing:.5px;
  cursor:pointer;border:1px solid var(--br2);text-transform:uppercase;
  background:var(--sf2);color:var(--t2);
  transition:background .15s,color .15s,border-color .15s,box-shadow .15s}
.btn:hover{background:var(--br2);color:var(--t1);border-color:var(--ac)}
.btn:focus{outline:2px solid var(--ac);outline-offset:2px}
.btn-p{background:linear-gradient(135deg,var(--ac) 0%,#00b8d9 60%,var(--ac2) 100%);
       border-color:transparent;color:#02040a;box-shadow:0 0 14px rgba(0,229,255,.3)}
.btn-p:hover{filter:brightness(1.12);box-shadow:0 0 22px rgba(0,229,255,.5)}
.btn-d{background:var(--er-bg);border-color:rgba(255,46,99,.4);color:var(--er)}
.btn-d:hover{background:rgba(255,46,99,.2);border-color:var(--er);box-shadow:0 0 12px rgba(255,46,99,.25)}
.pills{display:flex;flex-wrap:wrap;gap:5px;margin:5px 0 8px}
.pill{
  padding:3px 10px;border-radius:20px;font:11px var(--mono);letter-spacing:.3px;
  border:1px solid var(--br2);background:transparent;color:var(--t2);
  cursor:pointer;transition:all .15s}
.pill:hover{border-color:var(--ac);color:var(--ac)}
.pill.on{border-color:var(--ac);background:var(--ac-bg);color:var(--ac);box-shadow:0 0 8px rgba(0,229,255,.25)}
.pill:focus{outline:2px solid var(--ac);outline-offset:2px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;
     vertical-align:middle;margin-right:4px}
.d-ok{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.d-er{background:var(--er);box-shadow:0 0 6px var(--er)}
.d-wa{background:var(--wa);box-shadow:0 0 6px var(--wa)}
.log{
  background:var(--bg);border:1px solid var(--br);border-radius:4px;
  padding:8px 10px;font-family:var(--mono);font-size:10px;color:var(--t3);
  height:130px;overflow-y:auto;white-space:pre-wrap;
  word-break:break-all;line-height:1.5;margin-top:4px}
hr{border:none;border-top:1px solid var(--br);margin:10px 0}
.ibar{height:2px;background:var(--br);position:sticky;bottom:0}
.ifill{height:100%;background:linear-gradient(90deg,var(--ac),var(--ac2));border-radius:1px;
       transition:width 1s linear;box-shadow:0 0 8px rgba(0,229,255,.5)}
#toast{
  position:fixed;bottom:14px;right:14px;z-index:999;
  background:var(--sf);border:1px solid var(--br2);border-radius:6px;
  padding:9px 15px;font:12px var(--mono);opacity:0;transition:opacity .25s;
  pointer-events:none;max-width:260px}
#toast.show{opacity:1}
#toast.ok{border-left:3px solid var(--ok);color:var(--ok);box-shadow:0 0 18px rgba(57,255,136,.2)}
#toast.er{border-left:3px solid var(--er);color:var(--er);box-shadow:0 0 18px rgba(255,46,99,.2)}
</style>
</head>
<body>
<div class="crt-sweep" aria-hidden="true"></div>
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

<!-- USB Identity -->
<section class="card" aria-labelledby="h-usb">
  <div class="ch">
    <h2 id="h-usb">USB identity</h2>
    <span class="cd">How this device appears to the connected computer</span>
  </div>
  <div class="cb">
    <div class="field">
      <span class="fl" id="h-preset">Quick preset</span>
      <span class="fd">Pick a keyboard preset (updates all fields). Click Apply to send. Presets marked "unverified" use real VID/PID values but haven't been checked against the physical device.</span>
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
        <input type="text" id="u-pid" placeholder="0xc52b" style="width:100%">
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

<!-- Display Identity (EDID) -->
<section class="card" aria-labelledby="h-edid">
  <div class="ch">
    <h2 id="h-edid">Display identity (EDID)</h2>
    <span class="cd">How this device's HDMI capture identifies itself as a monitor</span>
  </div>
  <div class="cb">
    <div id="edid-hw-st" style="font-size:12px;color:var(--t3);margin:0 0 8px" role="status" aria-live="polite">Checking hardware…</div>
    <div class="field">
      <span class="fl" id="h-edid-preset">Quick preset</span>
      <span class="fd">Pick a generic monitor identity (updates fields below). Click Apply to send.</span>
      <div class="pills" role="group" aria-labelledby="h-edid-preset" id="edid-pills"></div>
      <button class="btn btn-p" onclick="applyEdidPreset()" aria-label="Apply selected display preset">Apply preset</button>
    </div>
    <hr>
    <div class="frow" style="margin-bottom:7px">
      <div class="field" style="flex:1;min-width:80px">
        <label class="fl" for="e-mfr">Manufacturer ID</label>
        <input type="text" id="e-mfr" placeholder="DEL" maxlength="3" style="width:100%">
      </div>
      <div class="field" style="flex:2;min-width:140px">
        <label class="fl" for="e-name">Monitor name</label>
        <input type="text" id="e-name" placeholder="DELL P2419H" maxlength="12" style="width:100%">
      </div>
      <div class="field" style="flex:1;min-width:90px">
        <label class="fl" for="e-pid">Product ID</label>
        <input type="text" id="e-pid" placeholder="0xA06B" style="width:100%">
      </div>
      <div class="field" style="flex:1;min-width:90px">
        <label class="fl" for="e-ser">Serial</label>
        <input type="text" id="e-ser" style="width:100%">
      </div>
    </div>
    <span class="fd" style="display:block;margin-bottom:8px">
      Clones only the monitor's identity (name/manufacturer/serial) onto a base EDID capped at
      1080p50 - the Pi 4's TC358743 capture ceiling. Never clones a donor's raw timings, so the
      target can't be pushed into a resolution this Pi can't capture.
    </span>
    <div class="frow" style="flex-wrap:wrap;gap:6px">
      <button class="btn btn-p" onclick="applyEdidCustom()" aria-label="Apply custom display identity">Apply identity</button>
      <button class="btn" onclick="randEdidSerial()" aria-label="Generate random serial">Random serial</button>
      <button class="btn btn-d" onclick="resetEdid()" aria-label="Reset display identity">Reset</button>
    </div>
  </div>
</section>

<!-- Network -->
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
      </div>
      <span class="fl" style="margin-top:8px;display:block" id="h-mac-vendor">Vendor (random suffix, real OUI)</span>
      <div class="pills" role="group" aria-labelledby="h-mac-vendor" id="mac-pills"></div>
      <button class="btn" onclick="randMac()" aria-label="Randomize MAC with selected vendor">Randomize</button>
      <span class="fd">Applied immediately and persists across reboots via systemd.</span>
      <div id="mac-persist-st" style="font-size:11px;color:var(--t3);margin-top:4px" role="status" aria-live="polite"></div>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-ts">Tailscale: encrypted remote-access tunnel</span>
      <div id="ts-st" role="status" aria-live="polite" style="font-size:12px;color:var(--t3);margin:4px 0 6px">Loading…</div>
      <button class="btn" onclick="tsUp()" aria-label="Reconnect Tailscale">Reconnect</button>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-fn">Tailscale Funnel: public HTTPS access</span>
      <span class="fd">Exposes MagicBridge publicly. Requires Tailscale ≥ 1.34 and Funnel enabled in your tailnet.</span>
      <div id="fn-st" role="status" aria-live="polite" style="font-size:12px;color:var(--t3);margin:4px 0 6px">Loading…</div>
      <div class="frow" style="flex-wrap:wrap;gap:6px">
        <button class="btn btn-p" onclick="funnelOn()" aria-label="Enable Tailscale Funnel">Enable Funnel</button>
        <button class="btn btn-d" onclick="funnelOff()" aria-label="Disable Funnel">Disable</button>
      </div>
    </div>
    <hr>
    <div class="field">
      <span class="fl" id="h-ddns">DuckDNS: free public hostname</span>
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

<!-- WiFi -->
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

<!-- KVM Activity -->
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

<!-- System -->
<section class="card" aria-labelledby="h-sys">
  <div class="ch">
    <h2 id="h-sys">System</h2>
    <span class="cd">Device health &amp; controls</span>
  </div>
  <div class="cb">
    <div id="sys-inf" style="font-size:12px;color:var(--t3);margin-bottom:10px" role="status" aria-live="polite">Loading…</div>
    <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2);cursor:pointer;margin-bottom:10px">
      <input type="checkbox" id="led-enabled" checked onchange="toggleLeds()">
      Onboard LEDs (activity/power lights) on
    </label>
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

<!-- Config Backup -->
<section class="card full" aria-labelledby="h-bk">
  <div class="ch">
    <h2 id="h-bk">Config backup &amp; restore</h2>
    <span class="cd">Save settings before reflashing, restore after reinstall</span>
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

<!-- Log Viewer -->
<section class="card full" aria-labelledby="h-log">
  <div class="ch">
    <h2 id="h-log">Logs</h2>
    <span class="cd">View system logs without SSH (last 50 lines)</span>
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
const EDID_PROFS = {{ edid_profiles|tojson }};
const MAC_PROFS = {{ mac_profiles|tojson }};
let selP = 0;
let selE = 0;
let selM = -1;  // -1 = plain random, no vendor OUI

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

/* Idle timer */
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

/* USB profiles */
function buildPills() {
  const c = document.getElementById('pills');
  c.innerHTML = '';
  PROFS.forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'pill' + (i === selP ? ' on' : '');
    b.setAttribute('aria-pressed', i === selP ? 'true' : 'false');
    b.textContent = p.name + (p.verified ? '' : ' · unverified');
    b.title = p.verified
      ? 'Verified against a real device’s USB descriptor.'
      : 'VID/PID are real (researched against public USB-ID/driver databases), but the interface count and serial presence have not been confirmed against the physical dongle.';
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

/* Display identity (EDID) */
function buildEdidPills() {
  const c = document.getElementById('edid-pills');
  if (!c) return;
  c.innerHTML = '';
  EDID_PROFS.forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'pill' + (i === selE ? ' on' : '');
    b.setAttribute('aria-pressed', i === selE ? 'true' : 'false');
    b.textContent = p.name;
    b.onclick = () => {
      selE = i; buildEdidPills();
      document.getElementById('e-mfr').value  = p.mfr;
      document.getElementById('e-name').value = p.product_name;
      document.getElementById('e-pid').value  = '0x' + p.product_id.toString(16).toUpperCase();
    };
    c.appendChild(b);
  });
}

function _edidNote(r) {
  if (r.applied_live) return 'Applied to capture device ('+r.device+')';
  return r.reason || 'Saved (hardware pending)';
}

async function applyEdidPreset() {
  const r = await api('/stealth/api/apply', {action:'edid_profile', idx:selE});
  toast(r.ok ? _edidNote(r) : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadStatus();
}

async function applyEdidCustom() {
  const r = await api('/stealth/api/apply', {
    action:'edid_identity',
    mfr: document.getElementById('e-mfr').value,
    product_name: document.getElementById('e-name').value,
    product_id: document.getElementById('e-pid').value,
    serial: document.getElementById('e-ser').value,
  });
  toast(r.ok ? _edidNote(r) : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadStatus();
}

async function randEdidSerial() {
  const s = 'MB' + Math.random().toString(16).slice(2, 10).toUpperCase();
  document.getElementById('e-ser').value = s;
}

async function resetEdid() {
  const r = await api('/stealth/api/apply', {action:'edid_reset'});
  toast(r.ok ? 'Display identity reset' : (r.error||'Error'), r.ok?'ok':'er');
  if (r.ok) loadStatus();
}

async function safeMode() {
  const r = await api('/stealth/api/apply', {action:'safe_mode'});
  toast(r.ok ? (r.safe ? 'Safe mode ON' : 'Safe mode OFF') : 'Error', r.ok?'ok':'er');
  const b = document.getElementById('safe-btn');
  if (b) b.textContent = r.safe ? 'Exit safe mode' : 'Safe mode';
}

/* MAC */
async function applyMac() {
  const r = await api('/stealth/api/apply', {
    action:'mac',
    iface: document.getElementById('net-if').value,
    mac:   document.getElementById('net-mac').value,
  });
  toast(r.ok ? 'MAC applied' : (r.error||'Error'), r.ok?'ok':'er');
}

function buildMacPills() {
  const c = document.getElementById('mac-pills');
  if (!c) return;
  c.innerHTML = '';
  const opts = [{name:'Random (no vendor)'}].concat(MAC_PROFS);
  opts.forEach((p, i) => {
    const idx = i - 1;  // -1 for the leading "Random" option
    const b = document.createElement('button');
    b.className = 'pill' + (idx === selM ? ' on' : '');
    b.setAttribute('aria-pressed', idx === selM ? 'true' : 'false');
    b.textContent = p.name;
    b.onclick = () => { selM = idx; buildMacPills(); };
    c.appendChild(b);
  });
}

async function randMac() {
  const iface = document.getElementById('net-if').value;
  const body = {action:'rand_mac', iface};
  if (selM >= 0) body.vendor_idx = selM;
  const r = await api('/stealth/api/apply', body);
  if (r.mac) { document.getElementById('net-mac').value = r.mac; toast('MAC: '+r.mac); }
}

/* Tailscale */
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
    el.innerHTML = '<span class="dot d-ok"></span>Active: <a href="'+r.url+'" target="_blank" style="color:var(--ac)">'+r.url+'</a>';
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

/* DuckDNS */
async function applyDdns() {
  const r = await api('/stealth/api/apply', {
    action:'duckdns',
    host:  document.getElementById('ddns-h').value,
    token: document.getElementById('ddns-t').value,
  });
  const el = document.getElementById('ddns-st');
  el.textContent = r.ok ? 'Updated, IP: '+(r.ip||'?') : (r.error||'Failed');
  el.style.color  = r.ok ? 'var(--ok)' : 'var(--er)';
  toast(r.ok ? 'DuckDNS updated' : 'DuckDNS failed', r.ok?'ok':'er');
}

/* Status / Stats */
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
  const ed = r.edid || {};
  document.getElementById('e-mfr').value  = ed.mfr || '';
  document.getElementById('e-name').value = ed.product_name || '';
  document.getElementById('e-pid').value  = ed.product_id ? ('0x'+Number(ed.product_id).toString(16).toUpperCase()) : '';
  document.getElementById('e-ser').value  = ed.serial || '';
  const hw = r.edid_hw || {};
  const hwEl = document.getElementById('edid-hw-st');
  if (hwEl) {
    hwEl.innerHTML = hw.ready
      ? '<span class="dot d-ok"></span>C790/TC358743 detected - identity applies live.'
      : '<span class="dot d-er"></span>'+(hw.reason || 'Hardware pending')+' Settings save now and apply automatically once ready.';
  }
  const ledChk = document.getElementById('led-enabled');
  if (ledChk) ledChk.checked = r.leds_enabled !== false;
}

async function toggleLeds() {
  const enabled = document.getElementById('led-enabled').checked;
  const r = await api('/stealth/api/apply', {action:'leds', enabled});
  toast(r.ok ? ('LEDs ' + (enabled ? 'on' : 'off')) : (r.error||'Error'), r.ok?'ok':'er');
  if (!r.ok) document.getElementById('led-enabled').checked = !enabled;  // revert on failure
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

/* WiFi */
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

/* Logs */
async function refreshLogs() {
  const src = document.getElementById('log-src').value;
  const r   = await api('/stealth/api/logs?source='+src);
  document.getElementById('log-view').textContent = r.content || '(empty)';
}

/* Backup */
function dlBackup() { location.href = '/stealth/api/backup'; }

async function ulRestore(input) {
  const f = input.files[0];
  if (!f) return;
  let d;
  try { d = JSON.parse(await f.text()); } catch { toast('Invalid JSON', 'er'); return; }
  const r = await api('/stealth/api/restore', d);
  toast(r.ok ? 'Restored, reload page' : (r.error||'Error'), r.ok?'ok':'er');
}

/* Reboot */
async function doReboot() {
  if (!confirm('Reboot? Active KVM session will be interrupted.')) return;
  await api('/stealth/api/apply-reboot', {});
  toast('Rebooting…');
}

/* Change password */
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

/* Init */
buildPills();
buildEdidPills();
buildMacPills();
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

# Routes

@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = _load()
    _ensure_defaults(cfg)
    _purge_old_logs_if_due()
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
                 "vid":p["vid"],"pid":p["pid"],"verified":p["verified"]} for p in USB_PROFILES]
    edid_profiles = [{"name":p["name"],"mfr":p["mfr"],"product_name":p["product_name"],
                       "product_id":p["product_id"]} for p in EDID_PROFILES]
    mac_profiles = [{"name": p["name"]} for p in MAC_PROFILES]
    return render_template_string(MAIN_HTML, csrf=session.get("csrf",""),
                                   profiles=profiles, edid_profiles=edid_profiles,
                                   mac_profiles=mac_profiles)


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
        "edid":        {**dict(EDID_DEFAULTS), **cfg.get("edid", {})},
        "edid_hw":     (lambda r: {"ready": r[0], "reason": r[1]})(_edid_hw_ready(cfg)),
        "leds_enabled": cfg.get("leds_enabled", True),
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
    cfg = _load()
    idx = cfg.get("usb", {}).get("profile_idx", 0)
    p = USB_PROFILES[idx] if 0 <= idx < len(USB_PROFILES) else USB_PROFILES[0]
    if not p.get("has_serial", True):
        return jsonify({"ok": False,
                         "error": f"{p['name']} doesn't have a serial number on real "
                                  f"hardware - adding one would be less realistic, not more."}), 400
    ser = _rand_serial(p.get("pfx", "MB"))
    _usb_w("strings/0x409/serialnumber", ser)
    cfg.setdefault("usb", {})["serial"] = ser
    _save(cfg)
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
    auth = cfg.setdefault("auth", {})
    auth["password_hash"] = _hash_pw(pw)

    # Rotate the Flask session-signing secret so any other already-issued
    # session cookie stops validating immediately. app.secret_key is only
    # read once at boot, so it's updated live here too, not just on disk.
    # The session is re-touched (not re-valued) below so Flask re-signs
    # this browser's cookie with the new secret instead of logging it out;
    # the CSRF value itself is left untouched since the page's already-loaded
    # <meta name="csrf-token"> would otherwise go stale mid-session.
    new_secret = secrets.token_hex(32)
    auth["secret_key"] = new_secret
    _save(cfg)
    app.secret_key = new_secret
    session["ok"] = True
    session["t"]  = time.time()

    _log_sess(f"Password changed by {_client_ip()}, other sessions invalidated")
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
            has_ser = p.get("has_serial", True)
            # Stable per-Pi/per-profile, not freshly randomized on every
            # click - see _gen_profile_serial() docstring.
            ser = _gen_profile_serial(idx, p["pfx"]) if has_ser else ""
            _apply_usb(p["mfr"], p["prod"], ser, p["vid"], p["pid"],
                       has_serial=has_ser, extra_iface=p.get("extra_iface", False))
            # Reload fresh (don't reuse the cfg loaded at the top of this
            # request) so we add profile_idx without clobbering what
            # _apply_usb just persisted.
            cfg = _load()
            cfg.setdefault("usb", {})["profile_idx"] = idx
            _save(cfg)
            _log_sess(f"USB profile: {p['name']}")
            return jsonify({"ok": True})

        elif act == "edid_profile":
            idx = int(d.get("idx", 0))
            if not 0 <= idx < len(EDID_PROFILES):
                return jsonify({"error": "Bad index"}), 400
            p = EDID_PROFILES[idx]
            ser = _rand_serial(p["serial_prefix"])
            result = _apply_edid(p["mfr"], p["product_name"], p["product_id"], ser, profile_idx=idx)
            _log_sess(f"Display identity: {p['name']}"
                      + ("" if result.get("applied_live") else " (saved, hardware pending)"))
            return jsonify(result)

        elif act == "edid_identity":
            mfr = str(d.get("mfr", "")).strip()
            product_name = str(d.get("product_name", "")).strip()[:12]
            try:
                product_id = int(str(d.get("product_id", 0)).strip() or "0", 0) & 0xFFFF
            except (TypeError, ValueError):
                return jsonify({"error": "Product ID must be a number (e.g. 0xA06B)"}), 400
            ser = str(d.get("serial", "")).strip() or _rand_serial("MB")
            try:
                import mb_edidconf as _mec
                _mec.encode_pnp_id(mfr)  # validate 3-letter PNP id before saving
            except Exception as ex:
                return jsonify({"error": f"Invalid manufacturer ID: {ex}"}), 400
            result = _apply_edid(mfr, product_name, product_id, ser, profile_idx=None)
            _log_sess(f"Display identity (custom): {mfr}/{product_name}"
                      + ("" if result.get("applied_live") else " (saved, hardware pending)"))
            return jsonify(result)

        elif act == "leds":
            enabled = bool(d.get("enabled", True))
            result = _apply_leds(enabled)
            _log_sess(f"Onboard LEDs: {'on' if enabled else 'off'}")
            return jsonify(result)

        elif act == "edid_reset":
            cfg2 = _load()
            cfg2["edid"] = dict(EDID_DEFAULTS)
            _save(cfg2)
            _log_sess("Display identity reset to default")
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
            idx = d.get("vendor_idx")
            oui = None
            vendor_name = "random"
            if idx is not None:
                idx = int(idx)
                if not 0 <= idx < len(MAC_PROFILES):
                    return jsonify({"error": "Bad vendor index"}), 400
                oui = MAC_PROFILES[idx]["oui"]
                vendor_name = MAC_PROFILES[idx]["name"]
            mac = _rand_mac(oui)
            _set_mac(iface, mac)
            # Persist immediately, same as the manual "mac" action above -
            # previously this only took effect live and reverted on reboot
            # unless the user separately re-applied it via the manual field.
            _persist_mac(iface, mac)
            _log_sess(f"MAC randomized {iface}: {mac} ({vendor_name})")
            return jsonify({"ok": True, "mac": mac})

        elif act == "safe_mode":
            in_safe = cfg.get("safe_mode", False)
            if not in_safe:
                ser = _gen_default_serial() if ORIG.get("has_serial", True) else ""
                _apply_usb(ORIG["manufacturer"], ORIG["product"], ser,
                           ORIG["idVendor"], ORIG["idProduct"],
                           has_serial=ORIG.get("has_serial", True),
                           extra_iface=ORIG.get("extra_iface", False))
                new_safe = True
            else:
                idx = cfg.get("usb", {}).get("profile_idx", 0)
                if 0 <= idx < len(USB_PROFILES):
                    p = USB_PROFILES[idx]
                    has_ser = p.get("has_serial", True)
                    # Restore the SAME stable serial this profile always
                    # gets, not a freshly randomized one - exiting safe mode
                    # should look like "the same device reconnected", not
                    # "a different device with the same name showed up".
                    _apply_usb(p["mfr"], p["prod"], _gen_profile_serial(idx, p["pfx"]) if has_ser else "",
                               p["vid"], p["pid"], has_serial=has_ser,
                               extra_iface=p.get("extra_iface", False))
                new_safe = False
            # Reload fresh so we only touch safe_mode, not the usb section
            # _apply_usb just persisted.
            cfg = _load()
            cfg["safe_mode"] = new_safe
            _save(cfg)
            _log_sess(f"Safe mode: {new_safe}")
            return jsonify({"ok": True, "safe": new_safe})

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
            return jsonify({"ok": False, "error": "DuckDNS update failed, check hostname and token"})

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


# WiFi API. These used to be reachable unauthenticated via an nginx
# /api/wifi/ passthrough intended for the main KVM page - that page has
# since moved to its own authenticated /api/networks endpoint in
# magicbridge.py, so the passthrough was pure dead weight left open to
# anyone who could reach the Pi's HTTPS port with no login at all (add/
# remove/force-connect WiFi networks, no credentials required). The nginx
# location has been removed and every route below now requires an active
# stealth-panel session, matching status-auth/psk-auth's existing pattern.
# Kept at these same paths (not renamed) since the stealth panel's own JS
# (loadWifiStatus/loadSavedWifi/addWifi/removeWifi/connectWifi) already
# calls them through /stealth/api/wifi/... and already sends the CSRF
# header on every request via the shared api() helper.

def _nm(*args, timeout=15):
    return subprocess.run(["nmcli"] + list(args),
                          capture_output=True, text=True, timeout=timeout)


@app.route("/api/wifi/status")
def api_wifi_status():
    if not _authed(): return jsonify({"error":"auth"}), 401
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
    if not _authed(): return jsonify({"error":"auth"}), 401
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
    if not _authed(): return jsonify({"error":"auth"}), 401
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
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
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
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
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
    if not _authed(): return jsonify({"error":"auth"}), 401
    if not _csrf_ok(): return jsonify({"error":"csrf"}), 403
    d    = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"ok":False,"error":"name required"}), 400
    r = _nm("connection","up",name,timeout=25)
    if r.returncode == 0:
        _log_sess(f"WiFi connect: {name}")
        return jsonify({"ok":True})
    err = (r.stderr or r.stdout).strip()
    return jsonify({"ok":False,"error":err[:120]})


# Stealth-panel-proxied WiFi routes (called from stealth panel JS)

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


# Boot

_boot()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7777, debug=False, use_reloader=False)