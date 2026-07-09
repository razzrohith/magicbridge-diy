#!/usr/bin/env python3
"""
mb_edidconf.py - MagicBridge EDID identity overlay tool.

Rewrites ONLY the identity fields (manufacturer PNP ID, product code,
serial number, monitor-name descriptor) of an existing Pi-4-safe base EDID
blob, and fixes checksums. Deliberately does NOT touch timing descriptors,
established/standard timing bitmaps, or the CEA extension's timing data -
see EDID_CLONING_WORKFLOW.md section 1 for why: cloning a donor monitor's
raw EDID wholesale would import its 1080p60/4K modes, which the Pi 4's
2-CSI-lane TC358743 path (hard ceiling: 1080p50) cannot capture. Clone the
identity, not the raw timings.

STATUS: hardware-pending. There is currently no C790/TC358743 board wired
to this Pi, and no base EDID blob has been installed at the configured
path (see workflow doc section 3 - the base blob must come from the
Geekworm C790 wiki or PiKVM's tc358743-edid.hex, never hand-typed). This
module is exercised by stealth-dashboard.py's /stealth/api/apply
(action=edid_profile / edid_identity), which checks for both the hardware
and the base file before calling apply_identity_to_file(), and reports a
clear "pending" status instead of failing when either is missing.

Verified (without hardware) against a synthetic EDID structure: PNP-ID
encode/decode round-trip, checksum math, name-descriptor overwrite,
CEA Basic Audio Support bit set, and that non-identity/timing bytes are
left untouched.
"""
from __future__ import annotations
import re
from pathlib import Path

HEADER = bytes.fromhex("00ffffffffffff00")
DESC_OFFSETS = (54, 72, 90, 108)   # 4 x 18-byte descriptor blocks in base 128B
TAG_SERIAL = 0xFF   # Display Product Serial Number descriptor
TAG_NAME = 0xFC      # Display Product Name descriptor


class EdidError(Exception):
    pass


def encode_pnp_id(pnp: str) -> bytes:
    """3-letter PNP manufacturer ID (e.g. 'DEL', 'GSM', 'SAM') -> 2 packed bytes."""
    pnp = pnp.strip().upper()
    if len(pnp) != 3 or not pnp.isalpha():
        raise EdidError(f"PNP manufacturer ID must be exactly 3 letters, got {pnp!r}")
    c1, c2, c3 = (ord(ch) - ord('A') + 1 for ch in pnp)
    word = (c1 << 10) | (c2 << 5) | c3
    return bytes([(word >> 8) & 0xFF, word & 0xFF])


def decode_pnp_id(b: bytes) -> str:
    """Inverse of encode_pnp_id - used to read a donor's identity back out."""
    word = (b[0] << 8) | b[1]
    c1 = (word >> 10) & 0x1F
    c2 = (word >> 5) & 0x1F
    c3 = word & 0x1F
    return "".join(chr(c + ord('A') - 1) for c in (c1, c2, c3))


def _checksum_fix(block: bytearray) -> None:
    if len(block) != 128:
        raise EdidError("checksum fix expects a 128-byte block")
    block[127] = 0
    block[127] = (256 - sum(block) % 256) % 256


def _find_descriptor(block: bytearray, tag: int):
    """Return offset of an existing descriptor with the given tag, else None."""
    for off in DESC_OFFSETS:
        d = block[off:off + 18]
        if d[0] == 0 and d[1] == 0 and d[2] == 0 and d[3] == tag:
            return off
    return None


def _find_free_descriptor(block: bytearray):
    """Fallback: any descriptor slot that isn't a real detailed timing
    (a DTD has a nonzero pixel clock in bytes 0-1)."""
    for off in DESC_OFFSETS:
        d = block[off:off + 18]
        if d[0] == 0 and d[1] == 0:
            return off
    return None


def _write_text_descriptor(block: bytearray, offset: int, tag: int, text: str) -> None:
    payload = text.encode("ascii", "ignore")[:12] + b"\n"
    payload = payload.ljust(13, b" ")
    block[offset:offset + 18] = bytes([0, 0, 0, tag, 0]) + payload


def set_basic_audio_support(ext_block: bytearray) -> None:
    """Set the Basic Audio Support bit (bit 6 of byte 3) in a CEA-861
    extension block, then fix its checksum. Required per PiKVM's own
    TC358743 docs, or the source PC won't send LPCM audio over HDMI at all
    - see EDID_CLONING_WORKFLOW.md section 5 ('Audio' subsection), which
    ties directly into the audio pass-through feature (video.py)."""
    if len(ext_block) != 128:
        raise EdidError("CEA extension block must be 128 bytes")
    if ext_block[0] != 0x02:
        raise EdidError(f"Not a CEA-861 extension block (tag={ext_block[0]:#x})")
    ext_block[3] |= 0x40
    _checksum_fix(ext_block)


