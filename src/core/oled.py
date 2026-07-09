#!/usr/bin/env python3
"""
MagicBridge - OLED status display (SSD1306, I2C).

Small always-on status readout for the 0.91" SSD1306 panel in the case:
IP address, CPU temp, and whether the video stream / main service are
actually alive. Deliberately a SEPARATE process/service from magicbridge.py
(mb-oled.service, not imported by the main backend) for two reasons:

  1. It has its own hardware dependency (I2C + the physical panel) that may
     not be present yet - it should be able to fail/retry on its own without
     touching the KVM service at all.
  2. Reading system state directly (not via an authenticated HTTP call to
     /api/status) means the display keeps working even if magicbridge.py
     itself is down/restarting - useful for a physical "is this Pi alive"
     readout, which is the whole point of having a screen on the box.

Hardware: SSD1306, I2C, default address 0x3C, assumed 128x32 (the common
0.91" panel size) - change OLED_WIDTH/OLED_HEIGHT below if a 128x64 panel
is used instead. Needs `dtparam=i2c_arm=on` in /boot/firmware/config.txt
and the `luma.oled` Python package (`pip install luma.oled` or
`apt install python3-luma.oled` if packaged).

Safe to install/run before the display is physically wired: init failures
are logged once (not spammed every loop) and retried on an interval rather
than crashing the service.

Display content is configurable from the web UI (System tab -> OLED
Display), stored in the same shared /etc/magicbridge/config.json used by
the rest of the backend, under an "oled" key. This file is polled (cheap
mtime check) every loop so changes made in the UI apply live within one
refresh cycle - no service restart needed. If the config file or key is
missing/corrupt, falls back to DEFAULT_CFG (which reproduces the original
static 3-line layout exactly), so a bad edit can never brick the display.
"""
import json
import logging
import subprocess
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-16s %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("magicbridge.oled")

OLED_I2C_PORT = 1        # /dev/i2c-1, standard on all Pi models
OLED_I2C_ADDR = 0x3C     # SSD1306 default; some panels are 0x3D
OLED_WIDTH    = 128
OLED_HEIGHT   = 32       # 0.91" panels are 128x32; set to 64 for 128x64 panels
RETRY_SEC     = 15       # how often to retry initializing the display if it's not found yet

# 3-line layout (the original/default) uses PIL's plain default font, which
# measures ~10-12px tall per line - 3 of those already fill all 32px of a
# 128x32 panel edge to edge. There is no spare room to add a 4th line at
# that size. When line4 is enabled, we switch ALL FOUR lines to a smaller
# font instead, so 4 rows fit without clipping. Verified by measuring actual
# glyph bounding boxes with Pillow: size=8 gives ~7px ink height, and 4 rows
# spaced 8px apart (0/8/16/24) fit cleanly inside 32px.
FONT_SIZE_SMALL = 8
LINE_Y_NORMAL = (0, 10, 20)       # 3-line layout (default)
LINE_Y_SMALL  = (0, 8, 16, 24)    # 4-line layout (opt-in, smaller font)

CONFIG_PATH = "/etc/magicbridge/config.json"   # shared with magicbridge.py

# Mirrors magicbridge.py's OLED_DEFAULTS - kept in sync manually since these
# are two separate processes/files. Reproduces the exact original static
# layout: "MagicBridge" / IP / "{temp}C up{uptime} {OK/DOWN}/{LIVE/OFF}".
DEFAULT_CFG = {
    "enabled": True,               # master on/off - mirrors magicbridge.py's
                                    # OLED_DEFAULTS; False blanks the panel
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
    # Opt-in 4th line - off by default (see FONT_SIZE_SMALL note above for
    # why this isn't just "add a row for free").
    "line4_enabled": False,
    "line4_mode": "blank",        # blank | hostname | tailscale | custom
    "line4_custom": "",
    "refresh_sec": 2,
}

