# MagicBridge DIY — building a flashable image

Goal: a single `.img` you flash with **Raspberry Pi Imager → "Use custom"**. On
first boot the unit personalizes itself (unique secrets), shows progress on the
**OLED**, then guides you to the **MagicBridge-Setup** WiFi hotspot. After WiFi
setup it's a normal MagicBridge unit, and the built-in updater keeps it current
from GitHub.

> You need a **Linux build host** (Ubuntu, WSL2, a Pi, or Docker) for the image
> steps — loop-mounting an image can't be done on Windows directly. The runtime
> pieces (first-boot service, OLED guidance, secret reset, full auto-update) are
> already in the repo and installed by `install.sh`.

---

## The first-boot experience (already built)
`mb-firstboot.service` runs once on the first boot of a flashed card, then
disables itself:
1. OLED → **"First setup / Please wait…"**
2. Personalizes the unit:
   - pre-baked image → `mb-secret-reset.sh` regenerates SSH host keys, TLS cert,
     machine-id, resets auth to the default passwords, clears baked
     WiFi/Tailscale/DuckDNS/AI keys, and re-derives the USB serial from this
     Pi's MAC.
   - net-install image → clones the repo and runs `install.sh`.
3. Hands off to WiFi provisioning. OLED → **"Join hotspot MagicBridge-Setup"**;
   after you submit WiFi in the captive portal it clears to the normal display.

Why personalize: never ship one image with shared SSH keys / TLS key / passwords
/ Tailscale identity — that would let units impersonate each other and be
cross-linked, breaking the anonymity model.

---

## Recommended: pi-gen (clean, reproducible, no secrets ever baked)
1. On a Linux/Docker host, clone `pi-gen` (Bookworm branch).
2. Add a `stage-magicbridge` that, in its `run.sh`/chroot, clones this repo and
   runs `install.sh --with-webrtc` (bakes video + WebRTC in). `install.sh` marks
   the unit "already set up" (`.firstboot-done`), so the output image is inert.
3. Build → you get `image.img` with everything installed but **no unit secrets**
   (TLS/auth are generated per-unit; pi-gen didn't run the Pi so host keys aren't
   final).
4. Arm it: `sudo bash src/provision/build-image.sh image.img magicbridge.img`
   (enables first-boot personalization, strips any stray secrets, shrinks).

## Fast alternative: clone a working unit  ← what we actually ship
1. Set up one unit fully (`install.sh --with-webrtc`, verify with `--check`).
2. Power it off cleanly, pull the card, and read it on another machine
   (Windows: **Win32 Disk Imager → Read**; Linux: `dd if=/dev/sdX of=base.img bs=4M`).
   Never image a *running* system — the filesystem would be inconsistent. The
   card itself is never modified, so it stays your backup.
3. Run the four-stage pipeline below.

### ⚠ LUKS first
If the golden unit encrypted `/etc/magicbridge` (LUKS), a clone ships a **shared
key + container**, and the config it holds is invisible to the arming step, which
mounts the empty mountpoint and silently strips *nothing*. `build-image.sh` now
**hard-fails** on a LUKS partition rather than arming such an image. De-LUKS it
first (decrypt → copy contents to a plain `/etc/magicbridge` → delete the
container, the boot-partition keyfile, `.orig_backup`, and the crypttab/fstab
lines → remove the `.firstboot-done` flag that lived *inside* the container).

## The build pipeline (`src/provision/build-image.sh`)
```bash
sudo bash build-image.sh          base.img  dist.img   # 1. arm
sudo bash build-image.sh --verify dist.img             # 2. prove it took
sudo bash build-image.sh --shrink dist.img             # 3. zero + shrink
sudo bash build-image.sh --compress dist.img           # 4. -> dist.img.xz
```
1. **arm** — strips every per-unit secret (SSH host keys, WiFi, Tailscale,
   machine-id, TLS, spoofed MAC, `mac_persist`, USB serial, auth), sets
   `video.mode=auto`, re-arms **both** first-boot stages, makes `/boot/firmware`
   `nofail`, and **self-heals the first-boot logic from this repo** so a golden
   unit predating these fixes cannot ship stale behaviour. Partitions are found
   **by content, never by index**.
2. **--verify** — 19 assertions; **exit 1** if any fail. Do not distribute a
   failing image. (It genuinely discriminates: it fails an un-armed image.)
3. **--shrink** — **zeroes free space on every partition first.** Deleting a file
   does not erase its blocks: an armed-but-unzeroed image still holds recoverable
   remnants (old WiFi, SSH keys, and for DIY the deleted LUKS container and
   plaintext config backup). Zeroing erases them *and* makes the image compress.
   Then `resize2fs -M` + shrink the partition + truncate.
4. **--compress** — `xz -T0` → `.img.xz`, verified with `xz -t`. **Raspberry Pi
   Imager flashes `.img.xz` natively** — ship that file.

> **pishrink is correct for DIY** (unlike the PiKVM sibling, which rejects it):
> DIY is Pi OS and its **last partition is the rootfs**, which is exactly what
> pishrink targets, and its auto-expand hook is Pi-OS-native.
> `mb-firstboot-late.sh` is the safety net if that hook ever fails to run.

## Lightest: net-install on stock Pi OS (no OLED during install)
Flash stock Pi OS Lite, add a first-boot hook that runs `install.sh`. Simplest
to produce, but the OLED can't show progress during the initial install (its
software isn't there yet), and first boot needs internet (~30 min with WebRTC).
Prefer pi-gen/clone if you want the OLED-guided experience.

