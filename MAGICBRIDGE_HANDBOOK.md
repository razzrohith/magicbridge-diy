# MagicBridge — Project Handbook
> Complete reference for development, debugging, and continuation.
> Last updated: July 9, 2026 (post Phases 1-4 stealth/security push, OLED, fan control, EDID scaffold, LUKS config encryption, GitHub repo sync, and a same-day feature audit/bugfix pass — see §11a for everything since the July 4 revision)

---

## 1. Project Overview

**MagicBridge** is a self-hosted KVM-over-IP system built on a Raspberry Pi 4. It lets you control a remote PC (keyboard, mouse, screen) entirely through a browser — no software installed on the target machine, no admin rights needed.

Think of it as a DIY [TinyPilot](https://tinypilotkvm.com/), but free, self-hosted, with a "stealth mode" admin panel (USB identity spoofing, MAC spoofing) plus extra features: WiFi management, Tailscale VPN, an AI-powered natural-language macro executor, clipboard sync, and a mouse jiggler.

```
[Target PC] ←USB HID (keyboard + mouse emulation)
     ↓
[Raspberry Pi 4]
  - Captures HDMI output via USB capture card (MS2109 today; C790 CSI board pending)
  - Emulates USB HID keyboard + mouse via USB gadget
  - Runs MagicBridge web server (Python/aiohttp + nginx)
     ↓
[Browser on any device] — https://172.16.20.116/ or https://magicbridge.local/
```

---

## 2. Hardware

| Component | Detail |
|-----------|--------|
| Pi model | Raspberry Pi 4 |
| OS | Raspberry Pi OS Bookworm (64-bit) |
| Capture card (current) | MS2109 USB HDMI-to-USB dongle → ustreamer MJPEG capture |
| Capture card (incoming) | C790 HDMI-to-CSI2 board — direct CSI/DMA path, enables hardware H.264 encode + near-zero latency. **Not yet physically arrived.** See §9. |
| USB cable (data) | Pi USB-C OTG port → Target PC; carries HID keyboard + mouse |
| Network | Pi on WiFi/Ethernet, same LAN as controlling device; Tailscale available for remote access |

USB gadget mode: the Pi's USB-C port presents as a USB keyboard + mouse via the `dwc2` OTG driver + `libcomposite` gadget framework at `/sys/kernel/config/usb_gadget/g1/`. Default reported identity is a **Logitech Unifying Receiver** (`USB Receiver`, VID `0x046d`, PID `0xc52b`, no serial, 3 HID interfaces) — chosen because it's a real device that legitimately has both keyboard+mouse interfaces (a single-purpose keyboard model structurally can't).

---

## 3. Pi — Network & Access

| Item | Value |
|------|-------|
| IP | `172.16.20.116` (may change if DHCP renews — check router if unreachable) |
| SSH user | `raj` |
| SSH password | `lol` |
| Sudo password | `lol` |
| Web UI | `https://172.16.20.116/` or `https://magicbridge.local/` |
| Sudo syntax | `echo 'lol' \| sudo -S bash -c '<command>'` |

### CRITICAL constraint: no sandbox → Pi network path
The Cowork/Claude Linux sandbox **cannot reach 172.16.20.116**. All Pi access goes through **Windows Python + paramiko**, executed via the File Explorer address-bar trick:

1. Save the script to `E:\Startup\magicbridge\<script>.py` (via Notepad: type/paste → Ctrl+Shift+S → select "All Files" as type so it doesn't get a `.txt` extension appended).
2. In File Explorer, click the address bar (or Ctrl+L), type:
   ```
   cmd /c python E:\Startup\magicbridge\<script>.py
   ```
   Press Enter. The address bar reverting to "Home" afterward is normal, not a failure.
3. The script must write its **own** log file internally (e.g. `E:\Startup\magicbridge\<script>_log.txt`). **Never** rely on shell redirection (`> log.txt 2>&1`) — it conflicts with the script's own file writer and produces an empty log.
4. Read the log back via Notepad (Ctrl+O, type the exact filename) — `E:\Startup\magicbridge\` is outside any Cowork-mounted path, so the Read/bash tools can't see it directly.

### Deploying files
Large files (HTML, Python) go over **SFTP**, never base64/echo (shell buffers silently truncate large payloads):
```python
sftp = ssh.open_sftp()
sftp.put(local_path, "/tmp/<name>.new")
sftp.close()
sudo(f"cp /tmp/<name>.new <dest> && chown root:root <dest>")
```
Standing deploy pattern used all of this session (reuse it): **backup remote file with timestamp suffix → SFTP to `/tmp/` → `sudo cp` into place with chown → byte-size verification (`stat -c %s`) → targeted `grep -c` marker checks → `nginx -t` BEFORE any `systemctl reload nginx` (never blind-reload) → service restart → `systemctl is-active` + `/health` + `/api/status` HTTP probes.**

---

## 4. Dev File Map (IMPORTANT — read this before editing anything)

This Cowork project folder (`C:\Users\razzr\Claude\Projects\MagicBridge\`) has **three** distinct areas. Don't confuse them:

| Location | What it is | Source-of-truth for |
|---|---|---|
| `_live_pull\` | The files actively edited this session, kept byte-for-byte in sync with the live Pi via the deploy pattern above | `video.py`, `magicbridge.py`, `index.html`, `nginx_magicbridge.conf`, `install_janus_webrtc.sh` |
| `pi_source\` | A fuller mirror of `/opt/magicbridge/` and `/usr/local/bin/` — most files here are stable and not touched every session, but were updated (and redeployed) where noted in §10 | `opt/magicbridge/core/hid.py`, `opt/magicbridge/dashboard/stealth-dashboard.py`, `opt/magicbridge/provision/mb-setup-ui.py`, `usr/local/bin/mb-gadget.sh`, `mb-provision.sh`, `mb-lockdown.sh` |
| `install.sh` (project root) | The master fresh-install script (`git clone` + install to a brand-new Pi). Pulls its own file tree from `src/` on the **GitHub repo** at push time. **RESOLVED 2026-07-09**: a real repo now exists at `github.com/razzrohith/MagicBridge`, and the Pi's live files, this laptop's clone (`C:\Users\razzr\Claude\Projects\MagicBridge\repo\`), and GitHub `main` were all reconciled to the same commit (`13246c7`) that session — see §11a. Task #8 is effectively done; going forward, use the `mb_git_*` deploy-script pattern (clone/pull-live-files/commit/push, then reconcile Pi) rather than re-initializing anything. | Bootstrap defaults only (e.g. `config.json` template) |

**Known drift risk:** the Windows-side `E:\Startup\magicbridge\` folder (used only as scratch space to run deploy scripts) also contains an old `index_v13.html` (96KB, dated 7/3). That file is **not** the source of truth — `_live_pull\index.html` is. Don't edit the one in `E:\Startup\magicbridge\`.

**Always use the `Read` tool, not bash, to check `_live_pull`/`pi_source` files that were just edited with `Edit` this session** — the bash sandbox's mount of this project folder has been observed to serve stale content for `Edit`-modified files within the same session (confirmed via mismatched `stat`/`wc -l`). Files written fresh via `Write` do stay in sync on bash. If you need to run a bash-side check (py_compile, node --check) on an Edited file, first `Write` a copy into the Cowork **outputs** folder, then check it there.

---

## 5. Architecture & Ports (verified this session — supersedes any older port numbers)

```
[Browser] ──HTTPS/WSS (443)──▶ nginx ──┬─▶ :8080  magicbridge.py   (main KVM backend, aiohttp)
                                        ├─▶ :8081  ustreamer         (MJPEG / H.264 sink, subprocess of video.py — NOT a standalone systemd service)
                                        ├─▶ :7777  stealth-dashboard.py (Flask, admin panel, fully separate auth)
                                        ├─▶ :8188  Janus Gateway WS  (WebRTC signaling — built, plugin doesn't load yet, see §9)
                                        └─▶ :8088  Janus Gateway HTTP (unused; Janus built with --disable-all-transports --enable-websockets only)
```

- `magicbridge.service` runs `/opt/magicbridge/core/magicbridge.py` (NOT `/opt/magicbridge/magicbridge.py` — that path is an empty decoy file, always has been).
- `video.py`'s `VideoManager` class starts/stops `ustreamer` itself as a managed subprocess; there is **no separate `ustreamer.service`** — if one exists on a given Pi it will fight `video.py` for the capture device and port 8081 and should be disabled.
- `stealth-dashboard.service` runs the Flask admin panel on 7777, reachable at `https://<host>/stealth/`. It has its own independent password/session — logging into the main KVM page does **not** unlock it, and vice versa.
- nginx terminates TLS (self-signed cert, SANs include the Pi's IP — required because Chrome blocks script-initiated WSS to IPs not in the cert's SAN, even after clicking through the warning).

### Firewall
`install.sh`'s iptables rules block 7777/8080/8081 from anything but loopback — only nginx (80/443) and SSH (22) are reachable externally. `mb-lockdown.sh on|off|status` additionally restricts 80/443 to the `tailscale0` interface only when enabled (SSH is never touched by that toggle).

---

## 6. Auth Model (audited this session, no changes needed — reference only)

Two **fully independent** auth systems:

- **Main KVM page** (`magicbridge.py`): bcrypt-hashed password (SHA-256 fallback if bcrypt unavailable) → HMAC-SHA256-signed session token with embedded timestamp, `SESSION_TIMEOUT=1800s`, checked via constant-time compare. Cookie flags: `httponly`, `secure`, `samesite=Lax`. Per-IP progressive brute-force delay (`min(fails,10)` seconds, resets on success). Changing the password rotates the signing secret, invalidating every other active session immediately. `X-Real-IP` is safe to trust for the per-IP limiter because nginx unconditionally overwrites it with `$remote_addr` on every location block (can't be spoofed by a client).
- **Stealth panel** (`stealth-dashboard.py`, Flask): same bcrypt/HMAC pattern, plus CSRF tokens (`X-CSRF-Token` header, checked via `_csrf_ok()`) on every state-changing POST.

Both middlewares gate `/api/*` and `/ws` with a JSON 401 (not a redirect) so the frontend's `fetch()` calls fail cleanly instead of following a redirect into HTML.

---

## 7. Backend API Endpoints (magicbridge.py, port 8080, behind nginx `/api/`, `/ws`, `/`, `/login`, `/logout`)

| Method | Endpoint | Notes |
|---|---|---|
| GET/POST | `/login` | HTML login form + auth |
| POST | `/logout` | Clears session cookie |
| POST | `/api/auth/change-password` | Requires current password; rotates session secret |
| GET | `/` | Serves `index.html` (auth-gated) |
| GET | `/ws` | WebSocket — keyboard/mouse events, status pushes |
| GET/POST | `/api/stream/settings` | Video resolution/fps/mode (`h264` default, `mjpeg` fallback) |
| GET | `/api/status` | CPU temp, uptime, IP, HID device state, `stream: video.status()` |
| GET | `/api/devices` | Enumerate capture devices |
| GET/POST | `/api/identity` | USB identity (read-only view; the *stealth* panel is where it's actually changed) |
| GET/POST | `/api/jiggler` | Mouse jiggler on/off + style |
| GET/POST | `/api/networks` | WiFi: list / `add` / `remove` / `connect` — **this is the one the main KVM page actually uses today** |
| GET/POST | `/api/tailscale` | Status / install / login / up / down / logout |
| GET/POST | `/api/network/lockdown` | Toggle Tailscale-only access (wraps `mb-lockdown.sh`) |
| POST | `/api/update` | Git pull + restart |
| POST | `/api/power` | `reboot` / `shutdown` |
| GET/POST | `/api/ai/run` | Natural-language → keystroke actions via OpenRouter/OpenAI/Gemini/Claude. Blocking HTTP call is now offloaded to a thread executor (fixed this session — see §10). |
| GET | `/api/stealth/logs` | — |

**Removed this session:** the old `/api/wifi/*` passthrough (see §10 — it was a live, unauthenticated vulnerability and has been deleted).

### Stealth panel endpoints (stealth-dashboard.py, port 7777, behind nginx `/stealth/`)
USB identity presets/custom apply, safe-mode toggle, MAC spoofing (+ persistence via a generated systemd unit), Tailscale/Funnel, DuckDNS, WiFi (`/api/wifi/*` — **now auth-gated**, see §10), config backup/restore, log viewer, password change, reboot. All state-changing routes require both the panel's session cookie and CSRF token.

---

## 8. Frontend (`_live_pull\index.html`)

Single-file HTML/CSS/JS, no build step, no framework. Tabs: Stream, Identity, Network, Agent (AI + macros/clips), System.

Key behaviors:
- **Video transport**: WebRTC (`h264`, via Janus) is the default, ★-marked in the dropdown. If Janus/the C790 board isn't present, both the server (`video.py`) and the client auto-fall-back to MJPEG within ~8s (connect timeout) with a toast telling the user why. `syncVideoTransport()` reconciles the UI to whatever `/api/status` actually reports on every poll, so server-side fallback is always reflected even if the user never touched the dropdown.
- **Pointer lock / KVM control**: capturing the screen now auto-hides the right-side settings sidebar (`setSidebarForControl(false)`); releasing (Esc or losing capture) restores it to whatever the user's own open/closed preference (`_sideOpen`) was — the two states are deliberately independent so this doesn't clobber a manually-closed sidebar.
- **Identity tab**: no longer mentions "Profile Switching / Stealth Mode" on the main page (removed per request — that's stealth-panel-only territory now).
- **MJPEG/WebRTC transport isolation** (fixed this session): the hidden MJPEG `<img>` no longer corrupts the visible status pills or reconnects itself in the background while WebRTC is the active transport — `onLoad()`/`onErr()`/the 15s watchdog are now all guarded on `_activeTransport === 'h264'`.
- **AI Agent**: API keys for OpenRouter/OpenAI/Gemini/Claude are stored in **plaintext in `localStorage`** (`mb_ai_key_<provider>`). Known, accepted-for-now design gap — see §10. A same-origin XSS would be able to read them; there's no CSP header restricting script sources today.

---

## 9. WebRTC / H.264 Upgrade — Status (the big pending item)

Goal: swap the MS2109 USB dongle for a **C790 HDMI-to-CSI2** board, giving a direct CSI/DMA capture path and enabling ustreamer's `--h264-sink` output, delivered to the browser over WebRTC via Janus Gateway (near-zero latency vs MJPEG-over-HTTP).

**What's done and deployed (works today, safe even without the hardware):**
- `video.py`'s `_start_ustreamer_h264()` uses the real, verified ustreamer flags (`--encoder M2M-IMAGE`, `--h264-sink`, `--h264-sink-mode 660`, `--h264-bitrate`, `--h264-gop`, etc. — NOT an `--encoder H264` flag, that doesn't exist in real ustreamer).
- Default mode is `h264` (in both `video.py` and `magicbridge.py`'s config default, and now in `install.sh`'s bootstrapped `config.json` too — this last one was a real bug found and fixed this session, see §10).
- Full two-layer fallback: server-side (`video.py` falls back to `mjpeg` if the h264 ustreamer process dies within 1s of starting) and client-side (Janus connect/attach/watch error handlers + 8s timeout → `_webrtcFallbackToMjpeg()`).
- `install_janus_webrtc.sh` correctly builds Janus Gateway from source (tag `v1.0.0`, matching pikvm/ustreamer's own docs) **before** building ustreamer with `WITH_JANUS=1` — the original script had this backwards, which was the root cause of a real build failure (`refcount.h: No such file or directory`), fixed by symlinking headers + a sed patch, confirmed against `github.com/pikvm/ustreamer/issues/134`.
- nginx has `/janus-ws` (used) and `/janus-http/` (present, unused, harmless) proxy locations.
- Frontend Janus client uses the correct v1.0.0 API (`onremotetrack`, not the older `onremotestream`), and the plugin config uses the correct `memsink: { object = "magicbridge::h264" }` HOCON format (both were real bugs caught by reading the actual pikvm/ustreamer source, not guessed).

**What's still broken and needs the physical hardware to actually debug:**
- Even with headers wired correctly, ustreamer's bundled Janus plugin source (`janus/src/plugin.c`) fails to compile against the Janus v1.0.0 API actually installed (`janus_plugin_rtp` struct mismatch) — `libjanus_ustreamer.so` is never produced. This is a known, flagged upstream compatibility issue, not something to keep guessing at without a real capture device to validate the rest of the pipeline against.
- **Net effect: WebRTC delivery does not work yet.** The system correctly and safely falls back to MJPEG in the meantime — this is expected, not a regression.

**When the C790 arrives:** this is task #9. Plug it in, confirm `v4l2-ctl --list-devices` sees it, retest the h264 sink path end-to-end, and revisit the Janus plugin compile error with a live pipeline to validate against (may need to patch `janus/src/plugin.c` itself, or pin to a different Janus tag).

---

## 10. Security Audit — This Session (full pass, "check everything")

A full-project bug/security audit was requested and completed. Found and fixed:

1. **Unauthenticated WiFi control (most serious).** `stealth-dashboard.py`'s `/api/wifi/{status,saved,scan,add,remove,connect}` had **no auth check at all**, and nginx proxied `https://<pi>/api/wifi/*` straight to them — no login required. Leftover from before the main KVM page had its own authenticated `/api/networks`; nothing legitimate used the old path anymore. **Fixed**: all six routes now require the stealth panel's session (`_authed()`), state-changing ones also check CSRF; the dead nginx `location /api/wifi/` block was deleted outright. Verified post-deploy: all four now return 401 with no cookie, and the stealth panel's own (cookie-carrying) calls to the same routes still work.
2. **`/api/ai/run` blocked the entire server.** Called `urllib.request.urlopen()` (blocking) directly inside the aiohttp coroutine — for as long as the external AI provider took to respond (up to 30s), no other client's keyboard/mouse/video/status could be processed at all. **Fixed**: the blocking call now runs via `loop.run_in_executor()`.
3. **Reflected XSS in the WiFi provisioning captive portal.** `mb-setup-ui.py` echoed the submitted SSID into the "Setup complete" page unescaped — WiFi network names are attacker-controlled text. **Fixed**: `html.escape()` on output, plus embedded newlines stripped from SSID/password before writing to the line-based file `mb-provision.sh` parses with `sed`.
4. **MJPEG/WebRTC status-pill and background-reconnect bug** in `index.html` — see §8, already described there.
5. **`install.sh`'s bootstrapped `config.json` still defaulted to `"mode": "mjpeg"`**, which would have silently defeated the WebRTC-by-default change on any fresh install of the other 2 physical units (present key beats the backend's own `"h264"` fallback). Fixed to `"h264"`.

**Reviewed and found sound, no changes needed:** the whole auth/session design (§6), `hid.py` (HID report encoding), `mb-gadget.sh`, `mb-lockdown.sh`, `mb-provision.sh` shell scripts, and the rest of `index.html`'s JS.

**Known, accepted, not fixed this session:** AI Agent API keys stored in plaintext in `localStorage` (§8) — fixing this properly means moving key storage server-side, a bigger redesign than a bug fix, left as a deliberate open decision rather than something to silently rearchitect.

---

## 11a. What Shipped July 5–9, 2026 (Phases 1-4 + adjacent features)

Everything below landed between the July 4 revision of this handbook and the July 9 GitHub sync, via the `mb_deploy_*`/`mb_luks_*`/`mb_add_*` script pattern (each has its own `_log.txt`). §9's WebRTC/H.264 status is **unchanged** by this work — still hardware-pending, see that section.

| Area | What shipped | Status as of 2026-07-09 |
|---|---|---|
| Stealth/security (Phases 1-4) | A staged hardening pass across USB gadget config, network, and logging — see rows below for the specific pieces (RAM-only logs, LUKS, USB hardening, HID auto-disconnect, typing jitter) | Done, deployed |
| OLED status display (`oled.py`) | IP/temp/uptime/service/stream health on an I2C OLED; config-driven layout, hot-reloaded via mtime check; optional 4th line with graceful font fallback | Code deployed; **hardware-pending** — deploy-time logs show `I2C device not found: /dev/i2c-1`, retries every 15s. Confirm the module is physically wired before relying on it |
| Fan control (`mb-setup-fan.sh`) | One-time GPIO fan overlay setup via `config.txt`, deliberately a static script (not a daemon) so it survives `magicbridge.py` being down | Working |
| EDID identity spoofing (`mb_edidconf.py`) | Clones/spoofs the monitor EDID the target PC sees, per `EDID_CLONING_WORKFLOW.md` | Code-complete, verified only against a synthetic EDID structure — **hardware-pending**, needs the real C790 + a target monitor (task #7 below) |
| LUKS config-directory encryption | Encrypts `/etc/magicbridge` at rest with boot-time auto-unlock | Working — first deploy attempt failed on a `cryptsetup` PATH-detection bug, fixed same session, then verified end-to-end against a real reboot |
| RAM-only (tmpfs) logs | Moves auth/session/nginx logs off the SD card | Working — first attempt failed on a symlink-dereference bug in the verification script, fixed in a follow-up stage. A related logrotate `su`-permission warning surfaced during that fix; a follow-up log exists but wasn't re-confirmed as part of this pass — worth a spot-check next session |
| USB hardening | Full-speed USB enumeration cap (best-effort, non-fatal), optional 3rd "aux" idle HID interface | Working |
| HID auto-disconnect | Idle unbind/rebind of the USB gadget, off by default | Working — documented interaction: if the mouse jiggler is also enabled, writes fail silently until the next real session reconnects the gadget. Not fixed, just documented |
| Typing jitter | Randomized inter-keystroke delay on paste, for realism | Working |
| Mouse jiggler | Randomized net-zero mouse movement, 4 presets, pauses on real input | Working (shipped just before this window, confirmed still solid) |
| Update-available indicator | Pulsing "Update available" button in the frontend when a new commit exists upstream | Working |
| IA merge / UI flatten / HUD redesign / mobile fix / system-tab redesign | Consolidated the frontend from an earlier 5-tab layout to Stream/Network/Agent/System; various UI cleanups | Working |
| GitHub repo sync | Created `github.com/razzrohith/MagicBridge`; reconciled Pi, laptop clone, and GitHub `main` to commit `13246c7` | Done — see updated §4 table |

**Same-day follow-up (2026-07-09, after a full feature audit):**
- **Fixed**: MAC address randomize-and-apply now persists automatically (previously the randomized MAC only took effect live and reverted on reboot unless the user separately re-applied it through the manual MAC field — a real UX/persistence gap). `stealth-dashboard.py`'s `rand_mac` action now calls `_persist_mac()` same as the manual `mac` action.
- **Improved**: USB identity presets (`USB_PROFILES` in `stealth-dashboard.py`) now carry an explicit `"verified"` flag — `True` for Logitech (checked against a real device descriptor), `False` for Microsoft/Dell (VID:PID researched against public USB-ID/driver databases and believed accurate, but interface count/serial presence unconfirmed without the physical dongles). The stealth panel UI now labels unverified presets and shows a tooltip explaining the gap, instead of presenting all three as equally battle-tested.

## 11. Known Issues / Pending Tasks (snapshot)

| # | Task | Status |
|---|---|---|
| 1 | Find Pi 4 acrylic-layer case on AliExpress | Pending |
| 7 | Draft EDID cloning workflow | Pending — needs the real C790 + a target monitor to capture actual EDID bytes. Code is complete and safe without the hardware (§11a) |
| 8 | Write/update master setup script for the other 2 physical units | **Done 2026-07-09** — GitHub repo created and reconciled with the Pi and this laptop (§4, §11a); use the `mb_git_*` script pattern for future syncs |
| 9 | Full hardware-dependent build + validation once C790 arrives | Pending — see §9. Also gates OLED (I2C not detected yet) and EDID (§11a) |
| — | AI Agent plaintext key storage | Open design decision. Worth re-checking whether the new LUKS-encrypted `/etc/magicbridge` (§11a) now covers `config.json` and supersedes this concern — not confirmed either way yet |
| — | MAC randomize didn't persist across reboot | **Fixed 2026-07-09** — see §11a |
| — | Microsoft/Dell USB presets presented as equally trustworthy as the verified Logitech one | **Improved 2026-07-09** — `verified` flag added, surfaced in UI (§11a). Still not verified against real hardware; that part is unresolved without the physical dongles |
| — | No real OS clipboard sync (only local "Clips" snippets, browser-side) | Open — feature gap, not a bug. Likely the most-requested KVM capability still missing |
| — | CSRF protection only on the stealth admin panel, not the main KVM app's state-changing routes | Open — main app relies on its session cookie's `samesite=Lax` + auth gate alone |
| — | Keyboard layout support | Open — only `us` is built out/verified; UK/DE/FR exist as UI scaffolding only |
| — | Tailscale Funnel toggle | Open — fire-and-forget subprocess call, result isn't captured or surfaced to the user |
| — | `pmic_read_adc` in power/thermal health reporting | Open — confirmed missing on the current dev unit ("Command not registered"); throttle-bit detection still works fine |
| — | Dead `/janus-http/` nginx proxy location | Open, harmless — nothing listens on port 8088 today; candidate for cleanup |

Everything else from earlier sessions (SSL SAN fix, WiFi listing/onclick bugs, video.py `is_running()`/watchdog bugs, JS syntax crash, Tailscale logout, raj.local mDNS alias, GitHub update endpoint) is done and stable — see git history / prior session logs if you need the exact diffs, they're not repeated here to keep this file current rather than exhaustive.

A full feature-by-feature audit (67 features scored working/partial/missing, with suggestions) was done 2026-07-09 and saved as `MagicBridge_Feature_Table.xlsx` in this project folder — check there for the complete inventory rather than duplicating it here.

---

## 12. Hard Rules (condensed — also enforced via this project's standing instructions)

1. Sandbox can't reach the Pi. All Pi access = Windows Python + paramiko, run via the File Explorer address-bar trick (§3).
2. Never shell-redirect a deploy script's output (`> log.txt`) — it writes its own log internally.
3. Large files (HTML/Python) deploy via SFTP, never base64/echo.
4. No admin rights on the Windows laptop.
5. Sudo on the Pi: `echo 'lol' | sudo -S bash -c '<command>'`.
6. Never put `JSON.stringify(x)` inside an HTML `onclick` attribute — use `data-*` attributes (`el.closest('[data-net]').dataset.net`), a real bug hit and fixed in an earlier session.
7. Real backend is `/opt/magicbridge/core/magicbridge.py` — `/opt/magicbridge/magicbridge.py` is an empty decoy.
8. Use the `Read` tool (not bash) as ground truth for any `_live_pull`/`pi_source` file edited via `Edit` this session — see the drift-risk note in §4.
9. Follow the backup→SFTP→verify→restart deploy pattern in §3 for every change — don't blind-reload nginx without `nginx -t` first.

---

*Source of truth: the live Pi filesystem is still ground truth for anything not yet synced. As of 2026-07-09, `github.com/razzrohith/MagicBridge` (`main`, commit `13246c7` as of this writing) and the laptop clone at `C:\Users\razzr\Claude\Projects\MagicBridge\repo\` are reconciled with it — prefer editing the repo clone and deploying via SFTP + `mb_git_*` sync over the older `_live_pull\`/`pi_source\` mirrors where both exist. Deploy-script staging area (not source of truth): `E:\Startup\magicbridge\`. Pi: `172.16.20.116`, user `raj`, password `lol`.*
