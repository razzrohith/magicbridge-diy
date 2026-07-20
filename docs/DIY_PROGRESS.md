# MagicBridge DIY — Progress & Debug Journal (C790 capture path)

Detailed, DIY-specific technical record. For the big picture see
`MAGICBRIDGE_SYSTEM.md`. This file is the deep hardware/driver journal so nobody
re-derives it. Newest first.

## 2026-07-18 — install.sh made fresh-install-complete + idempotent (+ --check doctor)

The installer only set up the MJPEG-era base (HID + web UI + stealth + WiFi +
TLS + nginx + 5 services); the C790 video path, EDID services, OLED, RAM-only
logs, and WebRTC had all been done by hand on the dev unit and never folded in.
Closed the gaps so a fresh SD card → one script → working unit:
- **config.txt** (idempotent `set_cfgtxt`): `camera_auto_detect=0`,
  `dtoverlay=tc358743(+-audio)`, `dtparam=i2c_arm=on` (was dwc2 only).
- **Files**: copy `oled.py`, the 1080p50 EDID blob, `mb-hdmi-init.sh`,
  `mb-setup-fan.sh`; `pip install luma.oled`; stage `install_janus_webrtc.sh`.
- **Services**: enable `mb-hdmi-init`, `mb-hdmi-watch`, `mb-oled`.
- **RAM-only logs**: add the `/var/log/magicbridge-ram` tmpfs to fstab + mount
  it **before nginx** (nginx.conf logs there; ordering matters or `nginx -t` dies).
- **`--with-webrtc`** flag builds Janus/H.264 and flips `video.mode` to h264.
- **`--check`** read-only doctor reports every component's state; **canonical
  repo URL** pinned (was the pre-rename `MagicBridge` name).

Validated: `bash install.sh --check` on the live unit is **all-green** (my
installer's target state == what actually works, LUKS included). LUKS at-rest
encryption is deliberately NOT auto-applied (risky to do blind) — documented as
the one manual hardening step; the rest of the anonymity model (RAM logs,
spoofable identity) is set up. Full clean-Pi run still wants a spare board to
prove end to end. Commit: see git log.

## 2026-07-18 — Janus WebRTC + H.264: the missing plugin, built & wired (server-verified)

The C790→ustreamer→`/dev/shm/magicbridge::h264` memsink was already live; the one
gap was the **Janus `libjanus_ustreamer.so` plugin** (Janus had only voicemail).
Built it and wired the whole path end-to-end. Four real problems, each solved:

1. **`janus-gateway.pc` missing.** ustreamer's `janus/Makefile` gets its cflags via
   `pkg-config janus-gateway`, which is what pulls in glib's include path (the
   janus headers `#include <glib.h>`). Janus's install left no `.pc` →
   `glib.h: No such file`. **Fix:** wrote `/opt/janus/lib/pkgconfig/janus-gateway.pc`
   (`Requires: glib-2.0 jansson`) + built with `PKG_CONFIG_PATH` set.
2. **`abs_capture_ts` API skew** (the deep failure the installer warned about).
   ustreamer (even v6.61) sets `packet.extensions.abs_capture_ts`, a field absent
   from the installed **Janus v1.0.0** `janus_plugin_rtp_extensions` struct. Root
   cause: the installer pins Janus but cloned ustreamer at HEAD. **Fix:** pin
   ustreamer to **v6.61** (== running binary → same memsink protocol) and neuter
   that one assignment (non-essential capture-time RTP ext; video unaffected).
3. **Config-dir bug.** Installer wrote the jcfg + pointed `janus-webrtc.service`
   `--configs-folder` at `/opt/janus/lib/janus/configs`, which **never existed**;
   the gateway reads `/opt/janus/etc/janus` (its compiled-in default). The unit
   was flapping (`activating`) as a result. **Fix:** correct dir; retired the
   stray apt `janus.service`, made `janus-webrtc.service` canonical.
4. **Wrong jcfg key.** v6.61's plugin wants **`video.sink`** (per its
   `janus/src/config.c`), not the docs' `memsink.object` → "Missing config value:
   video.sink". **Fix:** `video: { sink = "magicbridge::h264" }`, video-only (no
   `acap` block — I2S audio is the parked upstream dead-end; a bad ALSA device
   would just stop the plugin loading).

