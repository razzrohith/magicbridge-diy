# Stealth upgrades: status

Batch from the 22-item stealth audit. Emulator item (USB-stick monitor spoofing)
was dropped per the owner's decision to never pair an EDID emulator with the USB
capture card.

## Shipped this batch (verified locally, safe on a live unit)

| # | Upgrade | File(s) | Verified by |
|---|---|---|---|
| 1 | TLS cert SAN de-branded (drop magicbridge.local); live units re-issue on the next full upgrade | install.sh, mb-secret-reset.sh | openssl cert gen + SAN inspection: brand-free |
| 2 | USB bcdDevice 0x0100 -> 0x1203 (a real Logitech c52b release), config-overridable | mb-gadget.sh | applies on next reboot only, no live rebind |
| 5 | USB config descriptor advertises Remote Wakeup (bmAttributes 0xa0) | mb-gadget.sh | applies on next reboot only |
| 6 | mDNS alias defaults to empty on all install/clone paths (existing units keep their value) | install.sh, mb-secret-reset.sh | backfill only adds when missing |
| 7 | "Stealth" removed from pre-auth admin pages | stealth-dashboard.py | compile + string check |
| 8 | Update log moved to the RAM tmpfs; stale on-disk log purged | magicbridge.py | path change, 600 perms |
| 9 | EDID monitor serial PRESERVED across full upgrades (no phantom-monitor tell) | install.sh | simulated install+upgrade: serial stable, checksums valid |
| 10 | avahi mDNS surface pinned to address-records only (no _workstation/HINFO); live-safe with restore fallback | install.sh | configparser round-trip, avahi-native key=value |
| 11 | Stealth panel: true hard lockout (429) instead of sleep-only | stealth-dashboard.py | lockout logic test: locks at 8, clears on success |
| 13 | Incremental update: compile-gate + full rollback so a bad .py never crash-loops the live unit | magicbridge.py | rollback test: bad deploy reverted, live files intact |
| 17 | IPv6 address generation pinned to stable-privacy (no EUI-64 MAC leak) | install.sh | NM conf.d drop-in, reload only |
| 18 | wlan0 and eth0 get different vendor OUIs | mb-secret-reset.sh | 2000-mint test: 0 same-OUI collisions |
| 19 | Update fetch/pull stamp race closed (pin the exact target sha) | magicbridge.py | diff + ff-only + stamp all use one sha |
| 21 | Rescue tool no longer defaults to raj/lol; requires the per-unit password | mb-rescue.ps1 | guard errors clearly without it |
| 22 | HID auto-disconnect jittered + framed honestly as a trade-off | magicbridge.py | compile |

Target safety: none of these re-enumerate the USB device or re-apply the EDID on
a running unit. USB descriptor changes (2, 5) and any EDID change take effect only
on the next natural reboot. The full upgrade's install.sh is self-update guarded
(gadget not rebound, EDID not re-applied live). Cert/avahi/mDNS/IPv6 changes are
LAN-side only and never touch the target.

## Held for bench validation on the TEST Pi (NOT shipped to the live unit)

These are correct to want, but cannot be proven 100% safe without a reboot on the
test Pi with real USB/video hardware. Shipping them unverified could break USB
enumeration on the target or destabilise live video, which the owner forbade.

| # | Upgrade | Why held |
|---|---|---|
| 3 | Full 2FA on the stealth panel | The identity panel. A bug in an untested enrollment/verify flow could lock the owner out. The panel is already hardened (hard lockout + forced first-run password + de-branded). Do with a bench test of the login flow. |
| 12 | Force USB iSerialNumber to a true 0 | Needs an lsusb -v read on the target to confirm the current index, and possibly an f_hid change that could break enumeration. Verify on the bench; until then do not claim iSerial=0 in the model. |
| 16 | Smooth long-run video/timing cadence | Touches the live video pipeline; a bad change degrades latency/stability. Needs bench measurement. |
| 20 | Flesh out the 3rd (HID++) USB interface descriptor | A malformed report descriptor would break enumeration on reboot = target loses input. Bench-only. |

## Documentation-only (no code change, by design)

| # | Item | Note |
|---|---|---|
| 14 | LAN control plane reachable by default | Not forcing Tailscale-only, since that could cut off a customer who relies on LAN access. The brand tells on that surface (cert, mDNS, avahi) are removed by items 1/6/10, so even when LAN-reachable it carries no product name. Owner opts in to lockdown. |
| 15 | 50Hz refresh tell on the CSI capture path | Accepted residual: the Pi 4B 2-lane CSI cannot do 1080p60, and there is no emulator in the stack. Documented, not fixable here. |
