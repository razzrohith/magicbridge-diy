# MagicBridge — Project Handbook
> Complete reference for development, debugging, and continuation.
> Last updated: July 2026

---

## 1. Project Overview

**MagicBridge** is a self-hosted KVM-over-IP system built on a Raspberry Pi 4. It lets you control a remote PC (keyboard, mouse, screen) entirely through a browser — no software installed on the target machine, no admin rights needed.

Think of it as a DIY [TinyPilot](https://tinypilotkvm.com/), but free, self-hosted, and with extra features like WiFi management, Tailscale VPN, AI-powered macro execution, clipboard sync, and a USB identity spoofer.

### Core Concept
```
[Target PC] ←USB HID (keyboard + mouse emulation)
     ↓
[Raspberry Pi 4]
  - Captures HDMI output via USB capture card
  - Emulates USB HID keyboard + mouse via USB gadget
  - Runs MagicBridge web server (Python/aiohttp + nginx)
     ↓
[Browser on any device] — accesses https://172.16.20.116/ or https://magicbridge.local/
```

---

## 2. Hardware

| Component | Detail |
|-----------|--------|
| Pi model | Raspberry Pi 4 (4GB or 8GB) |
| OS | Raspberry Pi OS Lite (64-bit) |
| USB capture card | Plugs into Pi USB 3.0 port; receives HDMI from target PC |
| USB cable (data) | Pi USB-C OTG port → Target PC USB-A; carries HID keyboard + mouse |
| HDMI cable | Target PC HDMI out → Capture card HDMI in |
| Power | Separate USB-C power supply to Pi |
| Network | Pi connected via WiFi (or Ethernet) to same LAN as controlling device |

### USB Gadget Mode
The Pi's USB-C port is configured as a USB gadget device, presenting itself to the target PC as a USB keyboard + mouse. This is done via the Linux kernel `dwc2` OTG driver and `libcomposite` gadget framework at `/sys/kernel/config/usb_gadget/`.

---

## 3. Pi — Network & Access

| Item | Value |
|------|-------|
| IP (primary) | `172.16.20.116` (DHCP, may change — check router if unreachable) |
| SSH user | `raj` |
| SSH password | `lol` |
| Sudo password | `lol` |
| Web UI (IP) | `https://172.16.20.116/` |
| Web UI (hostname) | `https://magicbridge.local/` |
| Web UI (alias) | `https://raj.local/` |
| Sudo syntax | `echo 'lol' \| sudo -S <command>` |

### Connecting from Windows (no Linux sandbox access)
The Linux sandbox **cannot reach 172.16.20.116** (no network). All Pi access must go through Windows Python + paramiko:

```python
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("172.16.20.116", username="raj", password="lol", timeout=15)
def run(cmd, t=20):
    _,o,e = ssh.exec_command(cmd, timeout=t)
    return o.read().decode(errors="replace"), e.read().decode(errors="replace")
def sudo(cmd, t=30):
    return run("echo 'lol' | sudo -S bash -c "+repr(cmd), t)
```

### Address Bar Trick (running scripts)
In File Explorer, navigate to `E:\Startup\magicbridge`, press **Alt+D**, type:
```
cmd /c python E:\Startup\magicbridge\<script>.py
```
Then press Enter. Check `<script>_log.txt` for output.

> **CRITICAL:** Never use `cmd /c python script.py > log.txt 2>&1` — the shell redirect conflicts with the script's own file writer, producing an empty log. Let the script write its own log internally.

### Deploying Large Files (SFTP, not base64)
For files > ~50KB (especially HTML), use SFTP instead of base64 echo:
```python
sftp = ssh.open_sftp()
sftp.putfo(io.BytesIO(content_bytes), "/tmp/file.html")
sftp.close()
sudo("cp /tmp/file.html /destination/path && chown www-data:www-data /destination/path")
```
Base64 echo has shell buffer limits that silently truncate large files.

---

## 4. Pi — File Locations

| File | Path |
|------|------|
| Main backend | `/opt/magicbridge/core/magicbridge.py` |
| Video controller | `/opt/magicbridge/core/video.py` |
| Web UI (HTML) | `/opt/magicbridge/web/index.html` |
| SSL cert | `/etc/magicbridge/ssl/cert.pem` |
| SSL key | `/etc/magicbridge/ssl/key.pem` |
| nginx config | `/etc/nginx/sites-enabled/magicbridge` |
| magicbridge service | `/etc/systemd/system/magicbridge.service` |
| ustreamer service | `/etc/systemd/system/ustreamer.service` |
| avahi raj.local alias | `/etc/systemd/system/avahi-raj.service` |
| Gadget setup | `/sys/kernel/config/usb_gadget/magicbridge/` |

> **TRAP:** `/opt/magicbridge/magicbridge.py` is EMPTY. The real backend is always `/opt/magicbridge/core/magicbridge.py`.

### Windows Development Files
All local dev files live in `E:\Startup\magicbridge\`. The deployed frontend is `index_v13.html` (latest version), copied to the Pi as `index.html`.

---

## 5. Services & Architecture

### Systemd Services
```bash
systemctl status magicbridge.service   # Main Python backend (aiohttp)
systemctl status ustreamer.service     # MJPEG video streamer
systemctl status nginx.service         # Reverse proxy (HTTPS + WebSocket)
systemctl status avahi-daemon.service  # mDNS (magicbridge.local)
systemctl status avahi-raj.service     # mDNS alias (raj.local)
```

### Port Layout (internal)
| Port | Service |
|------|---------|
| 80 | nginx → redirects to 443 |
| 443 | nginx (HTTPS, WSS, proxies to 7777) |
| 7777 | MagicBridge Python backend (aiohttp) |
| 8080 | ustreamer MJPEG stream |

### nginx Configuration (key blocks)
```nginx
location / { proxy_pass http://127.0.0.1:7777; }
location /stream { proxy_pass http://127.0.0.1:8080/stream; }
location /ws {
    proxy_pass http://127.0.0.1:7777/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

### WebSocket
- URL: `wss://<host>/ws`
- Used for: real-time keyboard/mouse input, status updates
- Auth: session token via cookie

---

## 6. SSL Certificate

Regenerated to include all required SANs (Chrome 149+ requires IP in SAN for WSS):

```
Subject: CN=magicbridge.local
SANs:
  DNS:magicbridge.local
  DNS:raj.local
  DNS:localhost
  IP:172.16.20.116
  IP:127.0.0.1
```

**Why this matters:** Chrome blocks script-initiated WSS connections to IPs not listed in the certificate's Subject Alternative Names, even after the user has clicked through the HTTPS warning. The IP must be in the SAN, not just the CN.

**To add the cert to Windows (no admin):** Not possible on this machine. User must click through the certificate warning once per browser, then it works.

---

## 7. Backend API Endpoints

All endpoints served by `/opt/magicbridge/core/magicbridge.py` on port 7777, proxied through nginx.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serves `index.html` |
| GET/POST | `/ws` | WebSocket (keyboard/mouse events) |
| GET | `/stream` | MJPEG video stream (proxied from ustreamer) |
| GET | `/api/status` | Pi status: CPU temp, uptime, IP, HID, stream |
| GET | `/api/networks` | List saved WiFi connections |
| POST | `/api/networks` | WiFi actions: `add`, `remove`, `connect` |
| GET | `/api/networks/psk?name=X` | Get saved password for WiFi profile X |
| GET | `/api/tailscale` | Tailscale status |
| POST | `/api/tailscale` | Tailscale actions: `install`, `login`, `up`, `down`, `logout` |
| POST | `/api/identity` | USB profile management |
| POST | `/api/power` | Pi power: `reboot`, `shutdown` |
| POST | `/api/ai/run` | AI macro execution |
| POST | `/api/update` | Git pull + restart: actions `update`, `status` |
| GET | `/api/stealth/logs` | Access log (stealth dashboard) |

---

## 8. Frontend Features (index_v13.html)

The UI is a single-file HTML/CSS/JS application. No framework, no build step.

### Tabs
| Tab | Features |
|-----|---------|
| **Stream** | Live video (MJPEG), pointer lock (mouse capture), fullscreen mode |
| **Identity** | USB device spoofing (manufacturer, product, serial, VID/PID) |
| **Network** | WiFi management, Tailscale VPN |
| **Agent** | AI macro executor, quick actions, clipboard presets, key macros |
| **System** | Pi status, reboot/shutdown, GitHub update |

### Key Frontend Features
- **Pointer lock** — click video to capture mouse; Esc to release
- **Keyboard passthrough** — all keys forwarded via WebSocket when captured
- **Paste to remote** — text typed in bottom bar is sent as keystrokes to target PC
- **Paste speed control** — Fast / Normal / Slow / Very Slow / Human 🤖
- **Video overlay** — "Mouse & Keyboard captured" bar hides topbar when active
- **WiFi** — list all saved networks, show passwords (👁), connect, edit, forget
- **Tailscale** — connect, disconnect, logout; shows connected IP
- **USB identity spoofer** — 4 built-in profiles (USB flash, External HDD, USB hub, Custom)
- **AI agent** — sends natural language commands to AI API → generates keystrokes
- **System update** — pulls from GitHub and restarts service

---

## 9. Completed Fixes (Chronological)

### Phase 1 — Initial Setup & Services
- ✅ Diagnosed and fixed `magicbridge.service` (added `Restart=always`)
- ✅ Created standalone `ustreamer.service` with proper HDMI capture device path
- ✅ Fixed USB gadget setup script
- ✅ Built initial TinyPilot-style UI (index_v5 through v13)

### Phase 2 — SSL & WebSocket
- ✅ **SSL cert SAN fix** — Old cert missing `IP:172.16.20.116` in SAN → Chrome blocked WSS. Regenerated cert with full SAN list.
- ✅ **WebSocket confirmed working** — `stream.running: True`, Connected status, video live

### Phase 3 — video.py fixes
- ✅ **`is_running()` bug** — Only checked `self.process`, not `self._running`. When ustreamer runs as a systemd service (no subprocess), `process=None` → always returned False.
  ```python
  # Fix: check _running flag first
  def is_running(self) -> bool:
      if getattr(self, '_running', False):
          return True
      return self.process is not None and self.process.poll() is None
  ```
- ✅ **Watchdog indent bug** — `self.restart()` was outside the `if` block → called unconditionally every 5 seconds, causing infinite restart loop.

### Phase 4 — JS SyntaxError
- ✅ **Line 1276 JS crash** — Missing `'` in ternary string: `(a.text.length>40?'…':')+'"'` → crashed ALL JavaScript from line 1276 down. This was root cause of "Connecting...", onLoad undefined, all browser failures.

### Phase 5 — WiFi Backend
- ✅ **WiFi listing bug** — Backend checked `"wifi" in parts[1].lower()` but nmcli returns type `"802-11-wireless"` which does NOT contain "wifi". Fixed to check `"wireless" in parts[1].lower()`.
- ✅ **Password reveal endpoint** — Added `/api/networks/psk?name=X` using `nmcli -s -t -f 802-11-wireless-security.psk connection show <name>`

### Phase 6 — WiFi Frontend onclick Bug
- ✅ **Critical onclick breakage** — Used `onclick="fn(${JSON.stringify(name)})"` which puts `"double quotes"` inside a double-quoted HTML attribute, breaking every single handler silently. Fixed by switching to `data-net` attributes:
  ```javascript
  // WRONG — breaks onclick attribute parsing:
  onclick="wifiShowPsk(${JSON.stringify(n)},this)"
  // CORRECT — use data attribute:
  <div data-net="${escapedName}">
    <button onclick="wifiShowPsk(this)">👁</button>
  // In JS:
  function _wnet(el){ return el.closest('[data-net]'); }
  async function wifiShowPsk(btn){ const name = _wnet(btn).dataset.net; ... }
  ```

### Phase 7 — UI Improvements
- ✅ **Title bar overlay** — `#relbar` (z-index:900, position:fixed) overlapped `#topbar` when mouse captured. Fixed by hiding topbar when captured: `document.getElementById('topbar').style.display = captured ? 'none' : '';`
- ✅ **Tailscale Logout button** — Added ⏏ Logout alongside ⊘ Disconnect in connected state. Added `tsLogout()` function. Added "Stopped" state panel with ▶ Reconnect button.
- ✅ **Tailscale `logout` backend action** — Runs `tailscale logout` command
- ✅ **WiFi UI redesign** — Per-network cards with colored active indicator, 👁 password reveal, ▶ Use, Edit, ✕ forget buttons
- ✅ **raj.local hostname** — Created `avahi-raj.service` systemd unit running `avahi-publish -a -R raj.local 172.16.20.116`
- ✅ **GitHub update endpoint** — `/api/update` POST with `update` and `status` actions; shows git log, pulls, restarts

### Phase 8 — WiFi Form (PENDING DEPLOY)
- 🔄 **WiFi add/edit form improved** — Added labels, taller inputs (34px), text color fix, full-width Show/Hide button. HTML changes made locally in `index_v13.html` but NOT yet deployed (Pi was disconnected before SFTP could complete).

---

## 10. Known Issues / Pending Tasks

| # | Issue | Status | Notes |
|---|-------|--------|-------|
| 1 | WiFi form labels & password visibility | **Pending deploy** | HTML fixed locally, needs SFTP to Pi |
| 2 | Hostname access (`magicbridge.local`) | **Partial** | Pi broadcasts correctly; Windows may need network profile set to "Private" to enable mDNS |
| 3 | GitHub update button | **Partial** | Backend ready, but `/opt/magicbridge` isn't a git repo (files were deployed via SFTP, not cloned). Need to: push to GitHub, then `git clone <repo>` on Pi |
| 4 | USB serial defaults | **Low priority** | `_gen_serial()` exists in backend; may need frontend refresh to show new value |
| 5 | WiFi "Active" shows SSID but network connects to different SSID | **Low priority** | nmcli active detection uses SSID field from device wifi scan, not connection name |

---

## 11. Development Rules & Constraints

### HARD RULES
1. **No Linux sandbox → Pi** — The Cowork/Claude sandbox cannot reach `172.16.20.116`. ALL Pi operations must go through Windows Python + paramiko scripts run via the File Explorer address bar trick.
2. **No NTFS write-through** — Linux sandbox edits to `E:\` are NOT immediately visible to Windows Python. Always embed content as base64 inside the Windows deploy script if reading from disk, OR use SFTP from the script after writing locally.
3. **No admin rights** — This is a personal laptop without admin. Cannot: edit `C:\Windows\System32\drivers\etc\hosts`, install system services, change firewall rules at system level.
4. **No `cmd /c python script.py > log.txt 2>&1`** — Shell redirect conflicts with script's internal file writer. Use only internal logging (`flog = open(LOG, "w")`).
5. **Large file SFTP only** — Any HTML or binary > 50KB must be deployed via SFTP (`ssh.open_sftp()`), not base64 echo (shell buffer truncation).

### CODING RULES
6. **Never use `JSON.stringify(x)` in onclick HTML attributes** — Double quotes break attribute parsing. Always use `data-*` attributes instead.
7. **All sudo commands via**: `echo 'lol' | sudo -S bash -c 'command'`
8. **Always restart service after backend patch**: `sudo systemctl restart magicbridge.service`
9. **Always verify deployment**: Check file size on Pi matches local size after SFTP.
10. **Test WiFi endpoint after backend change**: `curl -sk https://127.0.0.1/api/networks`

### nmcli WiFi Type String
nmcli returns `"802-11-wireless"` as the WiFi connection type, NOT `"wifi"`. Check for `"wireless"` in `parts[1].lower()`.

---

## 12. Architecture Diagram

```
[Target PC]
   │  USB HID (keyboard + mouse input)
   │  HDMI out → capture card
   ▼
[Raspberry Pi 4]
   ├── USB gadget (dwc2 OTG) — emulates keyboard + mouse
   ├── USB capture card — receives HDMI
   ├── ustreamer (port 8080) — MJPEG stream from capture card
   ├── MagicBridge backend (port 7777) — aiohttp Python
   │     ├── /ws — WebSocket for keyboard/mouse events
   │     ├── /api/* — WiFi, Tailscale, identity, power, update
   │     └── serves index.html
   └── nginx (ports 80/443) — HTTPS proxy + SSL termination
         ├── / → proxy to :7777
         ├── /stream → proxy to :8080
         └── /ws → WebSocket proxy to :7777

[Browser] ──HTTPS──▶ nginx ──▶ backend
          ◀──MJPEG── nginx ◀── ustreamer
          ◀──WSS──── nginx ◀── backend
```

---

## 13. Git / Update Setup (TODO)

The `/opt/magicbridge` directory is NOT currently a git repo (files were deployed directly). To enable the "Update from GitHub" button:

1. Push project to GitHub (user's repo)
2. On Pi:
   ```bash
   cd /opt
   sudo mv magicbridge magicbridge_backup
   sudo git clone https://github.com/<user>/<repo>.git magicbridge
   sudo chown -R raj:raj /opt/magicbridge
   ```
3. Backend already has `/api/update` endpoint that runs `git -C /opt/magicbridge pull --ff-only` and restarts the service.

---

## 14. Important Code Snippets

### Checking if Pi is reachable
```python
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect("172.16.20.116", username="raj", password="lol", timeout=5)
    print("Connected")
except Exception as e:
    print(f"Failed: {e}")  # TimeoutError = Pi offline or IP changed
```

### Restarting magicbridge after backend edit
```python
sudo("systemctl restart magicbridge.service")
time.sleep(4)
o,_ = run("systemctl is-active magicbridge.service")
print(f"Status: {o.strip()}")
```

### Verifying WiFi fix
```python
o,_ = run("curl -sk https://127.0.0.1/api/networks")
print(o)  # Should show {"networks":["preconfigured","lol","qwed","raj"],"active":"..."}
```

### Frontend data-net pattern (correct onclick approach)
```javascript
// Render networks safely (no JSON.stringify in onclick!)
list.innerHTML = networks.map(n => {
    const esc = n.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    return `<div data-net="${esc}">
        <button onclick="wifiShowPsk(this)">👁</button>
        <button onclick="wifiRemove(this)">✕</button>
    </div>`;
}).join('');

// In JS — get name from data attribute
function _wnet(el) { return el.closest('[data-net]'); }
async function wifiShowPsk(btn) {
    const name = _wnet(btn).dataset.net;
    // ... fetch /api/networks/psk?name=encodeURIComponent(name)
}
```

---

## 15. Next Session Checklist

When you reconnect the Pi:

- [ ] Verify Pi IP: `ping magicbridge.local` or check router
- [ ] Deploy pending WiFi form fix: run `mb_v19_deploy.py`
- [ ] Test WiFi panel: all 4 buttons work, labels visible, password readable
- [ ] Test `magicbridge.local` in Chrome (set Windows network to Private if needed)
- [ ] Set up GitHub repo + git clone on Pi for update feature
- [ ] Verify Tailscale Logout button appears when connected

---

*End of handbook. All source files in `E:\Startup\magicbridge\`. Main frontend: `index_v13.html`. Main backend: Pi at `/opt/magicbridge/core/magicbridge.py`.*
