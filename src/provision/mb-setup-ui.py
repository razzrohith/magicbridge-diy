#!/usr/bin/env python3
"""MagicBridge WiFi provisioning captive portal.
Usage: mb-setup-ui.py <bind_ip> <port> <wifi_file> <ts_key_file>
Blocks until user submits WiFi credentials, then exits.
"""
import sys, os, json, threading, time, html
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

BIND_IP    = sys.argv[1] if len(sys.argv) > 1 else "192.168.73.1"
PORT       = int(sys.argv[2]) if len(sys.argv) > 2 else 80
WIFI_FILE  = sys.argv[3] if len(sys.argv) > 3 else "/tmp/mb-provision-wifi"
TS_FILE    = sys.argv[4] if len(sys.argv) > 4 else "/tmp/mb-ts-key"

_done  = threading.Event()
_server = None

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Device Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;background:#060606;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#ddd}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem}
.card{background:#0f0f0f;border:0.5px solid #1c1c1c;border-radius:12px;
      padding:2rem;width:100%;max-width:360px}
h1{font-size:18px;font-weight:600;margin-bottom:4px;color:#fff}
.sub{font-size:12px;color:#9aa4b0;margin-bottom:1.5rem}
label{display:block;font-size:11px;color:#c2cad6;margin-bottom:3px;margin-top:10px}
label:first-of-type{margin-top:0}
input{width:100%;padding:9px 11px;background:#080808;
      border:0.5px solid #2a2a2a;border-radius:7px;
      color:#f0f3f7;font-size:13px;outline:none;transition:border .15s}
input:focus{border-color:#4a9eff;box-shadow:0 0 0 2px rgba(74,158,255,.1)}
details{margin-top:14px;font-size:12px;color:#9aa4b0}
summary{cursor:pointer;color:#9aa4b0;padding:4px 0}
summary:hover{color:#c8d0da}
button{margin-top:1rem;width:100%;padding:10px;
       background:#4a9eff;border:none;border-radius:7px;
       color:#fff;font-size:13px;font-weight:500;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.83}
.msg{margin-top:.7rem;padding:8px 10px;border-radius:6px;font-size:12px}
.ok{background:rgba(76,190,130,.08);border:0.5px solid rgba(76,190,130,.3);color:#4cbe82}
.er{background:rgba(224,80,80,.08);border:0.5px solid rgba(224,80,80,.3);color:#e05050}
.hint{margin-top:1rem;font-size:11px;color:#8b949e;text-align:center;line-height:1.6}
</style>
</head>
<body>
<main>
<div class="card">
  <h1>Device Setup</h1>
  <p class="sub">Connect this device to your WiFi network to complete setup</p>
  MSGBLOCK
  <form method="POST" action="/setup">
    <label for="ssid">Network name (SSID)</label>
    <input type="text" id="ssid" name="ssid" required
           placeholder="Your WiFi network name" autocomplete="off">
    <label for="pass">Password (leave blank for open network)</label>
    <input type="password" id="pass" name="pass"
           placeholder="WiFi password" autocomplete="off">
    <details>
      <summary>Use it away from home (optional, advanced)</summary>
      <div style="margin-top:6px">
        <label for="tskey">Tailscale auth key</label>
        <input type="text" id="tskey" name="tskey"
               placeholder="tskey-auth-..." autocomplete="off">
        <p style="margin-top:5px;font-size:11px;color:#9aa4b0;line-height:1.5">
          Leave this blank to use MagicBridge on your home WiFi. To reach it from
          anywhere later, you can turn on secure remote access from the Network
          tab. (Advanced: paste a key from tailscale.com/admin/settings/keys.)
        </p>
      </div>
    </details>
    <button type="submit">Connect &amp; Complete Setup</button>
  </form>
  <p class="hint">
    After connecting, this device finishes setup automatically.<br>
    Reach it at the address shown on its small screen, or via Tailscale.
  </p>
</div>
</main>
</body>
</html>"""

SUCCESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connected</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;background:#060606;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#ddd}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem}
.card{background:#0f0f0f;border:0.5px solid #1c1c1c;border-radius:12px;
      padding:2rem;width:100%;max-width:340px;text-align:center}
.icon{font-size:40px;margin-bottom:12px}
h1{font-size:16px;font-weight:600;color:#4cbe82;margin-bottom:8px}
p{font-size:12.5px;color:#aab3c0;line-height:1.7;margin-bottom:.6rem}
strong{color:#e6edf3}
.addr{display:block;font-size:17px;color:#4a9eff;font-weight:600;margin:6px 0;word-break:break-all}
.note{font-size:11.5px;color:#8b949e}
</style>
</head>
<body>
<main>
<div class="card">
  <div class="icon">✓</div>
  <h1>Got it. Connecting now.</h1>
  <p>
    We are joining <strong>SSID_PLACEHOLDER</strong>. Give it about 2 minutes.
    This setup network will disappear shortly, which is normal.
  </p>
  <p>
    Reconnect your phone to your main WiFi, then open this address and
    <strong>save it</strong> (it is also shown on the device's small screen):
  </p>
  <span class="addr">https://HOSTNAME_PLACEHOLDER.local/</span>
  <p class="note">
    The first time you open it, your browser may say the connection is
    "not private." That is expected for a device on your own network:
    tap <strong>Advanced</strong>, then <strong>Continue / Proceed</strong>.
  </p>
  <p class="note">
    If the device does not come online, the setup network you just joined
    (its name starts with <strong>Setup-</strong>) will reappear. Rejoin it and
    enter your WiFi again, double-checking the password.
  </p>
</div>
</main>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress access log spam

    def _send(self, code, body, ct="text/html; charset=utf-8", extra_headers=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(b))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, location="/"):
        # Location header must be sent BEFORE end_headers(). Sending it after
        # (as this used to) is a no-op, so captive-portal auto-detection on some
        # OSes never got told where to go.
        self._send(302, b"", extra_headers={"Location": location})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/generate_204", "/hotspot-detect.html",
                           "/connecttest.txt", "/success.txt", "/ncsi.txt"):
            self._send(200, HTML.replace("MSGBLOCK", ""))
        else:
            self._redirect("/")

    def do_POST(self):
        if self.path != "/setup":
            self._redirect("/"); return
        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length).decode()
        params  = parse_qs(raw)
        # .strip() only trims the ends; an SSID/password containing an
        # embedded newline (WiFi SSIDs allow almost any byte) would still
        # corrupt WIFI_FILE's line-based format, which mb-provision.sh reads
        # back with `sed -n '1p'`/`'2p'` - an embedded \n in the SSID would
        # push everything after it onto what mb-provision.sh treats as the
        # password line. Stripped here since a real SSID/password legitimately
        # containing a newline isn't a case worth supporting.
        ssid    = params.get("ssid", [""])[0].strip().replace("\n", "").replace("\r", "")
        pw      = params.get("pass", [""])[0].replace("\n", "").replace("\r", "")
        tskey   = params.get("tskey", [""])[0].strip()
        if not ssid:
            page = HTML.replace("MSGBLOCK",
                '<div class="msg er">SSID is required.</div>')
            self._send(400, page); return
        # Write wifi credentials. 0600: this file holds the target LAN's WiFi PSK
        # in plaintext. A plain open() creates it 0644 in a 0755 dir, so the
        # non-root account could read the secret off disk. os.open sets 0600 on
        # create; the fchmod tightens it even if a stale file pre-existed at 0644.
        # mb-provision.sh reads it back as root, so nothing else needs to change.
        try:
            fd = os.open(WIFI_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(ssid + "\n")
                f.write(pw + "\n")
        except Exception as e:
            page = HTML.replace("MSGBLOCK",
                f'<div class="msg er">Error: {html.escape(str(e))}</div>')
            self._send(500, page); return
        # Write TS key if given
        if tskey:
            try:
                # 0600, like WIFI_FILE above. A bare open() creates 0644, and on
                # this image /tmp is on the ROOTFS rather than a tmpfs - so a
                # Tailscale auth key, which is enough to join the owner's tailnet,
                # was left world-readable on disk and survived a reboot.
                fd = os.open(TS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(tskey)
            except Exception:
                pass
        # html.escape: ssid is attacker-controllable (it's a WiFi network
        # name, chosen by whoever set up that network, not necessarily the
        # person using this portal) and was previously substituted into the
        # response page raw - a network named e.g. "<script>...</script>"
        # would have executed in the setup browser. Real, if narrow, XSS.
        # Tell the owner the ADDRESS here, on their phone, while they are still
        # holding it. The page used to say "open the address shown on the
        # device's small screen", which assumes an OLED - a unit built without
        # one, or whose panel has failed, left the owner with no way at all to
        # find the device. This costs nothing in stealth: the hostname is
        # already broadcast by DHCP and mDNS regardless, and it deliberately
        # reads as an ordinary PC (DESKTOP-XXXXXXX), unlike the "magicbridge"
        # alias which is disabled in shipped images for exactly that reason.
        try:
            import socket as _sock
            _hn = _sock.gethostname().split(".")[0] or "magicbridge"
        except Exception:
            _hn = "magicbridge"
        page = (SUCCESS_HTML.replace("SSID_PLACEHOLDER", html.escape(ssid))
                            .replace("HOSTNAME_PLACEHOLDER", html.escape(_hn)))
        self._send(200, page)
        # Signal main thread to stop
        threading.Timer(1.5, _done.set).start()


def run():
    global _server
    _server = HTTPServer((BIND_IP, PORT), Handler)
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()
    print(f"[mb-setup-ui] Captive portal listening on {BIND_IP}:{PORT}", flush=True)
    _done.wait()
    print("[mb-setup-ui] WiFi credentials received, shutting down portal", flush=True)
    _server.shutdown()


if __name__ == "__main__":
    run()