---

## Boot & first-boot hardening (why a shrunk image is safe here)

Shrinking makes the **rootfs** smaller on DIY, so the first-boot re-grow is
boot-critical. These are the failure modes the PiKVM sibling hit on real
hardware, and DIY's answer to each. Every one of them only appears on hardware —
none would show up in a build-host test.

| Failure mode | DIY's answer |
|---|---|
| **(a)** A non-essential partition mounted **without `nofail`** blocked the *entire* boot when left inconsistent (pings, but no SSH/services). | `/boot/firmware` is now `nofail,x-systemd.device-timeout=15s` — set by both `install.sh` and image arming, asserted by `--verify`. |
| **(b)** The first-boot **done-marker write failed silently**, so first-boot re-ran every boot and re-wiped the just-entered WiFi → endless "please wait / join hotspot" **loop**. | The marker is written, `sync`ed and **verified**; the unit is disabled either way. Plus `mb-secret-reset` honours `MB_KEEP_WIFI`, which `mb-firstboot` sets when it detects saved WiFi — so a stray re-run can never strand a working unit. |
| **(c)** A slow resize + forced `e2fsck` *inside* early first-boot blew the service timeout → killed before the marker → same loop. | Slow/live-device work moved to **`mb-firstboot-late.sh`** — post-boot, marker-guarded, non-blocking, nothing depends on it. Early `mb-firstboot` also has `TimeoutStartSec=3600`. |
| **(d)** Offline `resize2fs` failed because the fs stayed mounted. | The late step does an **ONLINE** grow; it never tries to unmount root. |
| **(e)** A flashed card can be a different size/device than the golden one. | All partition/disk lookups are dynamic (`findmnt`/`lsblk`/`blkid`); it only ever grows the **last** partition, and no-ops if already full. |
| **(f)** A looping unit lives only on its own hotspot, and joining it kills your laptop's internet. | **`src/provision/mb-rescue.ps1`** — offline, self-contained; see below. |
| **(g)** Under-voltage causes flakiness unrelated to the code. | `mb-firstboot-late.sh` logs `get_throttled`; the rescue report surfaces it. |

Every step in the late stage is best-effort and **always exits 0** — worst case a
unit boots with a smaller disk or the baked EDID serial, never a bricked or
looping unit.

**Bonus anonymity fix:** the late stage also gives each unit a **unique EDID
serial**. Every DIY unit previously shipped an *identical* Dell EDID, so two
units could be cross-linked by their monitor serial. Identity stays a genuine
`DELL P2419H`; only the serial differs.

## Rescue: a unit stuck on its setup hotspot
Joining `MagicBridge-Setup` cuts your laptop's internet, so you can't get help
while connected. Run this **offline**, then rejoin your WiFi and share the file:
```powershell
powershell -ExecutionPolicy Bypass -File src\provision\mb-rescue.ps1        # diagnose
powershell -ExecutionPolicy Bypass -File src\provision\mb-rescue.ps1 -Fix   # + fix
```
It SSHes to the AP (`192.168.73.1`), discovers the unit's freshly-generated host
key itself, and writes a report to your Desktop: rootfs writability, both markers,
**how many times first-boot has run** (>1 = the loop), saved WiFi, whether the
rootfs grew, fstab safety, under-voltage, failed units. `-Fix` forces both
markers, disables the first-boot units, makes `/boot/firmware` nofail, and runs
the online grow.

## Flashing + first run
1. Raspberry Pi Imager → **"Use custom"** → select `magicbridge.img` → write.
   (Skip Imager's own OS customization — the image self-configures.)
2. Put the card in the Pi 4B, power on. Watch the OLED: *please wait* →
   *Join hotspot MagicBridge-Setup*.
3. Join `MagicBridge-Setup` (open) from a phone/laptop, complete the captive
   portal WiFi form. The unit reboots/joins your WiFi; OLED shows the IP.
4. Reach it at **`https://magicbridge.local/`** (published by default via the
   `mdns_alias="magicbridge"` config — works out of the box, ideal for a headless
   / OLED-less unit). Change both default passwords on first login.
   - If `.local` won't resolve, it's almost always a **VPN on your client**
     (NordVPN etc. hijack DNS and block LAN mDNS) — pause it or allow LAN, or use
     a phone on the same WiFi. You can also find the IP in the router's client
     list (a `DESKTOP-XXXXXXX` device with a Dell/HP/Samsung MAC) or in
     `magicbridge-setup-report.txt` on the FAT boot partition.
   - **Anonymity note:** `magicbridge.local` is a LAN-visible name, and multiple
     units sharing it COLLIDE on one network. For a fleet / units given to
     others, set `mdns_alias` to a unique innocuous name per unit, or `""` for
     full stealth. The target (USB/HDMI) never sees this name either way.

## Updates (backend → GitHub, code **and** structure)
The built-in updater (System → Update, i.e. `POST /api/update`) pulls
`razzrohith/magicbridge-diy` and then **re-runs the idempotent `install.sh`** in
the background, so new dependencies, `config.txt` overlays, systemd services and
files all apply — not just changed Python. Progress:
`/var/log/magicbridge-update.log`; services restart, reconnect in ~1–2 min.
`sudo bash install.sh --check` verifies state anytime.
