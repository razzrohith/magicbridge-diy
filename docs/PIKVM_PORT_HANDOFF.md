# MagicBridge DIY → PiKVM port handoff

Everything the DIY session (bare Pi 4B + C790, Python/aiohttp stack) built or
fixed that the **PiKVM V4 Mini** project (`magicbridge-pikvm`, kvmd fork + our
services, CM4, root@172.16.20.209) should evaluate and adapt.

**Golden rule (`MAGICBRIDGE_SYSTEM.md §8`): port the IDEA and the stealth-safe
design, never blind-copy the code.** DIY = bare Python/aiohttp + SFTP deploy;
PiKVM = kvmd + our add-on services + git-tree deploy (`align_pi.py`). Re-verify
each item against kvmd; some are already handled natively there.

Tags: **[PORT]** adapt to kvmd · **[SKIP]** kvmd already has it · **[VERIFY]**
check PiKVM's own equivalent · **[PORT-concept]** take the idea, not the code.

---

## 🔒 Anonymity / security (do these first)
1. **Session logs RAM-only** `[VERIFY]` — DIY's backend was writing connection
   IPs + User-Agents + timestamps to the SD card; moved to a tmpfs log dir.
   Confirm every MagicBridge/kvmd/nginx access/session log on PiKVM goes to RAM
   (tmpfs), never the rootfs. **Gotcha (found the hard way):** do NOT mount that
   tmpfs `mode=1777`. A world-writable sticky dir holding nginx logs owned by
   `www-data` trips the kernel's `fs.protected_regular` (Bookworm default = 2),
   which blocks even root from opening the not-owned log files — `nginx -t` then
   fails and any *re-install* aborts (first install works because the logs don't
   exist yet). Mount it `mode=0755 root:root` (all log writers run as root; nginx
   creates its logs as root then hands them to www-data).
2. **nginx HTTP→HTTPS redirect logged visitor IPs to disk** `[VERIFY]` — the
   port-80 redirect vhost had no `access_log off`, so every first visit wrote an
   IP to the SD card. Check PiKVM's redirect vhost has `access_log off` (or RAM).
3. **Realistic monitor EDID by default** `[PORT]` — the base EDID advertised the
   monitor name **"MagicBridge"** (a dead giveaway); changed to **DELL P2419H /
   DEL** (identity only). Ensure PiKVM's EDID monitor name/manufacturer is a real
   monitor, not "MagicBridge"/a tell. (kvmd EDID differs; V4 Mini can do 1080p60
   so timings differ — but the **identity** must be realistic.)
4. **Realistic MAC on by default (was the real Pi/CM4 OUI `dc:a6:32…`)** `[PORT]`
   — the default MAC is a network tell every router client-list / scanner labels
   "Raspberry Pi". DIY now **auto-spoofs a real vendor MAC on first boot**
   (`_ensure_default_mac`, picks a verified Dell/HP/Samsung OUI + random suffix)
   and **persists it at the NetworkManager layer** via
   `/etc/NetworkManager/conf.d/00-mb-macspoof.conf` (`wifi/ethernet.cloned-mac-
   address`). Key lesson: the old `ip link set … address` approach **silently
   reverts** because NM reasserts the permanent MAC on reconnect — you must set
   `cloned-mac-address` on the connection/global default, not just the link.
   `mb-secret-reset` deletes the conf so each unit regenerates. Opt out with
   config `mac_autospoof:false`. Caveat: changing the WiFi MAC can move the DHCP
   lease/IP — reach the unit via mDNS. Port to kvmd (its own NM/dhcp setup).
5. **Realistic identity defaults, verified** `[VERIFY]` — USB = Logitech USB
   Receiver, Monitor = Dell. Verify ALL spoofing (USB, MAC, EDID) defaults to
   realistic values out of the box on PiKVM — on normal startup AND on a fresh
   SD-card first boot (no manual step). In DIY: USB falls back to Logitech in
   `mb-gadget.sh` even with no config; EDID auto-applies via `mb-hdmi-init`; MAC
   auto-spoofs on first boot; all survive `mb-secret-reset` on a clone.
