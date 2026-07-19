#!/usr/bin/env python3
"""
Generate a MagicBridge EDID that caps ANY source at 1080p50.

WHY: the Pi 4B's 2-lane CSI can carry at most 1080p50 (UYVY). If the EDID
advertises 1080p60, sources pick it and capture fails with
"Device has requested 3 data lanes". By advertising 1080p50 as the ONLY
top mode, every source - Intel, AMD, NVIDIA, any laptop, any BIOS -
auto-negotiates something we can actually capture. No manual steps.

Built programmatically with computed checksums, then self-validated.
"""
import sys

def pnp(s):
    v = ((ord(s[0]) - 64) << 10) | ((ord(s[1]) - 64) << 5) | (ord(s[2]) - 64)
    return [(v >> 8) & 0xFF, v & 0xFF]

def dtd(pclk_khz, ha, hb, va, vb, hfp, hsw, vfp, vsw, wmm, hmm):
    """18-byte Detailed Timing Descriptor."""
    pc = pclk_khz // 10
    return [
        pc & 0xFF, (pc >> 8) & 0xFF,
        ha & 0xFF, hb & 0xFF, ((ha >> 8) << 4) | (hb >> 8),
        va & 0xFF, vb & 0xFF, ((va >> 8) << 4) | (vb >> 8),
        hfp & 0xFF, hsw & 0xFF,
        ((vfp & 0xF) << 4) | (vsw & 0xF),
        ((hfp >> 8) << 6) | ((hsw >> 8) << 4) | ((vfp >> 4) << 2) | (vsw >> 4),
        wmm & 0xFF, hmm & 0xFF, ((wmm >> 8) << 4) | (hmm >> 8),
        0, 0,
        0x1E,  # digital separate sync, +vsync, +hsync
    ]

def desc(tag, payload):
    """18-byte non-timing descriptor."""
    b = [0x00, 0x00, 0x00, tag, 0x00] + list(payload)
    return (b + [0x20] * 18)[:18]

W, H = 598, 336  # image size mm (16:9)

# ---- BASE BLOCK -------------------------------------------------------
e = [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]
# Realistic monitor identity by default (Dell P2419H) - the target must see an
# ordinary monitor, never "MagicBridge". Identity only; timings stay 1080p50.
e += pnp("DEL")                      # manufacturer (Dell)
e += [0x6B, 0xA0]                    # product code 0xA06B (P2419H), little-endian
e += [0x01, 0x00, 0x00, 0x00]        # serial
e += [0x01, 0x22]                    # week 1, year 2024 (0x22=34 -> 1990+34)
e += [0x01, 0x03]                    # EDID 1.3
e += [0x80, 0x3C, 0x22, 0x78, 0x0A]  # digital, 60x34cm, gamma 2.2, RGB
e += [0x0D, 0xC9, 0xA0, 0x57, 0x47, 0x98, 0x27, 0x12, 0x48, 0x4C]  # chromaticity
# established timings: 640x480@60, 800x600@60, 1024x768@60  (BIOS/UEFI safety)
e += [0x21, 0x08, 0x00]
# standard timings: 1280x720@60, 1024x768@60, 800x600@60, rest unused
e += [0x81, 0xC0, 0x61, 0x40, 0x45, 0x40]
e += [0x01, 0x01] * 5
# DTD1 = PREFERRED = 1920x1080p50 (148.5MHz, 2640x1125)
e += dtd(148500, 1920, 720, 1080, 45, 528, 44, 4, 5, W, H)
# DTD2 = 1280x720p60 fallback
e += dtd(74250, 1280, 370, 720, 30, 110, 40, 5, 5, W, H)
# range limits: 23-61Hz vert, 15-70kHz horiz, max 150MHz  (61 blocks 1080p60)
e += desc(0xFD, [23, 61, 15, 70, 15, 0x00, 0x0A, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20])
# monitor name (realistic, not "MagicBridge")
e += desc(0xFC, [ord(c) for c in "DELL P2419H"] + [0x0A])
e += [0x01]                          # 1 extension block
e += [(256 - (sum(e) % 256)) % 256]  # checksum
assert len(e) == 128, len(e)

# ---- CTA-861 EXTENSION ------------------------------------------------
# VICs: 1080p50 first (native). NOTHING above 1080p50. No VIC 16 (1080p60).
vics = [31 | 0x80,  # 1920x1080p50  <-- native/preferred
        34,         # 1920x1080p30
        33,         # 1920x1080p25
        32,         # 1920x1080p24
        19,         # 1280x720p50
        4,          # 1280x720p60
        17,         # 720x576p50
        2,          # 720x480p60
        1]          # 640x480p60
vdb = [(2 << 5) | len(vics)] + vics
adb = [(1 << 5) | 3, 0x09, 0x07, 0x07]        # LPCM 2ch, 32/44.1/48k, 16/20/24-bit
sab = [(4 << 5) | 3, 0x01, 0x00, 0x00]        # front L/R
vsdb = [(3 << 5) | 5, 0x03, 0x0C, 0x00, 0x10, 0x00]  # HDMI, phys addr 1.0.0.0

blocks = vdb + adb + sab + vsdb
dtd_off = 4 + len(blocks)
x = [0x02, 0x03, dtd_off, 0xC1]  # CTA rev3, underscan+audio+1 native DTD
x += blocks
x += dtd(148500, 1920, 720, 1080, 45, 528, 44, 4, 5, W, H)  # repeat 1080p50
x += [0x00] * (127 - len(x))
x += [(256 - (sum(x) % 256)) % 256]
assert len(x) == 128, len(x)

full = e + x

# ---- VALIDATE ---------------------------------------------------------
ok = True
if sum(full[0:128]) % 256 != 0:
    print("!! base checksum BAD"); ok = False
if sum(full[128:256]) % 256 != 0:
    print("!! ext checksum BAD"); ok = False
if full[0:8] != [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]:
    print("!! header BAD"); ok = False
if 16 in [v & 0x7F for v in vics]:
    print("!! VIC 16 (1080p60) present - would break capture"); ok = False
print("checksums OK, header OK, no 1080p60 advertised:", ok)
print("preferred timing: 1920x1080 @ 50Hz (148.5MHz)")
print("VIC list:", [v & 0x7F for v in vics])

import os as _os
out = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "mb-edid-1080p50.hex")
with open(out, "w", newline="\n") as f:
    for blk in (full[:128], full[128:]):
        for i in range(0, 128, 16):
            f.write(" ".join("%02x" % b for b in blk[i:i + 16]) + "\n")
        f.write("\n")
print("written:", out)
if not ok:
    sys.exit(1)
