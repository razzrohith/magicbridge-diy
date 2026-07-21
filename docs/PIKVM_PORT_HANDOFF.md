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
5d. **Two install-script bugs the full-reinstall path hit (both fixed)** `[VERIFY]`
   — check PiKVM's equivalents: (1) the RAM-log tmpfs must be `mode=0755`, NOT
   `1777` — a world-writable sticky dir holding www-data-owned nginx logs trips
   `fs.protected_regular` (Bookworm default 2) so even root can't open them and
   `nginx -t` fails, aborting a re-install (first install works only because the
   logs don't exist yet). See item 1. (2) A `tr -dc … </dev/urandom | head -c N`
   generator SIGPIPEs `tr` (rc 141); under `set -euo pipefail` that aborts the
   whole script — guard any such pipeline with `|| true`.

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
8b. **Auto-detect the capture hardware: CSI board vs USB dongle** `[PORT-concept]`
   — DIY now detects the capture device at runtime and picks the pipeline: the
   C790/TC358743 CSI board → H.264/WebRTC (DEFAULT/preferred), a USB UVC dongle
   (MS2109/MS2130/Cam Link) → MJPEG; if both are present the CSI board wins. One
   image now works on either hardware with no config. `video.device_type()`
   classifies a V4L2 node (`tc358743`/`unicam`/`fe801000` = csi, bus `usb-*` =
   usb) and `mode="auto"` resolves it. **Verified live on both** a real C790
   (1080p50 H.264, EDID cap enforced) and an MS2109 (1080p MJPEG, real frame
   captured). Two things to carry over: (a) the EDID/timings bring-up script must
   SKIP a USB dongle — never push `--set-edid` onto one (it has its own fixed
   EDID); (b) stealth caveat — the restricted-EDID trick (1080p50 cap + Dell
   monitor identity) is **CSI-only** (it lives in the TC358743), so on the USB
   path the dongle's own EDID is what the target sees. If PiKVM ever ships a
   USB-capture variant, port this detection; otherwise it's informational.

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
    `build-image.sh` also **strips the per-unit identity** so no two flashed
    units collide/cross-link: the spoofed MAC (`00-mb-macspoof.conf` +
    `mac_persist={}`) AND `video.mode=auto` (so a unit flashed onto USB-capture
    hardware doesn't inherit the golden unit's CSI mode). Do the same for PiKVM.
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
24. **Four first-boot bugs the DIY end-to-end flash test caught** `[PORT — VERIFY hard]`
    — every one appeared ONLY on a real flash, never on the build host. Check
    PiKVM's equivalents before you ship a base image:
    - **(i) Fresh flash boots on WiFi but SSH + web are DEAD.** The image ships
      with the SSH host keys + TLS cert STRIPPED (correct — per-unit), so sshd and
      the web server start EARLY and fail before first-boot regenerates them, and
      nothing restarts them → the unit looks up (OLED shows its IP) but nothing
      answers. Fix: after `mb-secret-reset` regenerates the keys/cert, RESTART
      those early services. (kvmd: `kvmd-nginx` + sshd; regen must be
      unconditional too — see IMAGING.md status note.)
    - **(ii) That restart can DEADLOCK first-boot.** Restarting a service ordered
      *after* the first-boot unit (DIY: `magicbridge`; kvmd: `kvmd`/`kvmd-nginx`
      if ordered after your first-boot) from *inside* first-boot blocks forever —
      the restart waits for first-boot to finish, which is waiting on the restart.
      Symptom: hangs before WiFi provisioning → no hotspot, OLED never progresses.
      Fix: restart ONLY services NOT ordered after first-boot (sshd + the web
      server), or use `--no-block`.
    - **(iii) The captive portal can't bind :80 because the web server holds it.**
      The portal needs `AP_IP:80`; nginx/kvmd-nginx listens on `0.0.0.0:80`. The
      portal dies with "Address already in use", provisioning tears the AP down,
      and the user stares at "join hotspot" for a hotspot that's gone. LATENT: it
      only appears once the web server actually starts (bug i's fix un-hid it).
      Fix: stop the web server for the duration of provisioning, restore it after
      (on the failure path too). Verify `mb-portal.sh` vs kvmd-nginx.
    - **(iv) A stuck unit is undiagnosable — write a report to the FAT boot
      partition.** A unit with no WiFi and no working hotspot is unreachable, and
      its ext4/root logs can't be read on Windows/macOS (`wsl --mount` refuses
      removable SD readers). DIY now mirrors a plain-text report (who holds :80,
      is hostapd running, portal exit, log tails) to `/boot/firmware/*.txt`, which
      any OS reads. PiKVM's boot partition is `PIBOOT` (FAT) — do the same.
25. **Base = repo HEAD, not a raw golden snapshot** `[PORT-concept]` — DIY's
    `build-image.sh` deploys the FULL repo HEAD into the image and syncs the baked
    git clone to `origin/main`, so a fresh unit reports "up to date" (not a
    day-one N-commit full reinstall) and the web updater is only ever used for
    FUTURE releases. It also strips `wtmp`/`btmp`/`lastlog` (the golden unit's
    login/reboot history otherwise ships and cross-links units). Adapt to
    `align_pi.py`.

26. **Wrong WiFi password STRANDED the unit (no wifi, no hotspot)** `[PORT — VERIFY hard]`
    — found on a real fresh setup, and the nastiest UX bug of the lot. DIY's
    provisioning did `nmcli connection up "$SSID" || true`, **never checked the
    result**, announced "Connected!" on the OLED anyway, and had already torn the
    AP down — so a mistyped password left the unit with **no WiFi and no hotspot,
    recoverable only by power-cycling**. Fix: (a) VERIFY the connection actually
    reached NM state `connected` (poll ~24s), (b) on failure DELETE the bad
    profile so wrong creds are never kept, (c) **re-raise the setup hotspot**
    (DIY re-execs the provisioning script) so the user just rejoins and retries,
    (d) cap retries via an exported counter (4) then stop with a clear
    power-cycle message, (e) raise the unit's `TimeoutStartSec` so a retry can't
    be killed mid-flow. Check `mb-portal.sh`/kvmd's equivalent: **any** path that
    tears down the AP before confirming the new connection has this bug.
27. **Stale unit files in the image silently undo script fixes** `[PORT]` — the
    26 fix landed in the script, but the built image still carried the OLD
    `.service` (with the short timeout) because the image builder only deployed
    the *first-boot* unit files. Half the fix shipped. **Deploy EVERY unit file
    from the repo when arming**, and add `--verify` assertions for the specific
    values that matter (DIY now asserts the retry logic is present, the timeout
    is raised, and the mDNS alias is set) so this fails the build instead of
    shipping. Caught only by verifying the built artifact, not the commit.
28. **Headless (no-OLED) units need a name — mDNS default reversed** `[PORT-concept]`
    — with no screen there is no way to discover the unit's IP, so DIY reversed
    item 5b and now ships `mdns_alias="magicbridge"` **on by default**
    (`magicbridge.local`). Trade-off documented rather than hidden: it's a
    LAN-visible name and multiple units sharing it COLLIDE (avahi renames the
    losers), so a fleet wants a unique/innocuous name per unit or `""` for full
    stealth — the target (USB/HDMI) never sees it either way. Also worth knowing:
    when `.local` "doesn't work" it is almost always a **client-side VPN**
    (NordVPN etc.) hijacking DNS / blocking LAN mDNS, not the unit.
29. **USB capture that vanishes is a POWER problem, not software** `[VERIFY]` —
    a DIY unit powered over USB-C from a laptop port showed "NO CAPTURE DEVICE":
    `lsusb` listed no capture device and `/dev/video1` was gone, after having
    worked minutes earlier. Enumerate→work→disappear = insufficient USB power.
    Before debugging capture code, check `vcgencmd get_throttled` and put the Pi
    on a real 5V/3A supply. (The same unit also dropped off the network entirely.)

---

## Session commits (DIY repo `magicbridge-diy`, for reference)
```
fd5044b fix(image): deploy ALL unit files (stale .service undid the WiFi fix)    (item 27)
f123533 fix(wifi): wrong password stranded the unit - verify + re-raise hotspot  (item 26)
30cf625 feat(mdns): magicbridge.local ON by default (headless reachability)      (item 28)
3d7936c feat(wol): scheduled Wake-on-LAN (cron-backed) + UI
1aac451 feat(audio): USB-audio adapter as the working WebRTC audio path
1c01e3d feat(ui): clip recording, health banner, USB-EDID honesty, reconnect
3195250 feat(image): base = repo HEAD (full deploy + repo sync) + wtmp strip     (item 25)
b0e7d98 feat(provision): Windows-readable setup report on the FAT boot partition (item 24-iv)
7f279fe fix(wifi): captive portal never bound :80 - nginx held it, AP torn down  (item 24-iii)
507de5c fix(image): service-restart fix deadlocked first-boot - restart ssh+nginx (item 24-ii)
dc0e5a1 fix(image): fresh flash left SSH+web DOWN - restart services after reset  (item 24-i)
0e26b57 feat(oled): animated first-boot journey (setup->personalize->wifi->ready) (item 18/19)
036b3b7 feat(image): zero+shrink+xz pipeline, --verify, boot/first-boot hardening (item 20)
1865fcf feat(image): ship video.mode=auto so flashed units detect capture hw   (item 20)
d9fe895 feat(video): auto-detect C790/CSI vs USB capture, default to C790       (item 8b)
94889c1 feat(image): strip spoofed-MAC identity when arming an image            (item 20)
d52ba3f fix(install): hostname gen aborted installer under set -euo pipefail    (item 5d)
f21e6b8 fix(install): RAM-log tmpfs mode=0755 not 1777 (unbreaks re-install)    (item 1/5d)
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