5b. **Hostname + mDNS were name tells** `[PORT]` — the system hostname was
   literally **`magicbridge`** (broadcast via the DHCP hostname option + mDNS →
   shows as "magicbridge" in any router client list), and an alias service
   published **`magicbridge.local` + `raj.local`**. Both broken. DIY now sets a
   realistic per-unit **`DESKTOP-XXXXXXX`** hostname (idempotent across updates;
   regenerated per unit by `mb-secret-reset`) and makes branded aliases **opt-in**
   (`mdns_alias` in config, off by default) — avahi's automatic
   `<hostname>.local` + the IP still reach the unit. Check PiKVM's hostname
   (`pikvm`/`raspberrypi` would be tells) and any `.local` alias.
5c. **Provisioning must not RE-brand the hostname** `[PORT]` — a subtle trap
   found in the checkup: DIY's WiFi-provisioning script treated a realistic
   `DESKTOP-*` hostname as an "imaging-tool default" and reset it back to
   `magicbridge`, silently undoing the spoof mid-provision. ANY code path that
   "normalizes" the hostname must KEEP realistic names and only replace an
   actual tell. Audit every place PiKVM sets the hostname (install, provision,
   first-boot) so none of them fight each other.

## 📶 WiFi / provisioning
6. **Captive-portal dnsmasq `:53` conflict** `[VERIFY]` — DIY's setup-AP dnsmasq
   couldn't bind `:53` (a system dnsmasq held it) → dead hotspot. Fixed with
   stop-system-dnsmasq + `bind-dynamic` + `except-interface=lo` + `rfkill unblock`.
   Same class as PiKVM's earlier portal saga (bug #3) — confirm `mb-portal.sh`
   already handles it.
7. **Saved-WiFi PSK reveal truncated PSKs with a colon** `[VERIFY]` — DIY used
   `nmcli -t | split(':')[-1]`; fixed with `nmcli -e no -g`. PiKVM uses
   wpa_supplicant — check its PSK-reveal parses the conf correctly.

## 🎥 Video / WebRTC
8. **Built the Janus ustreamer plugin + wired WebRTC/H.264** `[SKIP]` — a huge
   DIY effort (janus-gateway.pc, `abs_capture_ts` patch, config dir, `video.sink`
   key). **kvmd already has native Janus/WebRTC.** This was DIY catching up to
   PiKVM. Skip entirely.

## 🖱 HID / input
9. **Absolute + relative mouse** `[PORT-UI-only]` — DIY had to build a whole
   absolute HID gadget descriptor. **kvmd already supports absolute/relative**
   (`mouse_output`). Just add the UI toggle using kvmd's capability; the
   descriptor work is N/A.
10. **Esc = hold-to-exit** `[PORT]` — single Esc tap forwards to the target;
    hold ~2.5s releases control. Frontend (Keyboard Lock API + timer).
11. **Predictive cursor overlay (relative mode)** `[PORT]` — a local dot shows
    movement instantly while the remote cursor catches up. Less needed if PiKVM
    defaults to absolute.
12. **Scroll silently dropped** `[VERIFY]` — frontend sent WS `scroll`, backend
    only handled `wheel`. Check PiKVM's wheel/scroll path.

## 🖥 UI / UX (web page)
13. **Connected-viewers + live device details** `[PORT]` — top-bar chip (who's
    connected count) + a System-tab list with IP · browser+OS · duration; backend
    exposes viewers in `/api/status`. kvmd may already expose sessions.
14. **"How the target sees it" identity card** `[PORT]` — shows the monitor
    (EDID) identity next to the USB identity, framed as "what the target sees"
    (not "spoofed").
