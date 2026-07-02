# MagicBridge — Complete Handoff Handbook

> Give this entire document to the new chat. It contains every credential, root cause, working state, and task needed to finish MagicBridge perfectly.

---

## 1. What MagicBridge Is

A self-hosted KVM-over-IP built on Raspberry Pi 4.
- Pi connects to target Windows PC via **USB-C** (acts as Logitech K120 keyboard + mouse HID device)
- Pi captures target's HDMI output via **USB capture card** (MacroSilicon MS2109)
- User opens `https://magicbridge.local` in Chrome → sees the remote screen, controls it with mouse/keyboard

---

## 2. Credentials & Access

| Item | Value |
|------|-------|
| Pi IP | `172.16.20.116` |
| Pi hostname | `magicbridge.local` |
| Pi SSH user | `raj` |
| Pi SSH password | `lol` |
| Pi sudo pattern | `echo lol \| sudo -S <cmd>` |
| Post-install web user | `admin` |
| Web/stealth password | `lol` |
| GitHub repo | `razzrohith/MagicBridge` |
| Windows machine | Raj's PC, `E:\Startup\magicbridge\` = project root |

**SSH from Windows Python** (address-bar trick — the only reliable way to run scripts):
1. Open File Explorer → navigate to `E:\Startup\magicbridge`
2. Press Alt+D → type `cmd /c python E:\Startup\magicbridge\<script>.py` → Enter
3. Check `<script>_log.txt` for output

**CRITICAL**: Linux sandbox cannot reach `172.16.20.116` (network unreachable). All Pi access must go through Windows Python + paramiko.

**CRITICAL**: Linux sandbox edits to E:\ files have NTFS write-through lag. Always embed file content as base64 in Windows Python deploy scripts — never read from E:\ disk in the deploy script.

---

## 3. Pi File Layout

```
/opt/magicbridge/
  core/
    magicbridge.py     ← main aiohttp server (HTTP + WebSocket)
    hid.py             ← USB HID keyboard + mouse writer
    video.py           ← ustreamer manager
  web/
    index.html         ← KVM web UI (the one the user sees)
  dashboard/
    stealth-dashboard.py ← admin panel at /stealth/

/etc/magicbridge/
  config.json          ← video resolution/quality/fps settings
  ssl/cert.pem         ← self-signed TLS cert
  ssl/key.pem

/etc/nginx/sites-enabled/magicbridge   ← nginx config
/etc/systemd/system/magicbridge.service
/etc/systemd/system/mb-gadget.service
/usr/local/bin/mb-gadget.sh            ← USB gadget setup script
```

---

## 4. Confirmed Working (Don't Break These)

### USB HID Gadget
- **UDC**: `fe980000.usb` bound, state = `configured` (Windows IS enumerating it)
- **Functions**: `hid.keyboard` + `hid.mouse` symlinked in gadget `g1` configfs
- **Devices**: `/dev/hidg0` (keyboard), `/dev/hidg1` (mouse) — permissions `crw-rw-rw-`
- **Identity**: Logitech K120, VID=0x046d PID=0xc31c
- **HID writes succeed** without sudo (confirmed via test script)
- **Service**: `mb-gadget.service` active (exited) — correct for oneshot

### Video Capture
- **Device**: `/dev/video0` — USB Video (MacroSilicon MS2109)
- **Max resolution**: 1920×1080 @ 60fps MJPEG (confirmed via v4l2-ctl)
- **ustreamer command** (correct): `ustreamer --device /dev/video0 --format MJPEG --resolution 1920x1080 --desired-fps 30 --quality 90 --host 127.0.0.1 --port 8081 --workers 2 --persistent --drop-same-frames 30`
- **Config**: `/etc/magicbridge/config.json` → `{"video": {"resolution": "1920x1080", "fps": 30, "quality": 90, "format": "MJPEG"}}`

### Network
- nginx on 443 with HTTPS (self-signed cert, Chrome shows "Not secure" — expected)
- WebSocket proxy: `/ws` → `127.0.0.1:8080`
- Stream proxy: `/stream` → `127.0.0.1:8081/stream`
- Permissions-Policy header includes `pointer-lock=(self)` ✓

---

## 5. Known Issues & Root Causes

### Issue 1: Stream Goes Black / "Connecting..."
**Root cause**: `magicbridge.service` crashes or ustreamer subprocess dies. When magicbridge restarts, ustreamer sometimes fails to re-acquire `/dev/video0`.

**Fix needed**:
```ini
# In /etc/systemd/system/magicbridge.service — add:
Restart=always
RestartSec=3
```
Also run ustreamer as a **separate service** (not subprocess of magicbridge) so it survives magicbridge restarts.

