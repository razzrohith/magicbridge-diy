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

## Fast alternative: clone a working unit
1. Set up one unit fully (`install.sh --with-webrtc`, verify with `--check`).
   ⚠ If you enabled LUKS on `/etc/magicbridge`, the LUKS **key** lives on the
   card and would be cloned — re-key per unit or skip LUKS on the golden unit.
2. Image its card on another machine:
   `sudo dd if=/dev/sdX of=magicbridge-base.img bs=4M status=progress`
   (Do NOT `dd` a live running system's own disk — power off and read the card
   from a second machine, or the filesystem is inconsistent.)
3. Arm + shrink: `sudo bash src/provision/build-image.sh magicbridge-base.img magicbridge.img`

## Lightest: net-install on stock Pi OS (no OLED during install)
Flash stock Pi OS Lite, add a first-boot hook that runs `install.sh`. Simplest
to produce, but the OLED can't show progress during the initial install (its
software isn't there yet), and first boot needs internet (~30 min with WebRTC).
Prefer pi-gen/clone if you want the OLED-guided experience.

---

## Flashing + first run
1. Raspberry Pi Imager → **"Use custom"** → select `magicbridge.img` → write.
   (Skip Imager's own OS customization — the image self-configures.)
2. Put the card in the Pi 4B, power on. Watch the OLED: *please wait* →
   *Join hotspot MagicBridge-Setup*.
3. Join `MagicBridge-Setup` (open) from a phone/laptop, complete the captive
   portal WiFi form. The unit reboots/joins your WiFi; OLED shows the IP.
4. Open `https://magicbridge.local/`, change both default passwords.

## Updates (backend → GitHub, code **and** structure)
The built-in updater (System → Update, i.e. `POST /api/update`) pulls
`razzrohith/magicbridge-diy` and then **re-runs the idempotent `install.sh`** in
the background, so new dependencies, `config.txt` overlays, systemd services and
files all apply — not just changed Python. Progress:
`/var/log/magicbridge-update.log`; services restart, reconnect in ~1–2 min.
`sudo bash install.sh --check` verifies state anytime.
