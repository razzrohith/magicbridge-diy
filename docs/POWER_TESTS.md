# Power-path A/B tests

Which way of feeding the Pi 4B is actually best, measured instead of guessed.
Raw run logs in `docs/powertests/`. Tool: `src/core/mb-power-test.sh`.

## Result: option 4 wins, and it is not close

All three runs: Pi 4B Rev 1.4, **C790/CSI capture**, fresh boot on the wiring
under test, `magicbridge.service` live + 4-core burn.

| | **4** splitter<br>(charger + laptop) | **2** 5V PSU on GPIO<br>+ VBUS-cut cable | **1** laptop USB-C only |
|---|---|---|---|
| live under-voltage | **0 / 59** | **continuous at IDLE** | **51 / 57** |
| min core voltage | **0.9260V** | 0.8600V | 0.8500V |
| `throttled` at end | `0x0` | `0x50005` | `0x50005` |
| max temp | 52.1 °C | 35.5 °C (throttled, doing nothing) | 42.3 °C |
| USB gadget | `configured` | `configured` | `configured` |
| capture device | present | present | present |
| **verdict** | ✅ **PASS** | ❌ FAIL | ❌ FAIL |

Option 4 rechecked at 2102s uptime: still `0x0`. Not a lucky sample.

## Why the original readings were misleading

`vcgencmd get_throttled` has two halves:

| bits | meaning |
|---|---|
| 0–3 | happening **right now** |
| 16–19 | **has happened since boot** — sticky, never clears until power-cycle |

The `0x50000` we kept seeing is bit 16 + bit 18 = *under-voltage occurred* +
*throttling occurred*, **at some point since boot**. Option 1's dmesg shows
exactly what that was:

```
[10.27] hwmon hwmon1: Undervoltage detected!
[18.33] hwmon hwmon1: Voltage normalised
```

An **inrush transient at plug-in that recovered 8s later** — and then latched a
flag that looked like a live fault for the rest of the boot. Every run here
therefore boots on the wiring under test first, and reports the **live** bits
separately from the sticky ones.

## Is option 1 good enough for normal use? No — and the near-miss is instructive

The 4-core burn is harsher than real use, so option 1 was retested with **no
artificial CPU load**, just the real capture → encode → network path:

| option 1, realistic load | live under-voltage | verdict |
|---|---|---|
| stream pulled over **loopback** | **0 / 88** | PASS |
| same, plus real **WiFi transmit** | **66 / 69** | **FAIL** |

The first run says option 1 is fine. It is wrong, and the reason matters: pulling
the stream over `127.0.0.1` never keys the **WiFi radio**, and WiFi TX is a large
current draw on a Pi. Add the transmit the product actually does and the board
browns out within seconds. **Any power test that does not push traffic over the
real radio flatters the supply.**

Also note `min core voltage` reads 0.8500V in *both* realistic runs — the passing
one and the failing one. Core voltage tracks DVFS (the SoC clocks down when idle),
so it is **not** a supply-sag indicator and must not be compared across different
load modes. The live under-voltage bit is the honest signal.

## 5.0V vs 5.1V is the whole ballgame (2026-07-21, measured)

A "5V 3A" supply through the option-4 splitter still browned out **constantly**.
Swapping to a **5.1V 3A** supply — nothing else changed — fixed it outright:

| same load, same splitter, same cable run | 5.0V / 3A | **5.1V / 3A** |
|---|---|---|
| `get_throttled` | `0x50005` (live under-voltage **and** live throttling) | **`0x0`** |
| ARM clock under a 4-core burn | **600 MHz, locked** | **1800 MHz** |
| undervoltage events since boot | continuous | **0** |
| temp under full load | - | 47.7 °C |

The Pi 4 was pinned at its throttle floor — **running at a third of its rated
speed** — and reached full 1800 MHz the moment the supply had headroom.

Why 0.1V matters: the Pi 4 trips below **4.63V at the board**, and the losses
stack — a USB-C power/data splitter costs 100-300 mV under 2A, and a thin or
long cable another 200-300 mV. A 5.0V brick starts with no margin and loses.
The official Raspberry Pi PSU is deliberately 5.1V for exactly this reason.

**Diagnostic note:** 600 MHz alone proves nothing - it is also the Pi's normal
*idle* clock. The test that settles it is to load all four cores and check
whether the clock can RISE. If it stays at 600 MHz under full load with bit 2
set, it is throttled.

**What it did NOT cause:** the green banding (M2M encoder worker contention) and
the high latency (55 Mbit/s MJPEG saturating WiFi) both had proven, separate
causes and were fixed before this supply swap. Under-voltage cost CPU headroom
for H.264 encoding - real, but a different problem. Do not let a live
under-voltage banner become the explanation for every symptom.

## What each result actually means

**Option 4 — splitter, data leg → laptop, power leg → wall charger.** The only
configuration that never browned out. Power enters through the Pi's own fuse and
input protection. This is what PiKVM/TinyPilot ship, and it is the recommendation.

**Option 2 — 5V PSU on GPIO + VBUS-cut cable.** Failed, but read the failure
correctly: **the topology passed, the supply failed.** The HID gadget enumerated
and `/dev/video0` was present, which settles the open question — a VBUS-cut cable
does **not** break enumeration on the Pi 4B, because USB-C VBUS is tied to the 5V
rail and the GPIO supply asserts session-valid on its own. What failed was the 5V
brick: continuous under-voltage while *idle*, and dmesg never logged a
"Voltage normalised" to match its "Undervoltage detected!". Worth retrying only
with a verified 5.1V/3A supply and short, thick leads.

No load test was run for option 2. The board was already in live under-voltage
and live throttling at idle; adding a 4-core burn risked a real brownout crash
mid-write and SD corruption, and no load result could rescue a verdict of "fails
at idle".

**Option 1 — laptop USB-C only.** Survives idle, collapses under load: 51 of 57
samples in live under-voltage, core sagging to 0.8500V, ending at `0x50005`. A
laptop port typically advertises 900mA–1.5A while a Pi 4B + C790 wants 1.0–1.5A
idle with 2A+ spikes.

**Option 3 — 5V PSU on GPIO *plus* a live USB-C↔C to the laptop.** Excluded by
inspection, never wired. The Pi 4B has no reverse-current protection between
USB-C VBUS and the 5V rail (VBUS → polyfuse → 5V rail), and the GPIO 5V pins land
on that same rail bypassing even the fuse. Two supplies would be hard-paralleled:
the higher one (typically the 5.1V PSU) back-feeds into the **laptop's** USB port,
and no real load-sharing happens — whichever source is a few tens of mV higher
supplies everything. It risks the expensive half of the setup for a benefit that
does not exist.

## Beyond voltage: option 1 cannot do Wake-on-LAN

With the laptop as the only supply, **the Pi dies whenever the target sleeps or
shuts down** — so it can never wake it. Scheduled Wake-on-LAN structurally
requires external power. That disqualifies option 1 independently of how its
voltage measures.

## Recommendation

Ship and use **option 4**. If a GPIO-powered build is ever wanted (option 2's
topology is sound), qualify the supply first — the Pi 4 trips under-voltage below
~4.63V at the SoC and the official PSU is deliberately **5.1V** for that headroom.
A generic "5V 3A" brick plus thin DuPont leads can lose 200–300mV before the
current even arrives.