### Issue 2: Pointer Lock Fails in Chrome
**Root cause**: Chrome requires `requestPointerLock()` to be called from a `click` event listener registered **directly on the element being locked**. Calling it from a toolbar button's onclick (a different element) → Chrome fires `pointerlockerror`.

**Fix** (already applied to local `src/web/index.html` but had deployment issues):
```javascript
// CORRECT — click listener directly on the element being locked
const va = document.getElementById('va');
va.addEventListener('click', () => {
  if (!document.pointerLockElement) {
    va.requestPointerLock();  // same element, Chrome accepts this
  }
});
```
**DO NOT** use `{unadjustedMovement: true}` — causes Promise-based failures that block the fallback.

### Issue 3: Deploy Script Reads Stale Files from NTFS
**Root cause**: Linux sandbox edits files at the NTFS mount point, but Windows Python reads a cached version. File is 20031 bytes in Linux but Windows Python reads old 20421 bytes.

**Fix**: Always embed file content as base64 string directly inside the Windows Python deploy script. Use this pattern:
```python
# In Linux sandbox:
import base64
with open('/sessions/.../src/web/index.html','rb') as f:
    b64 = base64.b64encode(f.read()).decode()
# Then embed b64 as a string in the Windows Python script
```

---

## 6. Fresh Rebuild Plan

### Architecture (TinyPilot-inspired, simpler & more robust)

```
Browser (Chrome)
  │  HTTPS (443)
  ▼
nginx on Pi
  ├── GET /          → index.html (static)
  ├── GET /stream    → proxy → ustreamer:8081 (MJPEG)
  ├── /ws            → proxy → magicbridge:8080 (WebSocket)
  └── /api/          → proxy → magicbridge:8080

magicbridge.py (aiohttp, port 8080)
  ├── WebSocket handler → hid.py → /dev/hidg0, /dev/hidg1
  └── API handlers

ustreamer (separate service, port 8081)
  └── /dev/video0 → MJPEG stream

mb-gadget.service (oneshot)
  └── Configures USB HID gadget → /dev/hidg0, /dev/hidg1
```

### Key Design Decisions for Rebuild

1. **ustreamer as its own systemd service** — not a subprocess. Survives magicbridge restarts.
2. **No unadjustedMovement** — plain `va.requestPointerLock()`, click on video area directly.
3. **Stream as `<img src="/stream">`** — this is what TinyPilot uses and it works. Simple, no MSE/WebRTC complexity.
4. **Keyboard capture**: capture at `document` level when `captured = true` (pointer locked). Already works.
5. **Absolute mouse fallback**: if pointer lock fails, offer absolute mode (mousemove sends X/Y fraction of video dimensions).

---

## 7. Fresh Task List (New Chat)

### Immediate (Fix Current Install)
- [ ] **T1**: SSH into Pi → check magicbridge service status → restart if crashed
- [ ] **T2**: Add `Restart=always` + `RestartSec=3` to magicbridge.service
- [ ] **T3**: Extract ustreamer into its own `ustreamer.service` (separate from magicbridge)
- [ ] **T4**: Deploy fixed `index.html` with correct pointer lock code (embed as base64)
- [ ] **T5**: Test end-to-end: stream loads → click video area → pointer locks → mouse/keyboard works

### Rebuild (If T1-T5 don't fully fix it)
- [ ] **T6**: Rewrite `index.html` from scratch using TinyPilot's clean approach (see reference below)
- [ ] **T7**: Add absolute mouse mode fallback (no pointer lock needed)
- [ ] **T8**: Rewrite `magicbridge.service` with proper restart policy
- [ ] **T9**: Write `ustreamer.service` as standalone service
- [ ] **T10**: Update `install.sh` to set up both services

### Polish
- [ ] **T11**: Make stream fill the full browser viewport (CSS `object-fit: contain` on img)
- [ ] **T12**: Add stream quality selector (720p/1080p/60fps toggle)
- [ ] **T13**: Push final working version to GitHub `razzrohith/MagicBridge`
- [ ] **T14**: Write proper README with setup instructions

---

## 8. TinyPilot Reference — Clean Index.html Approach

TinyPilot (open source, MIT) uses this pattern which works reliably:

