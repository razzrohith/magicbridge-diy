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

## The four formerly-held items — now resolved (v1.7.1)

Handled so the live unit is never at risk: the two hardware-descriptor items are
OFF by default (byte-identical to today) with an auto-fallback, so deploying the
code changes nothing on the live unit until the owner flips them on at the test
Pi. The other two needed honesty, not a risky change.

| # | Item | How it was resolved | Safe because |
|---|---|---|---|
| 3 | Stealth-panel 2FA | Reuses the MAIN panel's TOTP (shared config); the stealth login requires the same code ONLY when 2FA is already enabled there. Verified against RFC 6238 vectors. | Inert unless 2FA is enrolled, so it can never lock out an un-enrolled user. `magicbridge.py --disable-2fa` (SSH) is the escape hatch. |
| 12 | True USB iSerial=0 | NOT forced. A true zero index isn't cleanly achievable via configfs and forcing it risks breaking enumeration. The empty serial string stays; the "iSerial=0" claim was softened to the truth. **Bench check:** on the target run `lsusb -v -d 046d:c52b \| grep -i iSerial` - if it shows `iSerial 0` we're already correct; a non-zero index is the residual. | No gadget change, so no enumeration risk. |
| 16 | Long-run timing cadence | The only target-visible periodic signal is the mouse jiggler, already handled by the v1.6.0 "Human" style. Video-bitrate cadence is deliberately left alone (risk to live video > speculative gain); the web-poll cadence is owner-side and encrypted, not a target tell. | No change to the live video pipeline. |
| 20 | Full HID++ 3rd interface | Correct full descriptor (short 0x10 + long 0x11) added behind `usb.hidpp_full` (default OFF = today's stub). The UDC bind now auto-falls-back to keyboard+mouse if any descriptor is rejected, so it can never brick HID. | Default off + auto-fallback = the target never loses input. **Bench:** set `usb.hidpp_full=true`, reboot the test Pi, confirm HID works and `lsusb` shows the fuller interface. |

## Documentation-only (no code change, by design)

| # | Item | Note |
|---|---|---|
| 14 | LAN control plane reachable by default | Not forcing Tailscale-only, since that could cut off a customer who relies on LAN access. The brand tells on that surface (cert, mDNS, avahi) are removed by items 1/6/10, so even when LAN-reachable it carries no product name. Owner opts in to lockdown. |
| 15 | 50Hz refresh tell on the CSI capture path | Accepted residual: the Pi 4B 2-lane CSI cannot do 1080p60, and there is no emulator in the stack. Documented, not fixable here. |
