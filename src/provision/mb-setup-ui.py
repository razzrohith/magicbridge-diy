#!/usr/bin/env python3
"""MagicBridge WiFi provisioning captive portal.
Usage: mb-setup-ui.py <bind_ip> <port> <wifi_file> <ts_key_file>
Blocks until user submits WiFi credentials, then exits.
"""
import sys, os, json, threading, time
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
<title>MagicBridge Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;background:#060606;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#ddd}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem}
.card{background:#0f0f0f;border:0.5px solid #1c1c1c;border-radius:12px;
      padding:2rem;width:100%;max-width:360px}
h1{font-size:18px;font-weight:600;margin-bottom:4px;color:#fff}
.sub{font-size:12px;color:#444;margin-bottom:1.5rem}
label{display:block;font-size:11px;color:#555;margin-bottom:3px;margin-top:10px}
label:first-of-type{margin-top:0}
input{width:100%;padding:9px 11px;background:#080808;
      border:0.5px solid #1c1c1c;border-radius:7px;
      color:#ddd;font-size:13px;outline:none;transition:border .15s}
input:focus{border-color:#4a9eff;box-shadow:0 0 0 2px rgba(74,158,255,.1)}
details{margin-top:14px;font-size:12px;color:#444}
summary{cursor:pointer;color:#333;padding:4px 0}
summary:hover{color:#555}
button{margin-top:1rem;width:100%;padding:10px;
       background:#4a9eff;border:none;border-radius:7px;
       color:#fff;font-size:13px;font-weight:500;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.83}
.msg{margin-top:.7rem;padding:8px 10px;border-radius:6px;font-size:12px}
.ok{background:rgba(76,190,130,.08);border:0.5px solid rgba(76,190,130,.3);color:#4cbe82}
.er{background:rgba(224,80,80,.08);border:0.5px solid rgba(224,80,80,.3);color:#e05050}
.hint{margin-top:1rem;font-size:11px;color:#2a2a2a;text-align:center;line-height:1.6}
</style>
</head>
<body>
<main>
<div class="card">
  <h1>MagicBridge</h1>
  <p class="sub">Connect to your WiFi network to complete setup</p>
  MSGBLOCK
  <form method="POST" action="/setup">
    <label for="ssid">Network name (SSID)</label>
    <input type="text" id="ssid" name="ssid" required
           placeholder="Your WiFi network name" autocomplete="off">
    <label for="pass">Password (leave blank for open network)</label>
    <input type="password" id="pass" name="pass"
           placeholder="WiFi password" autocomplete="off">
    <details>
      <summary>Tailscale auth key (optional)</summary>
      <div style="margin-top:6px">
        <label for="tskey">Auth key</label>
        <input type="text" id="tskey" name="tskey"
               placeholder="tskey-auth-..." autocomplete="off">
        <p style="margin-top:5px;font-size:11px;color:#333;line-height:1.5">
          Generate at tailscale.com/admin/settings/keys.
          Enables remote access immediately after setup.
        </p>
      </div>
    </details>
    <button type="submit">Connect &amp; Complete Setup</button>
  </form>
  <p class="hint">
    After connecting, MagicBridge will reboot into normal KVM mode.<br>
    Access it at <strong>http://magicbridge.local/</strong> or via Tailscale.
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
<title>MagicBridge Connected</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;background:#060606;
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#ddd}
body{display:flex;align-items:center;justify-content:center;padding:1.5rem}
.card{background:#0f0f0f;border:0.5px solid #1c1c1c;border-radius:12px;
      padding:2rem;width:100%;max-width:340px;text-align:center}
.icon{font-size:40px;margin-bottom:12px}
h1{font-size:16px;font-weight:600;color:#4cbe82;margin-bottom:8px}
p{font-size:12px;color:#444;line-height:1.7}
strong{color:#888}
</style>
</head>
<body>
<main>
<div class="card">
  <div class="icon">✓</div>
  <h1>Setup complete!</h1>
  <p>
    MagicBridge is connecting to <strong>SSID_PLACEHOLDER</strong>.<br>
    This AP will disappear in a few seconds.<br><br>
    Reconnect your device to your main network, then open:<br>
    <strong>http://magicbridge.local/</strong>
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
        ssid    = params.get("ssid", [""])[0].strip()
        pw      = params.get("pass", [""])[0]
        tskey   = params.get("tskey", [""])[0].strip()
        if not ssid:
            page = HTML.replace("MSGBLOCK",
                '<div class="msg er">SSID is required.</div>')
            self._send(400, page); return
        # Write wifi credentials
        try:
            with open(WIFI_FILE, "w") as f:
                f.write(ssid + "\n")
                f.write(pw + "\n")
        except Exception as e:
            page = HTML.replace("MSGBLOCK",
                f'<div class="msg er">Error: {e}</div>')
            self._send(500, page); return
        # Write TS key if given
        if tskey:
            try:
                with open(TS_FILE, "w") as f:
                    f.write(tskey)
            except Exception:
                pass
        page = SUCCESS_HTML.replace("SSID_PLACEHOLDER", ssid)
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