**Verified (headless, since the sandbox/browser can't reach the LAN):** a raw-WS
probe drove Janus `create→attach(janus.plugin.ustreamer)→watch` and got back a
**JSEP offer with `m=video … 96`, H264=true**; the plugin log flipped
`Using vcap-sink: magicbridge::h264` → `Memsink opened; reading frames` →
`Memsink closed` on stop. So capture→H.264→memsink→plugin→WebRTC SDP offer all
work. **Boot-persistent:** `janus-webrtc` enabled, old `janus` disabled,
`magicbridge` enabled, nginx proxies `/janus-ws`→8188. **Left for on-site:** a
real browser decoding the stream (ICE/DTLS/decode via the vendored `janus.js`);
and confirm after a natural reboot.

Anonymity: STUN stays commented out in `janus.jcfg` (host/LAN candidates only, no
external phone-home); Janus is 127.0.0.1-only behind nginx; no new USB/UI/network
tell. `install_janus_webrtc.sh` updated to reproduce all of the above.

Live changes (all persistent): `/opt/janus/lib/pkgconfig/janus-gateway.pc`,
`/opt/janus/lib/janus/plugins/libjanus_ustreamer.so`,
`/opt/janus/etc/janus/janus.plugin.ustreamer.jcfg`,
`/etc/systemd/system/janus-webrtc.service` (backup `.bak`).

## 2026-07-18 — WiFi provisioning hotspot fix (dnsmasq :53 conflict)

Ported the root-cause class from the PiKVM captive-portal saga (its bug #3) after
Raj hit "auto WiFi hotspot / no wifi found" there. The DIY setup AP
(`mb-provision.sh` → `hostapd` + `dnsmasq` on `wlan0`, SSID **MagicBridge-Setup**)
had the same latent defect, proven live on a **dummy interface** (wlan0 untouched):

- The image ships a **standalone `dnsmasq.service` enabled and running**, bound to
  the **wildcard `0.0.0.0:53`** (it's the box's DNS). The provisioning script
  launched its own `dnsmasq` for the AP's DHCP + captive-DNS redirect
  (`address=/#/192.168.73.1`) but on the wildcard `:53` too → **`EADDRINUSE`, the
  AP dnsmasq dies → clients on MagicBridge-Setup get no IP and no portal** (a
  hotspot that appears but is dead).
- **`bind-dynamic` alone does NOT fix it** — a specific-IP bind still collides
  with a wildcard holder (verified: TEST B failed the same way). The load-bearing
  fix is to **stop the system `dnsmasq.service` for the duration of provisioning**
  and restart it in teardown. `bind-dynamic` + `except-interface=lo` added too, to
  match PiKVM and avoid ever grabbing the `lo`/wildcard socket.
- Also added `rfkill unblock wifi` before `hostapd` (a soft-blocked radio would
  make the setup hotspot never appear at all — the literal "no wifi found").

Test proof (dummy iface `mbtest0` @ 192.168.73.1, `wlan0` never touched):
`OLD config + system dnsmasq up → EADDRINUSE` · `NEW bind-dynamic + system dnsmasq
up → still EADDRINUSE` · `NEW config + system dnsmasq stopped → dnsmasq serves,
DHCP .10–.50 on :67, bound 192.168.73.1:53`. Cleanup restored dnsmasq + DNS.

Deployed to `/usr/local/bin/mb-provision.sh` (backup
`mb-provision.sh.bak.wifi-fix`). DIY differs from PiKVM: **NetworkManager**
(`nmcli`, not `wpa_supplicant`) so the `wpa_passphrase` bug is N/A; **read-write
rootfs** so the `rw`/`ro` recursion bug is N/A; the portal (`mb-setup-ui.py`)
already ends only on a real `/setup` POST + strips newlines + escapes the SSID.

**Untested remotely (needs Raj on-site — triggering the AP drops wlan0, our only
link):** the full `hostapd` broadcast → phone join → portal submit → `nmcli`
connect flow. Only the dnsmasq DHCP/DNS bug (the actual failure) was reproduced +
fixed here. **Open note:** why a standalone `dnsmasq.service` holds wildcard `:53`
on a NetworkManager box is unexplained — likely image cruft; left in place (it's
the current DNS), the patch stops/restarts it cleanly either way.

## 2026-07-18 — C790 (TC358743) HDMI→CSI-2 bring-up

Replaced the MS2109 USB dongle with a **Geekworm C790** board (direct CSI/DMA,
enables hardware H.264). The C790's video path is the 15-pin CSI ribbon into the
Pi's **CAMERA** port (NOT the GPIO header). Audio is a separate 4-wire I2S cable.

### Driver enable
`/boot/firmware/config.txt`: `camera_auto_detect=0`, `dtoverlay=tc358743`,
`dtoverlay=tc358743-audio`. Backup: `config.txt.bak.20260718-075927`.
Probes as `tc358743 10-000f ... found @ 0x1e`; `/dev/video0` via unicam;
formats BGR3 (24bpp) / UYVY (16bpp) / RGB3.

### FACT — 1080p60 is physically impossible on Pi 4B
The Pi 4B CAM connector has only **2 CSI-2 data lanes**. 1080p60 YUV422 ≈ 2.37
Gbps > ~2 Gbps that 2 lanes carry, so the driver requests 3 lanes and unicam
refuses: `unicam fe801000.csi: Device has requested 3 data lanes, which is >2
configured in DT`, then `VIDIOC_STREAMON: Invalid argument`. Confirmed by
Raspberry Pi docs AND the Geekworm/BliKVM wikis: **2 lanes → max 1080p30 (BGR3)
or 1080p50 (UYVY)**. 1080p60 needs 4 lanes = CM4/Pi5 only. **We run 1080p50 UYVY.**
Pixel format MUST be UYVY (BGR3 can't reach 1080p50 on 2 lanes).

### GOTCHA — EDID is volatile (biggest trap)
`v4l2-ctl --set-edid` lives only in the chip's RAM. After ANY reboot there is NO
EDID → the source sees no valid sink → sends nothing →
`--query-dv-timings` = "Link has been severed", 0x0. **Every reboot needs EDID
re-applied.** SOLVED — see the service below.

### FIX — persistent + portable capture (commits 606a44c, ecf0870)
- **`/usr/local/bin/mb-hdmi-init.sh`** (+ `mb-hdmi-init.service`, oneshot,
  `Before=magicbridge.service`): at boot applies a **restricted EDID**
  (`/opt/magicbridge/edid/mb-edid-1080p50.hex`), waits for signal, locks DV
  timings, forces UYVY. Resolution-agnostic (adopts whatever the source sends).
- **Restricted EDID** (`src/edid/gen_edid.py` builds the hex): advertises
  **1080p50 as native and OMITS VIC 16 (1080p60)** so ANY target (Intel/AMD/
  NVIDIA, any panel) auto-negotiates a capturable mode with **zero manual per-
  laptop setup**. BIOS/UEFI fallbacks kept: 1080p30/25/24, 720p50/60, 576p50,
  480p60, 640x480. Monitor name "MagicBridge". Checksums computed + verified.
- **`mb-hdmi-watch.service`** (daemon): re-arms EDID/timings on unplug/hot-swap.
- Verified across cold boots: EDID auto-applied ~2s after boot, capture 50.7 fps.

### GOTCHA — systemd ordering cycle (self-inflicted, cost 3 services)
First version of `mb-hdmi-init.service` had `After=multi-user.target` AND
`Before=magicbridge.service`. Since magicbridge is pulled in BY
multi-user.target, that's an **ordering cycle**; systemd silently *deletes* jobs
to break it — it killed **magicbridge, mb-oled, stealth-dashboard**. The tell:
services "inactive (dead)" with an EMPTY journal and `systemctl --failed` clean
(= never attempted, not failed). Evidence: `journalctl -b | grep "ordering
cycle"`. **FIX: use `After=local-fs.target`, never `After=multi-user.target` in a
unit ordered `Before=` another multi-user service.**

### OPEN — partial frames (~93% filled, green band at bottom)
Frames arrive at full 50 fps with no kernel errors, but ~7–40% of each frame is
zeros (varies frame to frame). Ruled out: CMA (512 MB alloc / 490 free), kernel
errors (none), subdev pad format (correct `UYVY8_1X16/1920x1080`), media links
(enabled), lane count (fits at 50 Hz). **Leading suspect = power:**
`vcgencmd get_throttled` returned `0x50000` (under-voltage occurred) while
running off the laptop USB port. **Action: retest on the splitter's 5V wall power
(hardware pending).**

### PARKED — I2S audio (`arecord: Input/output error`), known upstream bug
Wiring PROVEN good (pin-level: BCK/DATA toggle; LRCK static-low until audio plays
then toggles) — BCK→pin12/GPIO18, LRCK→pin35/GPIO19, DATA→pin38/GPIO20,
GND→pin39. Chip PROVEN to receive audio: V4L2 `audio_present` flips 0→1 when
sound plays, `audio_sampling_rate=48000`. Yet arecord EIOs on every
format/rate/hw|plughw, even with audio present AND video streaming. Decompiled
`tc358743-audio.dtbo`: DAI config is the stock correct one (format i2s, codec is
clock master). **Root cause is upstream: the V4L2 driver does audio *detection*
only and never programs the chip's I2S output registers. No published fix
(RPi forums t=314944, t=258742, t=120702).** Deliberately NOT attempting raw I2C
register writes — would risk the working video path. Revisit only if upstream
fixes it. Audio is a nice-to-have for a KVM; `video.py` runs fine without it.

## Distributable image: zero + shrink + xz, and boot/first-boot hardening (2026-07-19)
Ported the IDEA (not the code) of the PiKVM sibling's imaging work. **DIY's stack
is the opposite in the way that matters:** 2 partitions, rw rootfs, and the
**last partition IS the rootfs** — so shrinking resizes root itself and the
first-boot re-grow is boot-critical. (The sibling shrinks a trailing media
partition on a read-only Arch root, and correctly rejects pishrink; pishrink is
the *right* tool here precisely because DIY is Pi OS and root is last.)

`build-image.sh` is now a four-stage pipeline — `arm` → `--verify` → `--shrink`
→ `--compress`. It identifies partitions **by content, never by index**,
**hard-fails on LUKS** (arming an encrypted unit silently stripped nothing and
shipped a shared key), **zeroes free space on every partition** before shrinking
(deleting a file doesn't erase its blocks — DIY's de-LUKS left the 64 MB
container and a plaintext config backup recoverable), and **self-heals the
first-boot logic from the repo** so a golden unit predating the fixes can't ship
stale behaviour. `--verify` runs 19 assertions and exits 1 on any failure; it
genuinely discriminates (it failed the older armed image on exactly the 2 new
hardening items).

Hardened against every failure the sibling hit on real hardware: `/boot/firmware`
is now `nofail` (an inconsistent non-essential mount blocked the ENTIRE boot);
the first-boot done-marker is written, synced and **verified**, and
`mb-secret-reset` honours `MB_KEEP_WIFI` so a stray re-run can never wipe a
provisioned unit's WiFi (the endless "join hotspot" loop); and slow/live-device
work moved to a new **`mb-firstboot-late.sh`** — post-boot, marker-guarded,
non-blocking — which does an **online** rootfs grow (never unmounts root) and
gives each unit a **unique EDID serial** (every DIY unit previously shipped an
identical Dell EDID, cross-linking units). Every late-stage failure path exits 0.
`mb-rescue.ps1` diagnoses/fixes a unit stuck on its hotspot **offline**, since
joining that hotspot kills the laptop's internet.

**END-TO-END FLASH TEST PASSED (2026-07-19).** A blank 256 GB card flashed from
`magicbridge.img.xz` (910 MB): first boot personalized into a UNIQUE unit —
hostname `DESKTOP-HEF7EYZ`, MAC `00:14:22:a3:08:4f`, its own SSH host key,
machine-id, and (new) a randomized EDID serial `0xaebb6434` — all differing from
the golden unit. **Root grew from the shrunk 4.4 GB to `255.8 GB`, filling the
card** (pishrink's hook did it; `mb-firstboot-late` correctly no-op'd as the
safety net). `/boot/firmware` came up `nofail`; under-voltage was flagged in the
late log; both first-boot markers present (no loop).

**The test caught one real bug (fixed):** a fresh flash came up on WiFi (OLED
showed its IP) but SSH/80/443 were all dead. Cause — the image ships with SSH
host keys + TLS cert STRIPPED, so sshd/nginx fail early in boot before
`mb-secret-reset` recreates them, and secret-reset never restarted them (same
class as the sibling's stripped-cert bug). Fixed: `mb-secret-reset` now restarts
ssh/nginx/magicbridge/stealth-dashboard after regenerating their secrets, so a
fresh flash is reachable with no manual reboot. (The unit tested needed a
one-time reboot because it was flashed before the fix.)

## Earlier DIY history (V1, condensed)
Hand-built KVM on bare Pi OS: USB gadget HID (`hid.py`), MS2109 MJPEG capture,
Python/aiohttp web server. Stealth suite (USB/MAC spoof, typing jitter, HID
auto-disconnect), RAM-only logs, LUKS `/etc/magicbridge`, OLED, fan, WiFi mgmt,
Tailscale, DuckDNS, AI macros. Solved: mDNS/hostname self-heal, Tailscale Funnel
(3 stacked bugs), nginx RAM-log perms, MAC-randomise persistence. See
`MAGICBRIDGE_HANDBOOK.md` and git history for detail.
