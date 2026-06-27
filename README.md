# MagicBridge

Self-hosted KVM-over-IP for Raspberry Pi 4. Control any computer remotely via browser — keyboard, mouse, and video. Zero dependency on TinyPilot or any external service.

---

## Install

Flash Pi OS Bookworm (64-bit) on a Pi 4, then SSH in and run:

```bash
curl -fsSL https://raw.githubusercontent.com/razzrohith/MagicBridge/main/install.sh | sudo bash
sudo reboot
```

After reboot, plug the Pi's **USB-C port** into the target computer's USB port.

---

## Access

| | |
|---|---|
| KVM (remote control) | `https://magicbridge.local/` |
| Admin panel | `https://magicbridge.local/stealth/` |
| SSH | `ssh admin@magicbridge.local` · password `lol` |
| Panel password | `lol` (change in Admin panel → System) |

> Browser will warn about the self-signed certificate — click "Advanced → Proceed".

---

## Features

- **Video** — MJPEG stream via ustreamer; supports all UVC capture cards (MS2109, Elgato Cam Link, UGREEN, etc.)
- **Keyboard + mouse** — full HID gadget; Pi appears as a real USB keyboard + mouse to the connected computer
- **USB identity spoofing** — change manufacturer, product name, VID/PID, serial number from the admin panel
- **WiFi provisioning** — on first boot, Pi broadcasts a `MagicBridge-Setup` WiFi AP with a captive portal to enter credentials
- **Stealth admin panel** — bcrypt-protected dashboard at `/stealth/` for USB, MAC, WiFi, Tailscale, DuckDNS, logs, backup
- **Tailscale + Funnel** — encrypted remote access from anywhere
- **DuckDNS** — free public hostname, updated every 5 min
- **MAC spoofing** — change and persist ethernet/WiFi MAC via systemd
- **Firewall** — default-deny iptables; internal ports blocked externally

---

## Supported capture cards

Any UVC-compliant V4L2 device works out of the box:
- Generic MS2109 HDMI capture (common $10–20 USB dongle)
- Elgato Cam Link 4K
- UGREEN HDMI USB capture card
- Any other UVC-compatible card

---

## First-time WiFi setup (no ethernet)

1. Power on Pi — it broadcasts an open WiFi network: **`MagicBridge-Setup`**
2. Connect your phone or laptop to it
3. Browser opens automatically (or go to `http://192.168.73.1/`)
4. Enter your home WiFi credentials → Pi connects and the setup AP disappears
5. Access at `https://magicbridge.local/`

---

## File layout

```
magicbridge/
├── install.sh                      # one-command installer
├── uninstall.sh                    # full removal
└── src/
    ├── core/
    │   ├── hid.py                  # USB HID keyboard + mouse driver
    │   ├── video.py                # V4L2 video capture manager
    │   ├── magicbridge.py          # aiohttp KVM server (port 8080)
    │   ├── magicbridge.service     # systemd unit
    │   ├── mb-gadget.sh            # USB gadget configfs setup
    │   └── mb-gadget.service       # systemd unit
    ├── web/
    │   └── index.html              # KVM web UI (vanilla JS, no CDN)
    ├── dashboard/
    │   ├── stealth-dashboard.py    # Flask admin panel (port 7777)
    │   └── stealth-dashboard.service
    ├── provision/
    │   ├── mb-provision.sh         # first-boot WiFi AP provisioning
    │   ├── mb-provision.service    # systemd unit
    │   └── mb-setup-ui.py          # captive portal web app
    └── nginx/
        └── magicbridge.conf        # nginx reverse proxy config
```

---

## Terms & Conditions

**Personal use only.** MagicBridge is provided as-is for personal, non-commercial, educational, and homelab use.

- You are solely responsible for the security of your deployment
- Do not use this to access computers you do not own or have explicit permission to control
- Do not expose the admin panel (`/stealth/`) publicly without a strong password
- The default password (`lol`) **must be changed** before exposing to any network you don't fully control
- The authors accept no liability for misuse, data loss, hardware damage, or unauthorized access

By installing and running MagicBridge, you agree to these terms.

---

## License

MIT — free to use, modify, and distribute with attribution.