_cfg = dict(DEFAULT_CFG)
_cfg_mtime = None


def _load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text())
        oled_cfg = raw.get("oled", {})
        cfg.update({k: v for k, v in oled_cfg.items() if k in DEFAULT_CFG})
    except Exception:
        # Missing/corrupt file or key - fall back to defaults rather than
        # crash. This is normal on a fresh install before any settings save.
        pass
    return cfg


def _maybe_reload_config():
    global _cfg, _cfg_mtime
    try:
        mtime = Path(CONFIG_PATH).stat().st_mtime
    except Exception:
        mtime = None
    if mtime != _cfg_mtime:
        _cfg = _load_config()
        _cfg_mtime = mtime
        log.info("OLED config (re)loaded: %s", _cfg)


def _read_temp():
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:
        return None


def _read_ip():
    try:
        ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.strip().split()
        return ip[0] if ip else "no IP"
    except Exception:
        return "no IP"


def _read_hostname():
    try:
        return subprocess.run(["hostname"], capture_output=True, text=True, timeout=2).stdout.strip() or "?"
    except Exception:
        return "?"


def _read_tailscale_ip():
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2)
        ip = r.stdout.strip().splitlines()
        return ip[0] if ip and r.returncode == 0 else None
    except Exception:
        return None


def _read_uptime():
    try:
        s = int(float(Path("/proc/uptime").read_text().split()[0]))
        d, r = divmod(s, 86400); h, r = divmod(r, 3600); m = r // 60
        return "".join([f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m"]) or "0m"
    except Exception:
        return "?"


def _magicbridge_alive():
    try:
        r = subprocess.run(["systemctl", "is-active", "magicbridge"],
                            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _stream_alive():
    # Cheap check that doesn't need authenticated access to magicbridge's
    # own API: ustreamer listens on 127.0.0.1:8081 whenever a stream (either
    # mjpeg or h264 mode) is actually running.
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "1", "http://127.0.0.1:8081/"],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() not in ("", "000")
    except Exception:
        return False


def _build_line1(cfg):
    mode = cfg.get("line1_mode", "app")
    if mode == "custom" and cfg.get("line1_custom"):
        return str(cfg["line1_custom"])[:21]
    if mode == "hostname":
        return _read_hostname()
    return "MagicBridge"


def _build_line2(cfg):
    mode = cfg.get("line2_mode", "ip")
    if mode == "custom" and cfg.get("line2_custom"):
        return str(cfg["line2_custom"])[:21]
    if mode == "blank":
        return ""
    if mode == "tailscale":
        ip = _read_tailscale_ip()
        return ip or "TS: not connected"
    return _read_ip()


def _build_line4(cfg):
    mode = cfg.get("line4_mode", "blank")
    if mode == "custom" and cfg.get("line4_custom"):
        return str(cfg["line4_custom"])[:21]
    if mode == "hostname":
        return _read_hostname()
    if mode == "tailscale":
        ip = _read_tailscale_ip()
        return ip or "TS: not connected"
    return ""


def _build_line3(cfg, temp, uptime, mb_ok, stream_ok):
    if cfg.get("line3_custom_enabled") and cfg.get("line3_custom"):
        return str(cfg["line3_custom"])[:21]
    parts = []
    if cfg.get("line3_show_temp", True):
        parts.append(f"{temp}C" if temp is not None else "--C")
    if cfg.get("line3_show_uptime", True):
        parts.append(f"up{uptime}")
    svc_bits = []
    if cfg.get("line3_show_service", True):
        svc_bits.append("OK" if mb_ok else "DOWN")
    if cfg.get("line3_show_stream", True):
        svc_bits.append("LIVE" if stream_ok else "OFF")
    if svc_bits:
        parts.append("/".join(svc_bits))
    return " ".join(parts)


def _init_display():
    # Imports live here (not at module level) deliberately: if luma.oled/
    # Pillow aren't installed yet (pip install luma.oled), that's treated
    # exactly like "panel not physically wired yet" - retry-and-log-once,
    # not a crash-loop. Same reasoning as the hardware-not-present case.
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306
    serial = i2c(port=OLED_I2C_PORT, address=OLED_I2C_ADDR)
    return ssd1306(serial, width=OLED_WIDTH, height=OLED_HEIGHT)


def main():
    device = None
    font_normal = None
    font_small = None
    last_init_attempt = 0.0
    last_init_error = None

    _maybe_reload_config()
    log.info("MagicBridge OLED display starting (I2C addr=0x%02X, %dx%d)",
             OLED_I2C_ADDR, OLED_WIDTH, OLED_HEIGHT)
    log.info("If luma.oled/Pillow aren't installed yet: pip install luma.oled --break-system-packages")

    while True:
        _maybe_reload_config()

        if device is None:
            now = time.time()
            if now - last_init_attempt >= RETRY_SEC:
                last_init_attempt = now
                try:
                    device = _init_display()
                    from PIL import ImageFont
                    font_normal = ImageFont.load_default()
                    try:
                        font_small = ImageFont.load_default(size=FONT_SIZE_SMALL)
                    except TypeError:
                        # Older Pillow without the size= kwarg on load_default():
                        # fall back to the normal font. 4-line mode will look
                        # cramped/overlapping in that case, but won't crash.
                        font_small = font_normal
                    log.info("OLED panel found and initialized")
                except Exception as e:
                    if str(e) != last_init_error:
                        # Only log the first occurrence of a given error, then
                        # go quiet - this covers both "library not installed"
                        # and "panel not physically wired yet", both of which
                        # are expected/normal until setup is complete, not
                        # worth spamming the journal every retry.
                        log.info("OLED not ready yet (%s) - retrying every %ds", e, RETRY_SEC)
                        last_init_error = str(e)
            time.sleep(1)
            continue

        if not _cfg.get("enabled", True):
            # Master off-switch: blank the panel and idle without tearing
            # down the initialized device, so re-enabling is instant (no
            # re-probe of the I2C bus) - just resumes rendering next loop.
            try:
                device.clear()
            except Exception:
                pass
            time.sleep(1)
            continue

        try:
            from luma.core.render import canvas
        except Exception as e:
            log.warning("luma.core.render import failed after successful init (%s) - re-checking", e)
            device = None
            continue

        try:
            temp = _read_temp()
            uptime = _read_uptime()
            mb_ok = _magicbridge_alive()
            stream_ok = _stream_alive()

            line1 = _build_line1(_cfg)
            line2 = _build_line2(_cfg)
            line3 = _build_line3(_cfg, temp, uptime, mb_ok, stream_ok)

            four_lines = bool(_cfg.get("line4_enabled"))
            with canvas(device) as draw:
                if four_lines:
                    line4 = _build_line4(_cfg)
                    ys = LINE_Y_SMALL
                    draw.text((0, ys[0]), line1, font=font_small, fill="white")
                    draw.text((0, ys[1]), line2, font=font_small, fill="white")
                    draw.text((0, ys[2]), line3, font=font_small, fill="white")
                    draw.text((0, ys[3]), line4, font=font_small, fill="white")
                else:
                    ys = LINE_Y_NORMAL
                    draw.text((0, ys[0]), line1, font=font_normal, fill="white")
                    draw.text((0, ys[1]), line2, font=font_normal, fill="white")
                    draw.text((0, ys[2]), line3, font=font_normal, fill="white")

            time.sleep(max(1, min(30, _cfg.get("refresh_sec", 2))))
        except Exception as e:
            # Display was working, then failed mid-loop (unplugged, I2C
            # error, etc.) - drop back to retry/init mode rather than
            # crashing the whole service.
            log.warning("OLED render error, will re-init: %s", e)
            device = None
            last_init_error = None
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
