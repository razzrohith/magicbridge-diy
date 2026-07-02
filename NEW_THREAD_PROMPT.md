# New Thread Prompt — MagicBridge Project

Copy everything below this line and paste into a new Claude Cowork session:

---

I'm continuing development of **MagicBridge**, a self-hosted KVM-over-IP system running on a Raspberry Pi 4. Full project handbook is at `E:\Startup\magicbridge\MAGICBRIDGE_HANDBOOK.md` — please read it first before doing anything.

## Quick Summary

MagicBridge lets me control a remote PC (keyboard + mouse + HDMI capture) entirely through a browser, using the Pi as a USB HID relay. The frontend is a single HTML file; the backend is Python/aiohttp behind nginx.

## Critical Constraints (read before writing any code)

1. **Linux sandbox CANNOT reach the Pi** (`172.16.20.116` — network unreachable from sandbox). ALL Pi access must go through Windows Python + paramiko scripts, run via the File Explorer address bar trick: navigate to `E:\Startup\magicbridge`, press Alt+D, type `cmd /c python E:\Startup\magicbridge\<script>.py`, press Enter. Check `<script>_log.txt` for results.
2. **Never use shell redirect for logging** (`> log.txt 2>&1` conflicts with Python's own file writer → empty log). Scripts write their own log file internally.
3. **Large files (HTML) deploy via SFTP** — not base64 echo (buffer truncation). Use `ssh.open_sftp()` / `sftp.putfo()`.
4. **No admin rights** on this Windows laptop.
5. **Sudo on Pi**: `echo 'lol' | sudo -S bash -c 'command'`
6. **Never put `JSON.stringify(x)` in HTML onclick attributes** — double quotes break attribute parsing. Use `data-*` attributes instead.

## Pi Credentials
- IP: `172.16.20.116` (DHCP — check router if unreachable)
- SSH user: `raj` / password: `lol`
- Hostnames: `magicbridge.local`, `raj.local`

## Key File Locations
- Backend: `/opt/magicbridge/core/magicbridge.py` (NOT `/opt/magicbridge/magicbridge.py` — that's empty)
- Frontend: `/opt/magicbridge/web/index.html` (local copy: `E:\Startup\magicbridge\index_v13.html`)
- nginx: `/etc/nginx/sites-enabled/magicbridge`
- SSL cert: `/etc/magicbridge/ssl/cert.pem`

## Current State
Everything is working: video stream live, WebSocket connected, WiFi panel shows all 4 saved networks with working buttons, Tailscale has Disconnect + Logout, System tab has GitHub update endpoint.

## Pending Tasks (in priority order)
1. **Deploy WiFi form fix** — HTML already edited locally (`index_v13.html`) with labels, taller inputs, visible password text. Just needs `mb_v19_deploy.py` run via address bar trick once Pi is connected.
2. **GitHub setup** — `/opt/magicbridge` is not a git repo (files deployed via SFTP). Need to push to GitHub and do `git clone` on Pi to enable the "Update from GitHub" button in the System tab.
3. **Windows hostname mDNS** — `magicbridge.local` works from Pi side (avahi active). On Windows, set the network adapter to "Private" (not "Public") in Settings → Network to enable mDNS resolution.
4. Any new features or improvements.

## What I want to do today
[FILL IN YOUR TASK HERE]

---

Please read `E:\Startup\magicbridge\MAGICBRIDGE_HANDBOOK.md` first, then help me with the task above.
