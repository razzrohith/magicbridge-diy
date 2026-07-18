# MagicBridge DIY — Roadmap, Tasks & Ideas

Status legend: ✅ done · 🔵 next · ⏳ blocked/pending hardware · 💡 idea · 🅿️ parked

## Now / next
- ✅ **Janus/WebRTC + H.264 stream** — DONE & server-verified (2026-07-18). Built
  the missing `libjanus_ustreamer.so`, wired memsink→Janus→WebRTC; a headless WS
  probe got a JSEP H.264 offer and the plugin opened the memsink. See
  `DIY_PROGRESS.md`. **Remaining:** a real browser decode test (sandbox can't
  reach the LAN) + confirm after a natural reboot.
- ⏳ **Retest 1080p50 partial frames on wall power** — splitter/power-blocker
  arriving; under-voltage is the leading suspect for the green band.
- 🔵 **Wire the C790 device into the live UI** — confirm `video.py` auto-selects
  `/dev/video0`, resolution 1080p50, and the stream shows in the web console.

## Done (2026-07-18)
- ✅ WiFi setup-hotspot dnsmasq `:53` conflict fixed in `mb-provision.sh`
  (stop system `dnsmasq.service` during provisioning + `bind-dynamic`/
  `except-interface=lo` + `rfkill unblock`). Ported from the PiKVM portal saga;
  proven on a dummy iface. **Full AP→portal→connect flow still needs an on-site
  test** (triggering the AP drops wlan0). See `DIY_PROGRESS.md`.
- ✅ C790 driver enabled; `/dev/video0` capture at 1080p50 UYVY, 50 fps.
- ✅ EDID persistence across reboot (`mb-hdmi-init`).
- ✅ Portable restricted EDID (caps any source at 1080p50, no manual step).
- ✅ Hot-plug watchdog (`mb-hdmi-watch`).
- ✅ config.json fps 30→50.
- ✅ systemd ordering-cycle bug found + fixed.

## Blocked / pending
- ⏳ **I2S audio** 🅿️ — upstream driver bug (chip receives audio, driver never
  programs its I2S output regs). Revisit only if upstream fixes it. Do NOT poke
  raw I2C registers (risks video).
- ⏳ **nginx RAM-log EACCES** (from V1) — `nginx -t` fails on the RAM log path;
  live nginx fine but a restart/reboot is at risk. Needs a go-ahead to fix.

## Ideas / future
- 💡 **Repo hygiene** — `git mv` the hundreds of root `mb_*.py` / `_commit_msg*`
  / `live_*` scratch files into `_archive/`. RISKY-ish (history/paths) → propose
  first. Real code is `src/`.
- 💡 **HDMI downscaler support** — for targets that can't drop below 1080p (4K
  laptops): a cheap HDMI 4K→1080p scaler in-line feeds a clean 1080p50.
- 💡 **Multi-unit provisioning** — one master setup script to flash/configure
  additional physical DIY units (bamboo-case builds).
- 💡 **EDID profiles** — swappable monitor identities (already partly in
  `mb_edidconf.py`); expose realistic named profiles in the stealth panel.
- 💡 **Feature parity with PiKVM** — periodically reconcile the DIY↔PiKVM feature
  matrix; port stealth-safe features both ways (see `MAGICBRIDGE_SYSTEM.md` §8).

## Guardrails on all of the above
Every feature must pass the anonymity check (`MAGICBRIDGE_SYSTEM.md` §2): no new
USB descriptor tell, no network string that says KVM/Pi, no on-disk sensitive
log, no on-screen "KVM/Pi/PiKVM" text. Live-Pi changes: SAFE→do, RISKY→ask.
