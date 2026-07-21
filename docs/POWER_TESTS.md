# Power-path A/B tests

Which way of feeding the Pi 4B is actually best, measured instead of guessed.
Raw run logs live in `docs/powertests/`. Tool: `src/core/mb-power-test.sh`.

## Why the old readings were misleading

`vcgencmd get_throttled` has two halves:

| bits | meaning |
|---|---|
| 0–3 | happening **right now** |
| 16–19 | **has happened since boot** — sticky, never clears until power-cycle |

`0x50000` = bit 16 + bit 18 = *"under-voltage occurred"* + *"throttling occurred"*,
**at some point since boot**. The live bits were never set. So the repeated
"still under-voltage even on a 5V/3A supply" was a **latched flag**, quite
possibly from the inrush spike at plug-in — not a live condition.

Every run below therefore follows the same protocol: **boot on the wiring under
test → load it → read**. Same capture hardware every run, or the numbers don't
compare.

## The four wirings

| # | Wiring |
|---|---|
| 1 | USB-C↔C, Pi → laptop only (laptop is the sole supply) |
| 2 | 5V PSU on GPIO pins + VBUS-cut ("power blocker") cable to laptop |
| 3 | 5V PSU on GPIO **+** live USB-C↔C to laptop (two sources) |
| 4 | USB-C splitter: data leg → laptop, power leg → wall charger |

**Option 3 is excluded by inspection, not tested.** The Pi 4B has no reverse-
current protection between USB-C VBUS and the 5V rail (VBUS → polyfuse → 5V
rail), and the GPIO 5V pins land on that same rail bypassing even the fuse. So
option 3 hard-parallels two supplies: the higher one (typically the 5.1V PSU)
back-feeds current into the **laptop's** USB port, and no real load-sharing
happens — whichever source is a few tens of mV higher supplies everything. It
risks the expensive half of the setup for a benefit that doesn't exist.

## Results

### Option 4 — USB-C splitter (data → laptop, power → wall charger)

Run: 2026-07-21, `docs/powertests/option4-splitter-C790.log`, Pi 4B Rev 1.4,
**C790/CSI capture**, 120s, magicbridge.service live + 4-core burn.

| metric | result |
|---|---|
| `throttled` at start | `0x0` — clean |
| `throttled` at end | `0x0` — **no new bits** |
| live under-voltage | **0 / 59 samples** |
| min core voltage | 0.9260V (no sag) |
| max temp | 52.1 °C |
| USB gadget | `configured` before **and** after |
| capture device | `/dev/video0` present throughout |
| **verdict** | **PASS** |

Rechecked at 2102s uptime: still `0x0`. Not a lucky sample.

Functional state confirmed on the same boot:

- HDMI locked at 1920×1080 progressive on `/dev/video0` (`unicam`, `fe801000.csi`)
- capture EDID `00 ff ff ff ff ff ff 00 10 ac 6b a0…` → mfr `10 ac` = **DEL**,
  product `a06b` = **Dell P2419H** — the spoof is live
- USB gadget `046d:c52b` (Logitech Unifying Receiver) with
  `hid.keyboard` + `hid.mouse` + `hid.aux`
- `magicbridge`, `nginx`, `avahi-daemon` all active; API answers (`{"ok":false,
  "error":"auth"}` = up, auth enforced)

### Option 2 — 5V PSU on GPIO + VBUS-cut cable

_not yet run_

### Option 1 — USB-C↔C to laptop only

_not yet run_

## Notes that will matter when comparing

- **VBUS-cut does not break HID enumeration on the Pi 4B.** Because USB-C VBUS is
  tied to the 5V rail, powering from GPIO or a charger means the OTG controller
  sees its *own* 5V as VBUS and asserts session-valid. Options 2 and 4 both keep
  the gadget enumerated — option 4 is confirmed above.
- **External power keeps the Pi alive when the target is off.** Scheduled
  Wake-on-LAN depends on this. On option 1 the Pi dies with the laptop and can
  never wake it, so option 1 is functionally disqualified for WoL regardless of
  how its voltage measures.
- **Option 2 bypasses the Pi's input protection.** GPIO 5V skips the polyfuse
  and all input protection, so a miswire or a bad PSU kills the board instantly.
  Fine on the bench, riskier as a shipped default — which is why option 4 is the
  candidate for the shipped recommendation.
- **A generic "5V 3A" brick is not the same as the official PSU.** The Pi 4 trips
  under-voltage below ~4.63V and the official supply is deliberately **5.1V** for
  that headroom. If option 2 or 4 ever fails, measure the supply before blaming
  the topology.