15. **Live status polling** `[PORT]` — 5s poll while the page is visible (counts
    weren't auto-refreshing).
16. **Settings reorg** `[PORT]` — pulled **Software Update into its own
    category** (was under Power); sub-nav Monitor · Devices · Security · Power ·
    Update, with a status dot that goes amber when an update waits.
17. **Copy cleanup** `[PORT]` — removed ALL em dashes (an "AI text" tell),
    shortened verbose helper texts, fixed a duplicate "Check for updates" button.
    Apply the same voice to PiKVM's UI.

## 📟 OLED (if the V4 Mini screen applies)
18. **OLED status-override + first-boot/WiFi guidance** `[PORT-if-OLED]` — a
    `/run/…/oled-status` file the setup steps write to ("First setup, please
    wait", "Join hotspot MagicBridge-Setup").
19. **Animated "Updating" indicator** `[PORT-if-OLED]` — title + spinner + a
    Knight-Rider scanning bar during updates.

## 📦 Installer / imaging / updates
20. **Flashable image + first-boot personalization** `[PORT — high value]` —
    `mb-firstboot` (install/personalize on first boot with OLED guidance) +
    `mb-secret-reset` (regenerate per-unit secrets: SSH host keys, TLS,
    machine-id, auth→defaults, USB serial, clear baked WiFi/Tailscale) +
    `build-image.sh` + `docs/IMAGE_BUILD.md` runbook. Build a distributable
    MagicBridge-PiKVM image the same way (base = PiKVM OS). **Adapt the
    secret-reset for kvmd's secrets/certs** so units never ship shared creds.
21. **Idempotent installer + `--check` doctor** `[PORT-concept]` — installer is
    safe to re-run and has a read-only status report. Fold into `magic-install.sh`;
    add `--check`. (Mirrors PiKVM's open "installer gap" about file-level rebrands
    living outside the git tree.)
22. **Incremental vs full updates, auto-detected** `[PORT-concept]` — the updater
    diffs `HEAD..origin`: small change → copy only changed files + restart the
    affected service; structural change → full reinstall. Adapt to PiKVM's
    `align_pi.py` (git-reset): trivial diffs = fast path, structural = full.
23. **OLED "Updating…" during self-update; canonical repo URL pinned; git
    `safe.directory` for the root-run updater** `[PORT-concept / VERIFY]`.

---

## Session commits (DIY repo `magicbridge-diy`, for reference)
```
5b10cb9 fix(anonymity): provisioning must not re-brand hostname to "magicbridge"  (item 5c)
b74c10c feat(anonymity): realistic hostname + drop branded mDNS name tells        (item 5b)
9f08c94 feat(anonymity): realistic MAC on by default, persisted at the NM layer   (item 4)
395483e docs: this handoff file
ccef35a ui+stealth: dup update buttons, animated OLED update, realistic monitor EDID, display identity
afa3005 ui(system): move Software Update into its own category; tidy sub-nav
7d5f5f2 feat(update): incremental (fast) vs full (install.sh) updates, auto-detected
bd8bd52 feat(update): show "Updating..." on the OLED during a self-update
3228efa fix(install): git safe.directory for the updater
def6c5b fix(ui): EDID C790 detection, live connection count + device details, crisper copy
bcbda72 feat(image): flashable-image first-boot flow (OLED-guided) + full auto-update
0000a0e feat(install): make install.sh fresh-install-complete, idempotent, + --check
63b36ae fix(hid): PHYSICAL_MIN/MAX in absolute mouse descriptor (Windows)   [SKIP: kvmd]
91c3dfc feat(ui/hid): visible cursor, Esc-hold-to-exit, connected-viewers, absolute mouse
982b609 fix(wifi): saved-PSK reveal truncated PSKs with a colon
aa351be fix(anonymity): stop nginx port-80 redirect logging visitor IPs to the SD card
3f23baa fix(security): session log off the SD card; pin canonical update repo URL
872ef5f feat(webrtc): build+wire the Janus ustreamer plugin   [SKIP: kvmd native]
b22fa5e fix(wifi): setup-hotspot dnsmasq :53 conflict kills captive portal
```

Suggested order: anonymity (1–5c) → UI/UX (13–17) → imaging (20). Skip
8, 9-descriptor. Re-verify everything against kvmd; don't copy DIY code.

All DIY anonymity changes above were verified with a full offline checkup
(compile + shell syntax + logic unit tests + EDID validation + a residual-tell
sweep, 61 checks green) — the designs are sound to port; only device-runtime
behavior (NM keeping the cloned MAC, DHCP/IP, gadget enumeration) still needs
on-hardware confirmation on each side.