def apply_identity(raw: bytes, *, mfr_pnp: str, product_id: int, serial: int,
                    product_name: str, ensure_basic_audio: bool = True) -> bytes:
    """Overlay identity fields onto an existing base EDID (128 or 256 bytes).
    Does not touch any timing data. Returns the edited bytes; raises
    EdidError if `raw` doesn't look like a valid EDID base block."""
    if len(raw) < 128 or len(raw) % 128 != 0:
        raise EdidError(f"EDID must be a multiple of 128 bytes, got {len(raw)}")
    if raw[:8] != HEADER:
        raise EdidError("Missing standard EDID header (00 FF FF FF FF FF FF 00) - "
                         "is this really an EDID base blob?")

    data = bytearray(raw)
    base = data[0:128]

    base[8:10] = encode_pnp_id(mfr_pnp)
    base[10:12] = int(product_id).to_bytes(2, "little")
    base[12:16] = int(serial).to_bytes(4, "little")

    name_off = _find_descriptor(base, TAG_NAME)
    if name_off is None:
        name_off = _find_free_descriptor(base)
    if name_off is not None:
        _write_text_descriptor(base, name_off, TAG_NAME, product_name)
    # else: base has no spare descriptor slot for a name string - identity
    # still changes via mfr/product/serial above, just no display name text.

    _checksum_fix(base)
    data[0:128] = base

    if ensure_basic_audio and len(data) >= 256:
        ext = bytearray(data[128:256])
        try:
            set_basic_audio_support(ext)
            data[128:256] = ext
        except EdidError:
            pass  # not a CEA block / already fine - don't fail the whole apply over this

    return bytes(data)


def _load_bytes(path: str) -> bytes:
    """Base EDID files may be raw binary or v4l2-ctl-style hex text - detect
    and handle both."""
    raw = Path(path).read_bytes()
    if raw[:8] == HEADER:
        return raw
    try:
        text = raw.decode("ascii")
        hexonly = re.sub(r"\s+", "", text)
        decoded = bytes.fromhex(hexonly)
        if decoded[:8] == HEADER:
            return decoded
    except (UnicodeDecodeError, ValueError):
        pass
    raise EdidError(f"{path} doesn't look like a raw or hex-text EDID (no standard header found)")


def _save_hex_text(path: str, data: bytes) -> None:
    """Write in the plain hex-text form v4l2-ctl --set-edid=file= accepts:
    32 hex chars (16 bytes) per line, no separators."""
    hexstr = data.hex()
    lines = [hexstr[i:i + 32] for i in range(0, len(hexstr), 32)]
    Path(path).write_text("\n".join(lines) + "\n")


def apply_identity_to_file(base_path: str, out_path: str, **identity) -> dict:
    """Read base_path, overlay identity, write result to out_path (hex-text
    form ready for `v4l2-ctl --set-edid=file=<out_path> --fix-edid-checksums`).
    Returns a status dict; raises EdidError on malformed input rather than
    silently writing something broken - callers (stealth-dashboard.py) are
    expected to catch EdidError and report it, not crash."""
    raw = _load_bytes(base_path)
    edited = apply_identity(raw, **identity)
    _save_hex_text(out_path, edited)
    return {
        "ok": True,
        "base_path": base_path,
        "out_path": out_path,
        "size": len(edited),
        "has_cea_extension": len(edited) >= 256,
    }


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base", help="Path to base EDID (raw binary or hex text)")
    ap.add_argument("out", help="Path to write the identity-overlaid hex-text EDID")
    ap.add_argument("--mfr", required=True, help="3-letter PNP manufacturer ID, e.g. DEL")
    ap.add_argument("--product-id", type=lambda x: int(x, 0), required=True)
    ap.add_argument("--serial", type=lambda x: int(x, 0), required=True)
    ap.add_argument("--name", required=True, help="Monitor name string (<=12 chars used)")
    ap.add_argument("--no-audio-bit", action="store_true",
                     help="Don't force the CEA Basic Audio Support bit on")
    args = ap.parse_args()
    try:
        result = apply_identity_to_file(
            args.base, args.out,
            mfr_pnp=args.mfr, product_id=args.product_id, serial=args.serial,
            product_name=args.name, ensure_basic_audio=not args.no_audio_bit,
        )
        print(result)
    except EdidError as e:
        print(f"EDID error: {e}", file=sys.stderr)
        sys.exit(1)
