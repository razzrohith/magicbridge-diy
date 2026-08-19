# Building the shippable image — Route B (spare card golden unit)

Produces a distributable `.img.xz` **without touching your working unit**.

Why a golden unit at all: the image is a *clone of a real, fully set-up unit*.
Hand-patching an old image cannot work, because `install.sh` applies system-level
things (systemd units, nginx, boot config, apt packages) that a file copy misses.
Never image a **running** system — the filesystem would be inconsistent.

---

## You do steps 1-6 (physical + on-unit). I do step 7 (the pipeline).

### 1. Flash the old image to a SPARE card
Raspberry Pi Imager -> "Use custom" -> `E:\Startup\flashOS_DIY\magicbridge.img.xz`
-> write. Skip Imager's own OS customization; the image self-configures.

Use a **spare** card, not the one in your working unit. Smaller is better
(16-32 GB) — the clone in step 6 is the size of the whole card.

### 2. Boot it and complete first-boot setup
Put the card in a Pi 4B, power on, watch the OLED:
`please wait` -> `Join hotspot MagicBridge-Setup`.
Join `MagicBridge-Setup` from a phone, complete the captive-portal WiFi form.

> This unit does NOT need to be plugged into any target computer. It only needs
> power, and WiFi so it can pull the update in step 3.

### 3. Update it to current code
Open `https://magicbridge.local/` (or the IP shown on the OLED), log in, then
**Settings -> System -> Update** and let it finish (~1-2 min; services restart).

It should end up on **v1.3.2 / `da45394`** or newer. Verify over SSH:

```bash
git -C /opt/magicbridge-repo log -1 --oneline    # expect da45394 (or newer)
cat /etc/magicbridge/.deployed-commit
```

### 4. Build the patched video engine  ← DO NOT SKIP
The Update button does **not** rebuild ustreamer (`install.sh` only does that with
`--with-webrtc`). The image you flashed carries a **stock** ustreamer, which has
no bandwidth ceiling — that is the whole latency bug — and the verify gate in
step 7 will reject the image if it is still stock.

```bash
sudo bash /opt/magicbridge/install_janus_webrtc.sh
```

Takes ~15-30 min. Then confirm the patch is in the binary:

```bash
grep -c MB_H264_MIN_QP /usr/local/bin/ustreamer     # must be >= 1
```

If that prints 0, stop — the image would ship the latency bug.

### 5. Sanity-check the unit
```bash
sudo bash /opt/magicbridge-repo/install.sh --check
```
Everything should pass. The `LUKS (optional) — MISSING` warning is expected and
CORRECT: LUKS would make the image unshippable (arming cannot see inside it).

### 6. Power off cleanly, pull the card, read it
```bash
sudo shutdown -h now
```
Wait for the green LED to stop blinking, then pull the card and read it on
Windows with **Win32 Disk Imager -> Read** to:

```
E:\Startup\flashOS_DIY\base-new.img
```

The card is never modified, so it stays a working backup.

### 7. Tell me it's done — I run the pipeline
I then run, on WSL Ubuntu:

```bash
sudo bash build-image.sh          base-new.img  magicbridge-new.img   # arm
sudo bash build-image.sh --shrink magicbridge-new.img                 # zero free space + shrink
sudo bash build-image.sh --verify magicbridge-new.img                 # prove it
sudo bash build-image.sh --compress magicbridge-new.img               # -> .img.xz
```

`--verify` must print **"ALL CHECKS PASSED — safe to distribute"**. If any check
fails the image is not shipped — that is the gate doing its job.

**SHRINK BEFORE VERIFY, not after.** This used to be the other way round, which
was wrong in a way that mattered: `--verify` only tests that secret FILES are
absent, and deleting a file does not erase its blocks. Free space is zeroed by
`--shrink`, so verifying first blessed an image whose deleted SSH keys, WiFi PSK
and config backups were still recoverable straight out of the raw `.img`.
`--verify` now refuses an image that has not been through `--shrink`.

---

## Then: one fresh-flash smoke test before the first paid batch
Flash the produced `.img.xz` to a blank card, boot a unit that has never been
provisioned, and confirm picture + keyboard/mouse with **zero manual steps**.
That is the real ship bar, and it is the one thing no amount of code review
replaces.

---

## What must NOT be shipped
- The current `E:\Startup\flashOS_DIY\magicbridge.img.xz` (dated Aug 2). It
  predates every fix in v1.1-v1.3.2, still has the 27" EDID, the stale runtime
  code, and a stock ustreamer. The verify gate now rejects it.
