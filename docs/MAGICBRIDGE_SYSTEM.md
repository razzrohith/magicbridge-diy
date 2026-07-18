# MagicBridge — System Brain (authoritative, shared by both repos)

> This file is the single source of truth for **what MagicBridge is, why it exists,
> how the two projects relate, and everything learned so far.** It is copied
> byte-for-byte into BOTH repos (`magicbridge-diy` and `magicbridge-pikvm`) at
> `docs/MAGICBRIDGE_SYSTEM.md`. When you change it in one repo, port it to the
> other (see §8). Last updated: 2026-07-18.

---

## 1. What MagicBridge is (purpose)

MagicBridge is a **self-hosted KVM-over-IP** appliance — a free, private, DIY
alternative to commercial units like [TinyPilot](https://tinypilotkvm.com/) and
[PiKVM](https://pikvm.org/). It lets you **see the screen, and control the
keyboard + mouse, of a target computer over the network** — even when that
computer is off, in BIOS/UEFI, or has no OS — through a browser, with no agent
installed on the target.

The owner ("Raj") controls **his own second computer** with it. So the guiding
constraint is not "break into machines" — it is **stealth on a machine you own**:
when the device is plugged into the target, the target must see nothing unusual.

Core capabilities:
- **Video**: capture the target's HDMI output and stream it to a browser.
- **HID**: emulate a USB keyboard + mouse to the target (USB gadget mode).
- **Web UI**: a self-hosted control panel (Python/aiohttp + nginx).
- **Stealth suite**: USB-identity spoofing, MAC spoofing/randomisation, human-like
  typing, and stripping of every "this is a KVM / a Pi / PiKVM" tell (see §4).
- **Extras**: WiFi management, Tailscale remote access, DuckDNS, an AI
  natural-language macro executor, clipboard sync, a mouse jiggler, an OLED
  status panel.

---

## 2. The anonymity / untraceability / undetectability / spoofing model

This is the heart of the project and must be preserved through every change.
**Goal: a target computer the device is plugged into cannot tell a KVM/Pi is
attached, cannot identify the product, and cannot trace it back.**

**USB identity (undetectable HID).** The USB gadget presents by default as a
**Logitech USB Receiver — VID `0x046d`, PID `0xc52b`, no serial, 3 HID
interfaces**. This is a real, common device that legitimately carries both
keyboard and mouse, so it raises no suspicion. Identity is fully spoofable
(manufacturer / product / VID / PID / serial) from the stealth panel. USB
enumeration is capped at full-speed to look ordinary.

**Human-like input.** Typing uses randomised inter-keystroke delay ("typing
jitter") so pasted text doesn't look machine-generated. Optional HID
auto-disconnect idles the gadget so it isn't a permanently-attached device.

**Monitor identity (EDID).** The capture EDID is spoofable so the target sees a
realistic monitor, not a capture card. (DIY's `mb_edidconf.py` clones identity
fields only, never raw timings — see §5.)

**Network anonymity.** MAC address spoofing + randomisation (persists across
reboot). No hostname/mDNS strings that reveal "pikvm"/"kvm"/"raspberry".

**No product tells.** The web UI, hostname, OLED, and USB descriptors carry NO
"PiKVM", "kvmd", "Raspberry Pi", or version strings. On-screen brand is plain
**"MagicBridge"**. The main web page is deliberately **view-only**; the real
stealth/identity controls live behind the **stealth panel** (edit mode).

**Data at rest.** `/etc/magicbridge` is **LUKS-encrypted** with boot-time
auto-unlock; auth/session/nginx logs are **RAM-only (tmpfs)** so nothing
sensitive is written to the SD card.

**Attribution kept (legal).** Upstream credit (`PiKVM / kvmd (GPLv3)`) stays in
LICENSE/NOTICE — that is a licence obligation, not a user-facing tell, and is
deliberately NOT rebranded.

> ⚠️ Any new feature MUST be checked against this model. If it adds a USB
> descriptor, a network string, a log on disk, or an on-screen "KVM/Pi" tell,
> it breaks anonymity and needs a stealth-safe design first.

---

## 3. The two projects (why there are two)

There are TWO independent builds of MagicBridge that share the same mission,
UI language, and stealth model but run on different hardware/software stacks.

```
                       MagicBridge (mission + stealth model + UI language)
                                  │
                ┌─────────────────┴──────────────────┐
                │                                     │
        MagicBridge DIY                       MagicBridge PiKVM
        repo: magicbridge-diy                 repo: magicbridge-pikvm
        Pi @ 172.16.20.116 (raj/lol)          Pi @ 172.16.20.209 (root/root)
        Raspberry Pi 4B, from scratch          PiKVM V4 Mini, fork of kvmd
        C790 (TC358743) CSI capture            kvmd-native capture + Janus
        + LED + fan + OLED                     PiKVM OS base, rebranded
        bamboo 3D-printed case                 "MagicBridge" over kvmd
        code: src/core/magicbridge.py          code: kvmd fork + our services
        stack: Python/aiohttp + nginx          stack: kvmd + our add-on layer
        WORKED IN: Claude Code (this repo)     WORKED IN: Cowork + Claude Code
```

**DIY** = the original hand-built unit. Everything is ours, on bare Raspberry Pi
OS. Maximum control, more wiring, the C790 capture path. This is "V1" in old docs.

**PiKVM** = a port of the MagicBridge experience onto the mature PiKVM/kvmd
platform (fork + rebrand + our stealth/agent/UI layer). This is "V2" in old docs.
It reuses kvmd's battle-tested video/HID/WebRTC and adds our features on top.

Old "V2/V3" version labels are **RETIRED** — use the platform names above.

---

## 4. Hardware, network & access map

| | DIY | PiKVM |
|---|---|---|
| Board | Raspberry Pi 4B | PiKVM V4 Mini (CM4) |
| Pi IP | 172.16.20.116 | 172.16.20.209 |
| SSH | `raj` / `lol` | `root` / `root` |
| Capture | C790 HDMI→CSI-2 (TC358743) | kvmd-native |
| Video ceiling | 1080p**50** (2 CSI lanes) | up to 1080p60 (CM4, 4 lanes) |
| Install root | `/opt/magicbridge` (NOT a git repo — deploy via SFTP) | `/opt/magicbridge` is a git tree (deploy via `align_pi.py` git reset) |
| Real backend | `/opt/magicbridge/core/magicbridge.py` | kvmd + `/opt/magicbridge/services/*` |
| Sudo on Pi | `echo 'lol' \| sudo -S bash -c '…'` | root (no sudo needed) |

**How to reach the Pis (Claude Code):** use your **built-in shell/terminal
directly** — run `ssh` / `scp` / `git` / paramiko yourself. You do **NOT** need
Desktop Commander or any MCP shell bridge. (Desktop Commander was only used while
this project was built in *Cowork*, which has no native shell and needed a
bridge. Ignore any "Desktop Commander" references in older notes — they don't
apply to Claude Code.) Just use the real host shell; the Pis are on the LAN at
the IPs above.

**Safety in Claude Code** comes from two places: the SAFE/RISKY rules in each
repo's `CLAUDE.md`, and Claude Code's own permission prompts (it asks before
running commands / editing files). The Cowork Desktop-Commander allowlist and
blocked-command list do NOT apply here and are not needed.

**Dev locations (Windows host):**
- `E:\Startup\magicbridge-diy` — DIY git repo
- `E:\Startup\magicbridge-pikvm` — PiKVM git repo
- `E:\Startup\magicbridge-scratch` — throwaway temp files
- `C:\Users\razzr\Claude\Projects\MagicBridge` — Cowork workspace (PiKVM build)

**GitHub:** `razzrohith/magicbridge-diy`, `razzrohith/magicbridge-pikvm`
(renamed 2026-07-17 from `MagicBridge` / `MagicBridgeV2`; old names redirect).

---

## 5. Timeline — from scratch to now (with problems + solutions)

**Phase 0 — DIY V1 (Raspberry Pi 4, from scratch).** Hand-built KVM on bare Pi
OS: USB gadget HID, ustreamer MJPEG capture via an MS2109 USB dongle, Python web
server. Added the full stealth suite (USB/MAC spoofing, typing jitter, HID
auto-disconnect), RAM-only logs, LUKS encryption of `/etc/magicbridge`, OLED
status, fan control, WiFi management, Tailscale, DuckDNS, AI macro executor.
Notable fights solved: mDNS/hostname breakage (self-healing avahi fix),
Tailscale Funnel (3 stacked bugs: silent Popen, CLI hang, invalid flag),
nginx RAM-log permission bug, MAC-randomise-not-persisting.

**Phase 1 — PiKVM V2 port.** Forked kvmd, rebranded to MagicBridge, rebuilt the
UI as a professional cockpit, ported the stealth suite, wired WebRTC/H.264 via
Janus, added captive-portal WiFi. Restored "the real MagicBridge soul" after an
early port felt generic (human typing, real cyberpunk stealth suite). Janus
version mismatch fixed (use kvmd's own janus.js). Deferred: OSK, clipboard,
EDID, VNC, 2FA, WebRTC transport verify, login rebrand.

**Phase 2 — Naming + infra cleanup (2026-07-17).** Retired V2/V3 labels. Renamed
GitHub repos + E:\ folders + remotes to `magicbridge-pikvm` / `magicbridge-diy`.
Product on-screen brand `MagicBridgeV2` → `MagicBridge` (pushed; PiKVM device
deploy pending it being online). Set up host safety guardrails (Desktop Commander
allowlist locked to project folders + blocked-commands; SAFE→auto /
RISKY→stop-and-ask policy in CLAUDE.md + Personal Preferences).

**Phase 3 — DIY C790 capture bring-up (2026-07-18).** Installed the C790
HDMI→CSI-2 board on the DIY Pi to replace the USB dongle (direct DMA, enables
hardware H.264). What happened, in order:
- Enabled `dtoverlay=tc358743` (+audio), `camera_auto_detect=0`. C790 probed at
  i2c `0x0f`; `/dev/video0` appeared.
- **Problem: 1080p60 impossible.** Pi 4B's camera port has only **2 CSI lanes**;
  1080p60 needs 3–4. Kernel logs `Device has requested 3 data lanes`. **Solution:
  cap the source at 1080p50** (confirmed max for 2 lanes, UYVY).
- **Problem: no signal after reboot.** The TC358743 stores EDID only in RAM, so
  every reboot left the source with no sink → no video. **Solution: `mb-hdmi-init`
  service** applies EDID + locks timings at boot (commit `606a44c`). Cost me a
  self-inflicted **systemd ordering-cycle** bug that silently killed 3 services
  (`After=multi-user.target` + `Before=magicbridge` → cycle) — fixed with
  `After=local-fs.target`.
- **Problem: partial frames (~93% filled, green band).** Ruled out CMA, kernel
  errors, subdev format, media links. Leading suspect = **power** (under-voltage
  `0x50000` on the laptop USB port). Retest on the splitter's wall power (pending).
- **Problem: I2S audio fails (`Input/output error`).** Proved wiring good
  (pin-level toggling) and that the chip receives audio (`audio_present` 0→1), but
  ALSA always EIOs. Decompiled the overlay — config is the stock correct one.
  **Conclusion: known upstream driver bug** (driver never programs the chip's I2S
  output registers). Deliberately NOT fixed (would risk the working video path).
- **Portability (commit `ecf0870`).** Built a **restricted EDID** advertising
  1080p50 as native and omitting 1080p60, so ANY target auto-negotiates a
  capturable mode with no manual per-laptop step. Init script made
  resolution-agnostic; added `mb-hdmi-watch` hot-plug watchdog. Verified across
  cold boots.

See each repo's `docs/` (DIY) and `brain/05_DEBUG_JOURNAL.md` (PiKVM) for the
blow-by-blow. Full auto-memory history lives in the Cowork memory store.

---

## 6. Current status (2026-07-18)

**DIY:** Video capture WORKS at 1080p50 (auto-configures on boot, portable across
targets). HID gadget works. OLED works. All services healthy. **Open:** partial
frames (retest on wall power), I2S audio (upstream bug — parked), Janus/WebRTC
integration (the low-latency payoff — next big task).

**PiKVM:** Rebrand code committed; **deploy to Pi 209 pending device online**.
Feature port largely done (UI, stealth, WiFi, WebRTC); see its trackers.

---

## 7. Roadmap / future goals / ideas

1. **Janus/WebRTC + H.264** (both, but DIY first) — the real low-latency win
   (<100 ms vs MJPEG's 100–300 ms). This is where "fast + clear" is actually won.
2. **Retest DIY 1080p50 on clean wall power** — expected to fix partial frames.
3. **Deploy PiKVM rebrand** to Pi 209 when it's online (one `align_pi.py`).
4. **EDID persistence + portability** — done on DIY; port the *idea* to PiKVM.
5. **I2S audio** — revisit only if upstream lands a driver fix.
6. **Feature parity pass** — reconcile the DIY↔PiKVM feature matrix (see §8).
7. **Multi-unit provisioning** — master setup script for additional physical units.
8. **AI Agent tab** — deliberately hidden behind a flag; reveal when ready.

---

## 8. Cross-project workflow (how the two stay in sync)

The two repos are **siblings, not a monorepo**. Both are cloned side-by-side on
the host: `E:\Startup\magicbridge-diy` and `E:\Startup\magicbridge-pikvm`.

**Reading the other project from a Claude Code session.** A session opened on one
repo can still read the other — it's just another folder on disk
(`E:\Startup\magicbridge-<other>`). When you ask it to "see how the other project
does X", point it there; it can open, grep, and copy from the sibling repo.

**Porting a feature from one to the other.** When a feature/patch/fix is added in
one repo and should exist in both:
1. Implement + test in the origin repo; commit.
2. In the target repo's session, read the origin implementation from
   `E:\Startup\magicbridge-<origin>\…`, adapt it to the target's stack (DIY =
   bare Python/aiohttp; PiKVM = kvmd services), and re-test on THAT Pi.
3. **Never blind-copy** — the stacks differ. Copy the *idea and the stealth-safe
   design*, not necessarily the code.
4. Update BOTH this `MAGICBRIDGE_SYSTEM.md` files and the per-repo trackers.

**Parallel sessions.** Run two Claude Code sessions at once, one per repo. They
don't collide (different folders, different Pis). If a change touches shared
docs, make it in one, then have the other pull/read it. Keep git clean in each so
`git pull` between them stays trivial.

**Shared invariants (both must honour):** the anonymity model (§2), the naming
(§3), the safety policy (each repo's `CLAUDE.md`), and this document.

---

## 9. Safety rules (apply in every session)

Before any write/delete/deploy/command, classify it: **SAFE** (inside the
project, reversible, no system impact) → just do it. **RISKY** (touches the OS,
files outside the project, security/network settings, irreversible loss, or a
hard-to-undo change to a live Pi) → **stop and explain what/why/impact, wait for
an explicit yes.** Never break the anonymity model. Live-device deploys: state
exactly what will deploy; routine UI/file redeploys are SAFE, anything that could
brick the Pi or change its network/boot is RISKY.

---

## 10. Working style & product direction (Raj's preferences)

- **Feature taste:** prefer creative, codebase-grounded ideas over generic
  "security checklist" items. Ground every suggestion in how MagicBridge actually
  works, not boilerplate.
- **Dependency-free:** keep implementations self-contained — **no CDN**, no
  runtime downloads, minimal third-party deps. The device runs offline / on
  hostile networks; everything must work without reaching out.
- **Scope exclusion — NO virtual media.** Do **not** add ISO/virtual-media
  mounting (mounting disk images to the target). It's deliberately out of scope.
- **AI Agent tab** stays hidden behind a single feature flag until explicitly
  ready — do not surface it by default.
- **Verify, don't assume.** Prove changes on the live Pi (read the value back,
  capture a frame, check service state) rather than trusting "no error." A
  command that returns cleanly can still have done nothing (e.g. a `pkill -f X`
  whose own command line contains `X` kills its own shell — this actually bit us).
- **Communication:** concise and direct; own mistakes plainly.

## 11. Working with the Pi from a Claude Code session

- Use the **native shell + SSH** (an isolated sandbox can't reach the LAN Pis).
- **Sudo (DIY):** `echo 'lol' | sudo -S bash -c '…'`. PiKVM is root already.
- **Large files → SFTP** (`sftp.putfo` / `scp`), never base64-echo (truncates).
- **paramiko gotcha:** `exec_command(timeout=)` does NOT bound `stdout.read()`;
  set `channel.settimeout()` on the channel or a long read can hang forever.
- **Deploy paths differ:** DIY `/opt/magicbridge` is NOT a git repo → SFTP the
  changed files. PiKVM `/opt/magicbridge` IS a git tree → `align_pi.py`.
- **Never shell-redirect a Python script's own log** (`> log.txt`) when the
  script also writes that file — you get an empty log. Let scripts own their logs.
- After a working change: commit + `git push` in that repo (remotes are set).

## 12. This document IS the memory now

This project was built across a long Cowork chat with a rich private memory
(every gotcha, the systemd trap, the audio dead-end, the naming history). **That
chat and its memory are NOT available to these Claude Code sessions.** Everything
load-bearing has been distilled into: this file, each repo's `CLAUDE.md`, DIY's
`docs/DIY_PROGRESS.md` + `docs/DIY_ROADMAP.md`, and PiKVM's existing `brain/01–07`
+ trackers. **Treat these as the source of truth**, and whenever you learn
something new or non-obvious, write it back here (and port to the sibling repo) so
it survives the next session.

**Two-session hygiene:** run one session per repo — they don't collide (different
folders, different Pis). The ONE shared file is `docs/MAGICBRIDGE_SYSTEM.md`: edit
it in only one session at a time, then have the other read/pull it, or the two
copies diverge and need a manual merge.
