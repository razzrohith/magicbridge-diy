# CLAUDE.md — MagicBridge DIY (session entry point)

You are working on **MagicBridge DIY**: a self-hosted KVM-over-IP built from
scratch on a **Raspberry Pi 4B** with a **C790 (TC358743) HDMI→CSI-2** capture
board. Read `docs/MAGICBRIDGE_SYSTEM.md` first — it is the authoritative brain
(purpose, the anonymity model, the two-project architecture, full history,
roadmap, and how this repo relates to `magicbridge-pikvm`).

## What this project is
Free/private alternative to TinyPilot/PiKVM. Raj uses it to control **his own
second computer**. The whole point is **stealth on a machine he owns**: when the
device is plugged into the target, the target must not detect a KVM/Pi. Preserve
the anonymity model (`MAGICBRIDGE_SYSTEM.md` §2) through every change.

## The device
- Pi @ **172.16.20.116**, SSH **`raj` / `lol`**. Sudo: `echo 'lol' | sudo -S bash -c '…'`
- Real backend: **`/opt/magicbridge/core/magicbridge.py`** (NOT `/opt/magicbridge/magicbridge.py`, which is empty).
- `/opt/magicbridge` on the Pi is **NOT a git repo** → deploy by **SFTP** (large
  files via `sftp.putfo`, never base64 echo — it truncates).
- Capture: **1080p50 max** (Pi 4B has only 2 CSI lanes; 1080p60 is physically
  impossible here). EDID auto-applies at boot via `mb-hdmi-init` +
  `mb-hdmi-watch`; a restricted EDID caps any source at 1080p50 (`src/edid/`).

## Repo layout
- `src/core/` — backend (`magicbridge.py`, `hid.py`, `video.py`, `oled.py`), services
- `src/dashboard/` — stealth dashboard, `mb_edidconf.py`
- `src/web/` — the web UI (`index.html`)
- `src/edid/` — restricted 1080p50 EDID + generator
- `src/provision/`, `src/nginx/` — install/serve
- `MAGICBRIDGE_HANDBOOK.md` — original design handbook
- `docs/MAGICBRIDGE_SYSTEM.md` — shared system brain (see it first)
- `docs/DIY_PROGRESS.md`, `docs/DIY_ROADMAP.md` — timeline + tasks
- ⚠️ The repo ROOT is cluttered with hundreds of old one-off `mb_*.py` /
  `_commit_msg*` / `live_*` scratch files. **Ignore them; the real code is `src/`.**
  (A cleanup that `git mv`s these into `_archive/` would be welcome but is RISKY —
  propose before doing.)

## How to work
- Reach the Pi over SSH from the native shell (the sandbox can't reach the LAN).
- After a change: test on the Pi, then commit + `git push` (remote already set).
- To see how MagicBridge PiKVM solves something, read the sibling repo at
  `E:\Startup\magicbridge-pikvm` (see `MAGICBRIDGE_SYSTEM.md` §8).

## Safety (always)
SAFE (in-project, reversible, no system impact) → do it. RISKY (OS/system files,
files outside the project, security/network settings, irreversible loss, or a
hard-to-undo change to the live Pi) → **stop, state what/why/impact, wait for an
explicit yes.** Never weaken the anonymity model. Deploys to the Pi: say exactly
what deploys; routine UI/file pushes are SAFE, anything that could brick it or
change its boot/network is RISKY.

## Right now
Video works at 1080p50 (portable, auto-configures on boot). Open items:
retest partial frames on wall power; **Janus/WebRTC integration is the next big
task** (real low-latency win); I2S audio is a parked upstream-driver bug.