```html
<!-- Stream: simple img tag, MJPEG loads directly -->
<img id="remote-screen" src="/stream" 
     onerror="onStreamError()" onload="onStreamLoad()">

<script>
const screen = document.getElementById('remote-screen');
let isCapturing = false;

// KEY: listener on the image element itself
screen.addEventListener('click', startCapture);

function startCapture() {
  screen.requestPointerLock();
}

document.addEventListener('pointerlockchange', () => {
  isCapturing = document.pointerLockElement === screen;
  updateUI();
  if (!isCapturing) sendRelease();
});

document.addEventListener('mousemove', (e) => {
  if (!isCapturing) return;
  sendInput({ type: 'mousemove', dx: e.movementX, dy: e.movementY });
});

document.addEventListener('mousedown', (e) => {
  if (!isCapturing) return;
  e.preventDefault();
  sendInput({ type: 'mousedown', button: e.button });
});

document.addEventListener('mouseup', (e) => {
  if (!isCapturing) return;
  sendInput({ type: 'mouseup', button: e.button });
});

document.addEventListener('keydown', (e) => {
  if (!isCapturing) return;
  e.preventDefault();
  sendInput({ type: 'keydown', code: e.code });
});

document.addEventListener('keyup', (e) => {
  if (!isCapturing) return;
  sendInput({ type: 'keyup', code: e.code });
});

// WebSocket
let ws;
function connect() {
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.onclose = () => setTimeout(connect, 2000);
}
connect();

function sendInput(obj) {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify(obj));
}

function sendRelease() {
  sendInput({ type: 'release_all' });
}

// Stream retry
function onStreamError() {
  setTimeout(() => { screen.src = '/stream?' + Date.now(); }, 2000);
}
</script>
```

**Critical differences from what we had:**
1. `screen.requestPointerLock()` — locking the `<img>` element directly
2. `screen.addEventListener('click', startCapture)` — click on the img, not a button
3. Mouse events at `document` level (works after pointer lock, events are global)
4. `src` refresh on error with cache-bust (`?` + timestamp)

---

## 9. Useful Commands for New Chat

```bash
# Check service status
systemctl status magicbridge mb-gadget ustreamer

# Restart everything
echo lol | sudo -S systemctl restart magicbridge

# Check stream is working
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/stream

# Check USB gadget
cat /sys/class/udc/fe980000.usb/state  # should say "configured"
ls -la /dev/hidg*                       # should show hidg0, hidg1

# Check HID writes work
python3 -c "import struct; open('/dev/hidg0','wb').write(struct.pack('8B',0,0,0x04,0,0,0,0,0))"

# Check ustreamer process
ps aux | grep ustreamer | grep -v grep

# View magicbridge logs
journalctl -u magicbridge -n 50 --no-pager

# Deploy a file (sftp pattern)
sftp raj@172.16.20.116  # password: lol

# Git push (from E:\Startup\magicbridge, via address bar trick)
# Alt+D → cmd /c python E:\Startup\magicbridge\git_push_only.py → Enter
```

---

## 10. GitHub State

- Repo: `https://github.com/razzrohith/MagicBridge`
- Latest commit: `6744da3` (both HID fix + deploy scripts pushed ✓)
- `refs/remotes/origin/main` = `6744da3` (up to date)

---

## 11. Windows Helper Scripts in E:\Startup\magicbridge\

| Script | Purpose |
|--------|---------|
| `git_push_only.py` | Push to GitHub (run via address bar trick) |
| `deploy_pi.py` | Deploy files to Pi + restart service |
| `pi_diag.py` | Full diagnostics: video, HID, services |
| `pi_fix2.py` | Fix HID permissions + udev rules |
| `pi_fix3.py` | Fix config.json resolution |
| `pi_deploy_final.py` | Deploy index.html (content embedded as base64) |

All scripts write logs to `*_log.txt` in the same folder.

---

## 12. What the New Chat Should Do First

1. Run `pi_diag.py` via address bar → read `pi_diag_log.txt`
2. If service crashed: SSH in, restart, check ustreamer is at 1920×1080
3. If stream still broken: create standalone `ustreamer.service`
4. Deploy fixed `index.html` using the **base64 embedding method** (not file read)
5. Test: open `https://magicbridge.local` → Ctrl+Shift+R → click the video → confirm cursor locks

If pointer lock still fails after the fix, implement **absolute mouse mode**:
- No pointer lock needed
- Send `{type: 'mousemove_abs', x: ratio_x, y: ratio_y}` on every mousemove over the video
- Pi maps ratio to display coordinates and sends absolute HID report
- Works in all browsers without any special permissions

---

*This document was generated from the session that ran out of context. The setup is 95% complete — the hardware all works (HID confirmed, UDC configured, capture card working at 1920×1080). The remaining 5% is a pointer lock bug in the browser JavaScript and a service stability issue. Both are pure software fixes.*
