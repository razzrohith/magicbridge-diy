# MagicBridge DIY — Progress & Debug Journal (C790 capture path)

Detailed, DIY-specific technical record. For the big picture see
`MAGICBRIDGE_SYSTEM.md`. This file is the deep hardware/driver journal so nobody
re-derives it. Newest first.

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

## Earlier DIY history (V1, condensed)
Hand-built KVM on bare Pi OS: USB gadget HID (`hid.py`), MS2109 MJPEG capture,
Python/aiohttp web server. Stealth suite (USB/MAC spoof, typing jitter, HID
auto-disconnect), RAM-only logs, LUKS `/etc/magicbridge`, OLED, fan, WiFi mgmt,
Tailscale, DuckDNS, AI macros. Solved: mDNS/hostname self-heal, Tailscale Funnel
(3 stacked bugs), nginx RAM-log perms, MAC-randomise persistence. See
`MAGICBRIDGE_HANDBOOK.md` and git history for detail.
