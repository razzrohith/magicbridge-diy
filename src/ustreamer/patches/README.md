# ustreamer patches

MagicBridge runs a lightly patched `ustreamer` (based on upstream 6.61). The
patches here are the only difference from stock, and each one carries the
measurement that justifies it.

## 0001-h264-min-qp-env.patch

Makes the H.264 encoder's `MIN_QP` settable at runtime via the environment
variable `MB_H264_MIN_QP`. `video.py` sets it from `video.min_qp` (default 30).

**Why this and not `--h264-bitrate`.** On the Pi 4B's `bcm2835-codec` the
bitrate target is inert: asking for 350 kbps and asking for 10000 kbps both
emit ~4.2 Mbit/s at 1080p25 (a 28x change in the request moved the output 7%).
Two attempts to fix that in the encoder setup — raising `MAX_QP` 32→45, and
re-applying `BITRATE` after the CAPTURE `S_FMT` — were built and measured and
did nothing, then rolled back. `MIN_QP` is different: it bounds bits *per
frame*, so it caps the motion peak that otherwise floods a slow uplink and
drags control latency with it. Measured static, same screen, gop 50:

    MIN_QP 16 (upstream) -> 1.26 Mbit/s
    MIN_QP 30            -> 0.64 Mbit/s

Live before/after on a real remote link: peak video 13.33 → 1.67 Mbit/s, p95
control RTT 938 → 27 ms.

Absent or invalid `MB_H264_MIN_QP` falls back to upstream's hardcoded 16, so a
stock binary still runs correctly — it just has no ceiling.

## Building

```sh
git clone https://github.com/pikvm/ustreamer && cd ustreamer
git checkout v6.61
for p in /path/to/src/ustreamer/patches/*.patch; do patch -p1 < "$p"; done
make WITH_JANUS=1        # plus the other WITH_* flags the install uses
sudo install -m755 src/ustreamer.bin /usr/local/bin/ustreamer
```

## Backup binary

A prebuilt copy of the patched binary for the shipped hardware lives at
`_goldenstate/ustreamer-minqp/ustreamer.minqp.bin`. Verify before trusting it:

    md5sum _goldenstate/ustreamer-minqp/ustreamer.minqp.bin
    # expect: b0b8d6f5531a9330272574eae9ac59c1

It is a convenience/restore artifact, not the source of truth — the patch is.
