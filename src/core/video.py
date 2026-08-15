#!/usr/bin/env python3
"""
MagicBridge Video Capture Manager

Auto-detects the capture hardware and picks the right pipeline, defaulting to
the C790:
  - C790 / TC358743 HDMI->CSI-2 board (DEFAULT/preferred): hardware H.264 +
    Janus WebRTC (low latency), with the restricted 1080p50 EDID for stealth.
  - USB UVC HDMI dongle (MS2109/MS2130, Elgato Cam Link, UGREEN, etc.): native
    MJPEG. Used automatically when no CSI board is present.
Both are V4L2 devices; device_type() classifies them and start() resolves
mode="auto" to "h264" (csi) or "mjpeg" (usb).

Primary streamer: ustreamer (apt install ustreamer)
Fallback:         ffmpeg (apt install ffmpeg)
"""
import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger("magicbridge.video")

STREAM_HOST = "127.0.0.1"
STREAM_PORT = 8081   # nginx proxies /stream → this port

# How long a probed device inventory/classification stays trusted. The V4L2
# picture only changes on hot-plug, but /api/status polls it every 5s per
# connected client and each poll forked one `v4l2-ctl --info` per /dev/video*
# node (~15 fork+execs per poll on the C790 Pi) just to redraw the same values.
_DEV_CACHE_TTL = 30.0


def _clean_divisor_fps(req: int, src: int) -> int:
    """Largest clean-integer-divisor of the source refresh that is <= the
    requested fps, so ustreamer decimates evenly (source/N) instead of in a
    ragged keep/drop pattern. Examples: (50,60)->30, (50,50)->50, (30,60)->30,
    (60,60)->60. Falls back to min(req,src) when the source has no sensible
    divisor near the request (e.g. an oddball non-standard refresh), rather than
    crawling down to 1 fps just to be 'clean'."""
    try:
        req = int(req)
    except (TypeError, ValueError):
        return 30
    if not src or src <= 0:
        return req
    if req >= src:
        return src                      # can't exceed the source; 1:1 is ideal
    best = max((d for d in range(1, req + 1) if src % d == 0), default=req)
    # Floor was 20, which broke the low-fps presets: for a 50Hz source the
    # divisors jump 25 -> 10, so any request of 11-24 (the "Low" preset asks 15)
    # produced best=10, tripped the >=20 floor, and fell back to a RAGGED
    # min(req,src)=15 (50/15 = 3.33 uneven decimation) - the exact jitter this
    # function exists to avoid, on the one preset a weak-link operator reaches
    # for. 10 and 5 are perfectly good clean divisors for a KVM, so only fall
    # back when there is genuinely no sane divisor near the request.
    return best if best >= 5 else min(req, src)


class VideoManager:
    """Start/stop/restart the MJPEG video stream from a V4L2 capture device."""

    def __init__(self):
        self.process    = None
        self.device     = None
        self.resolution = "1920x1080"
        # 50, not 30: --h264-boost lifts the encoder ceiling to ~46fps at
        # 1080p, but only if we actually ASK for more than 25 - desired-fps
        # is a hard request, not a target. Safe to set high because the CSI
        # path caps it DOWN to whatever the source really sends, and a USB
        # dongle simply delivers its native rate.
        self.fps        = 50
        self._src_fps   = 0       # last CSI source frame rate; caps the request without overwriting it
        self.quality    = 90      # MJPEG quality 1-100
        # H.264 target bitrate (kbps) for the WebRTC path. Was hardcoded to
        # 5000 in the ustreamer command, which made the UI's Low/Bal/Sharp
        # bandwidth presets nearly meaningless over WebRTC: they only moved
        # resolution (ignored on CSI - we follow the source signal) and fps.
        self.bitrate    = 5000
        self.mode       = "auto"  # "auto" (detect hardware) -> "h264" (C790/CSI + Janus WebRTC, preferred) | "mjpeg" (MS2109/USB)
        self.port       = STREAM_PORT
        self.h264_sink  = None    # ustreamer memsink name, set when h264 mode starts
        self._lock      = threading.Lock()
        self._mon_thr   = None    # watchdog thread
        self._dev_cache    = None  # memoized detect_devices() result
        self._dev_cache_at = 0.0   # time.monotonic() of that result
        self._dtype_cache  = {}    # dev -> (kind, time.monotonic())

    # Device discovery

    def _invalidate_device_cache(self):
        """Forget the memoized device inventory and classifications.

        Called from start()/restart(), i.e. exactly the moments the hardware
        picture may have changed (hot-plug, a USB node re-enumerating, a capture
        board that vanished). A stale answer must never be what tells us the
        C790 is still there, so recovery paths always re-probe."""
        self._dev_cache    = None
        self._dev_cache_at = 0.0
        self._dtype_cache  = {}

    def detect_devices(self) -> list:
        """Return list of V4L2 VIDEO_CAPTURE devices with metadata.

        Memoized for _DEV_CACHE_TTL (see the constant: this is the /api/status
        polling cost). start()/restart() invalidate explicitly, so a hot-plug is
        never hidden for longer than one stream (re)start."""
        now = time.monotonic()
        if self._dev_cache is not None and (now - self._dev_cache_at) < _DEV_CACHE_TTL:
            return self._dev_cache
        devices = []
        for dev in sorted(glob.glob("/dev/video*")):
            try:
                r = subprocess.run(
                    ["v4l2-ctl", "--device", dev, "--info"],
                    capture_output=True, text=True, timeout=2
                )
                if "Video Capture" not in r.stdout:
                    continue
                name = dev
                m = re.search(r"Card type\s*:\s*(.+)", r.stdout)
                if m:
                    name = m.group(1).strip()
                bus = ""
                m2 = re.search(r"Bus info\s*:\s*(.+)", r.stdout)
                if m2:
                    bus = m2.group(1).strip()
                devices.append({"device": dev, "name": name, "bus": bus})
            except Exception:
                continue
        self._dev_cache    = devices
        self._dev_cache_at = now
        return devices

    def get_best_device(self) -> str:
        """Return best V4L2 capture device.

        Priority order:
          1. C790/TC358743 CSI capture node — matched by name/bus containing
             "tc358743" or "unicam" (the kernel driver name for the Pi's CSI
             receiver block that the TC358743 sits behind). This is the
             board this whole video-latency upgrade is built around, so it
             outranks everything else once present, INCLUDING a USB dongle
             that might still be plugged in during a transition period.
          2. USB capture devices (the MS2109 dongle, or any other UVC card).
             Kept as the #2 priority for back-compat with the pre-C790 setup.
          3. Anything else V4L2 reports (e.g. bcm2835-isp platform nodes) —
             last resort, generic fallback. NOTE: with a real C790 attached,
             several bcm2835-isp "platform:" nodes typically also show up
             (ISP passthrough stages) alongside the actual unicam/tc358743
             node — rule 1 exists specifically so auto-detect doesn't
             accidentally land on one of those instead of the real capture
             node. Confirm against `v4l2-ctl --list-devices` once the board
             is physically installed; the exact "Card type"/"Bus info"
             strings can vary by kernel version.
        """
        devs = self.detect_devices()
        if not devs:
            return None

        def _is_csi_board(d):
            blob = (d.get("name", "") + " " + d.get("bus", "")).lower()
            return "tc358743" in blob or "unicam" in blob

        csi = [d for d in devs if _is_csi_board(d)]
        if csi:
            log.info("Auto-selected C790/CSI capture device: %s (%s)", csi[0]["device"], csi[0]["name"])
            return csi[0]["device"]

        usb = [d for d in devs if d["bus"].startswith("usb")]
        if usb:
            log.info("Auto-selected USB capture device: %s (%s)", usb[0]["device"], usb[0]["name"])
            return usb[0]["device"]

        log.info("No USB or C790/CSI device found, falling back to first V4L2 device: %s", devs[0]["device"])
        return devs[0]["device"]

    def device_type(self, dev: str = None) -> str:
        """Classify a capture device so the stream mode can auto-follow the
        hardware: 'csi' (C790/TC358743 on the Pi CSI port), 'usb' (a UVC HDMI
        dongle like the MS2109), or 'other'. CSI -> hardware H.264/WebRTC (the
        preferred/default path); USB -> MJPEG.

        Memoized for _DEV_CACHE_TTL like detect_devices (status() classifies on
        every poll), but the cache is BYPASSED the moment the node itself is
        gone: the watchdog classifies on the live recovery path and restart()
        re-resolves the capture mode from this, so a stale 'csi' must never mask
        a capture board that actually disappeared."""
        dev = dev or self.device
        if not dev:
            return "other"
        now = time.monotonic()
        hit = self._dtype_cache.get(dev)
        if hit and (now - hit[1]) < _DEV_CACHE_TTL and os.path.exists(dev):
            return hit[0]
        try:
            r = subprocess.run(["v4l2-ctl", "--device", dev, "--info"],
                               capture_output=True, text=True, timeout=2)
            blob = r.stdout.lower()
        except Exception:
            return "other"     # a probe that blew up is not a fact worth caching
        if "tc358743" in blob or "unicam" in blob or "fe801000" in blob:
            kind = "csi"
        else:
            m = re.search(r"bus info\s*:\s*(\S+)", blob)
            kind = "usb" if (m and m.group(1).startswith("usb")) else "other"
        self._dtype_cache[dev] = (kind, now)
        return kind

    # Audio (C790 I2S HDMI de-embedded audio -> Janus, see h264.md's audio block)

    def detect_audio_device(self) -> str:
        """Best-effort detection of the ALSA capture card fed by the C790's
        I2S output (dtoverlay=tc358743-audio). Returns an "hw:N" string, or
        None if nothing looks like it, so callers can fall back sanely
        instead of feeding Janus a guess that's silently wrong.

        Real ALSA card naming for this overlay varies (seen as "tc358743"
        or similar in different kernel/overlay versions), so this matches
        loosely on the card list rather than assuming a fixed index like
        the upstream docs' "hw:1" example — that number depends on what
        else (HDMI audio out, USB audio, etc.) is enumerated first on a
        given boot, so hardcoding it is exactly the kind of guess this
        avoids.
        """
        try:
            out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return None
        csi_dev = usb_dev = None
        for line in out.splitlines():
            m = re.match(r"card (\d+):.*\[(.*?)\]", line)
            if not m:
                continue
            card, low = m.group(1), line.lower()
            # C790/TC358743 HDMI-audio capture - NOT the Pi's own bcm2835 HDMI.
            is_csi = "tc358743" in low or "csi" in low or ("hdmi" in low and "bcm2835" not in low)
            # A USB-audio class capture adapter (line-in from the target).
            is_usb = ("usb" in low or "uac" in low) and "bcm2835" not in low
            if is_csi and csi_dev is None:
                csi_dev = (card, line.strip())
            elif is_usb and usb_dev is None:
                # A cheap USB audio adapter (line-in from the target's audio out)
                # is the RELIABLE audio path: the C790's own I2S output is a
                # parked upstream driver dead-end (arecord EIOs), so a USB-audio
                # class capture device is what actually delivers sound to WebRTC.
                usb_dev = (card, line.strip())
        # Prefer the C790 if it ever works; otherwise the USB-audio adapter.
        if csi_dev:
            log.info("Auto-detected C790/TC358743 audio capture: hw:%s (%s)", csi_dev[0], csi_dev[1])
            return f"hw:{csi_dev[0]}"
        if usb_dev:
            log.info("Auto-detected USB audio capture: hw:%s (%s)", usb_dev[0], usb_dev[1])
            return f"hw:{usb_dev[0]}"
        log.info("No audio capture card found in `arecord -l` (no C790 I2S and no "
                 "USB-audio adapter). Video works fine without it.")
        return None

    def _probe_audio_capture(self, device: str) -> bool:
        """Return True only if `device` actually yields PCM samples right now.

        Two things make a merely-present capture card useless: the C790/TC358743
        I2S path enumerates as a card but EIOs on read (a parked upstream-driver
        issue), and the HDMI source may not be sending audio at all. Either way
        we must NOT hand Janus a device that can't capture, or its audio thread
        churns on a dead endpoint. A short arecord to raw stdout is the honest
        test. Cached briefly so repeated stream starts don't re-probe (the probe
        runs under the video lock, so it must stay cheap)."""
        cache = getattr(self, "_audio_probe_cache", None)
        if cache is None:
            cache = self._audio_probe_cache = {}
        hit = cache.get(device)
        now = time.monotonic()
        if hit and (now - hit[1]) < 120:
            return hit[0]
        ok = False
        try:
            r = subprocess.run(
                ["arecord", "-D", device, "-f", "S16_LE", "-r", "48000",
                 "-c", "2", "-d", "1", "-t", "raw"],
                capture_output=True, timeout=3)
            # EIO path exits non-zero with empty PCM; a real capture exits 0 with data.
            ok = (r.returncode == 0 and len(r.stdout) > 0)
        except Exception:
            ok = False
        cache[device] = (ok, now)
        return ok

    def _sync_janus_audio_cfg(self):
        """Rewrite janus.plugin.ustreamer.jcfg's audio block with whatever audio
        device ACTUALLY captures, so a wrong install-time guess, a shifted card
        index, or a broken/absent device all self-heal. No-ops safely if Janus
        isn't installed — h264 video keeps working either way; audio is additive,
        never load-bearing for the video path.

        Two bugs fixed here: (1) the old code wrote to
        /opt/janus/lib/janus/configs/, which is NOT the folder Janus reads
        (it runs with --configs-folder /opt/janus/etc/janus), so the audio block
        never reached Janus. We now write to whichever config actually exists.
        (2) It configured a device without checking it works, so the dead C790
        I2S path (arecord EIO) got handed to Janus. We now probe first and, if
        nothing captures, STRIP any stale audio block instead of leaving Janus
        pointed at a dead endpoint."""
        candidates = [
            "/opt/janus/etc/janus/janus.plugin.ustreamer.jcfg",          # --configs-folder (what Janus reads)
            "/opt/janus/lib/janus/configs/janus.plugin.ustreamer.jcfg",  # legacy layout
            "/usr/local/etc/janus/janus.plugin.ustreamer.jcfg",
        ]
        paths = [p for p in candidates if os.path.isfile(p)]
        if not paths:
            self._audio_cfg_written = False
            return
        dev = self.detect_audio_device()
        working = bool(dev) and self._probe_audio_capture(dev)
        self._audio_active = dev if working else None
        # SCHEMA AMBIGUITY, still to be confirmed ON THE DEVICE: src/
        # install_janus_webrtc.sh and docs/DIY_PROGRESS.md say the pinned
        # ustreamer v6.61 plugin reads "acap.device" (section "acap"), while
        # upstream PiKVM ships an "audio: { ... }" block for the same plugin.
        # Nothing in this tree can settle which one the BUILT plugin parses, so
        # don't flip the key on a guess: keep writing the block we have always
        # written, and strip BOTH spellings below so a stale block of either
        # schema can never be left behind pointing Janus at a dead capture card.
        # Settle it by reading janus/src/config.c in the ustreamer source that
        # was actually built on the Pi, then drop the spelling that isn't real.
        new_block = (
            "audio: {\n"
            f'    device = "{dev}"\n'
            f'    tc358743 = "{self.device}"\n'
            "}\n"
        ) if working else ""
        stale_re = re.compile(r"(?m)^[ \t]*(?:audio|acap)\s*:\s*\{[^}]*\}[ \t]*\r?\n?", re.S)
        wrote = False
        for jcfg_path in paths:
            try:
                text = open(jcfg_path).read()
                # Strip every stale audio block (either schema) first, then
                # re-append. Replacing in place would have inserted the new
                # block once per match if both spellings were present.
                text = stale_re.sub("", text)
                if working:
                    text = text.rstrip() + "\n" + new_block
                open(jcfg_path, "w").write(text)
                wrote = wrote or working
            except Exception as e:
                log.warning("Could not sync Janus audio config at %s (non-fatal): %s", jcfg_path, e)
        # Only a block that really reached a Janus config counts as configured
        # audio; status() reports from this, not from the bare arecord probe.
        self._audio_cfg_written = wrote
        log.info("Synced Janus audio config: device=%s working=%s -> %d file(s)",
                 dev, working, len(paths))

    def get_resolutions(self, device: str = None) -> list:
        """Return sorted list of supported resolutions for a device."""
        dev = device or self.device or "/dev/video0"
        defaults = ["1920x1080", "1280x720", "854x480", "640x480"]
        try:
            r = subprocess.run(
                ["v4l2-ctl", "--device", dev, "--list-formats-ext"],
                capture_output=True, text=True, timeout=5
            )
            seen: set = set()
            for m in re.finditer(r"(\d{3,4})x(\d{3,4})", r.stdout):
                w, h = int(m.group(1)), int(m.group(2))
                if w >= 640 and h >= 360:
                    seen.add(f"{w}x{h}")
            if seen:
                return sorted(seen, key=lambda s: -int(s.split("x")[0]))
        except Exception:
            pass
        return defaults

    def detect_csi_timings(self, device: str = None):
        """(width, height, fps) of the LIVE HDMI signal on a CSI capture, or None.

        The TC358743 re-locks its capture format to whatever the source is
        actually sending, so the driver is the authority - not config.json.
        Asking ustreamer for a resolution the device is not in produces no
        frames at all (the UI just says "No Signal"), or torn/garbage frames on
        the MJPEG path. Seen live: an iPad negotiated 1280x720@60 while config
        still said 1920x1080@50, and the stream was dead until this matched.
        """
        dev = device or self.device or "/dev/video0"
        try:
            r = subprocess.run(["v4l2-ctl", "--device", dev, "--query-dv-timings"],
                               capture_output=True, text=True, timeout=5)
            w = re.search(r"Active width:\s*(\d+)", r.stdout)
            h = re.search(r"Active height:\s*(\d+)", r.stdout)
            f = re.search(r"\(([\d.]+) frames per second\)", r.stdout)
            if not (w and h):
                return None
            width, height = int(w.group(1)), int(h.group(1))
            if width < 160 or height < 120:      # 0x0 = no signal locked
                return None
            fps = int(round(float(f.group(1)))) if f else 0
            return (width, height, fps)
        except Exception as e:
            log.debug("detect_csi_timings failed: %s", e)
            return None

    def get_best_mjpeg_resolution(self, device: str = None) -> str:
        """Return the highest native MJPEG resolution the capture card supports.

        Parses v4l2-ctl --list-formats-ext, looking only at resolutions listed
        under the MJPG/MJPEG pixel format block (not YUYV, which requires slow
        software conversion).  Falls back to None if detection fails.

        Typical MS2109 MJPEG resolutions: 1920x1080, 1280x720, 848x480, 640x480
        """
        dev = device or self.device or "/dev/video0"
        try:
            r = subprocess.run(
                ["v4l2-ctl", "--device", dev, "--list-formats-ext"],
                capture_output=True, text=True, timeout=5
            )
            in_mjpeg = False
            best_w, best_h = 0, 0
            for line in r.stdout.splitlines():
                stripped = line.strip()
                # Detect start of MJPEG format block
                if ("'MJPG'" in stripped or "MJPEG" in stripped) and "ioctl" not in stripped:
                    in_mjpeg = True
                # Detect start of a different format block (YUYV, NV12, etc.)
                elif stripped.startswith("[") and in_mjpeg and "MJPG" not in stripped and "MJPEG" not in stripped:
                    in_mjpeg = False
                if in_mjpeg:
                    m = re.search(r"(\d{3,4})x(\d{3,4})", line)
                    if m:
                        w, h = int(m.group(1)), int(m.group(2))
                        if w * h > best_w * best_h:
                            best_w, best_h = w, h
            if best_w > 0:
                log.info("Native MJPEG best resolution from %s: %dx%d", dev, best_w, best_h)
                return f"{best_w}x{best_h}"
        except Exception as e:
            log.debug("get_best_mjpeg_resolution failed: %s", e)
        return None

    # Stream control

    def start(self, device: str = None, resolution: str = None,
              fps: int = None, quality: int = None, mode: str = None,
              bitrate: int = None) -> bool:
        """Start streaming. Returns True on success. Safe to call repeatedly.

        Resolution selection priority:
          1. Explicit `resolution` argument (e.g. from stealth panel override)
          2. Auto-detect highest native MJPEG resolution from capture card
          3. Self.resolution fallback (default 1280x720)

        mode:
          "mjpeg" (default) - existing MS2109/USB-dongle path, unchanged.
          "h264"             - C790/CSI hardware H.264 + WebRTC path (see
                               _start_ustreamer_h264). Only meaningful once
                               the C790 board + Janus gateway are installed;
                               automatically falls back to mjpeg if the
                               ustreamer build or device don't support it.
        """
        with self._lock:
            # Clamp everything the API can set to sane bounds. Unclamped, the
            # settings endpoint passed these straight through: fps=0 or -5 became
            # --desired-fps 0 and --h264-gop 0 (invalid GOP breaks SPS/IDR
            # pairing => WebRTC late-join fails), a huge bitrate reached the
            # encoder, and a non-numeric value raised inside the executor => 500.
            def _clampi(v, lo, hi, dflt):
                try: return max(lo, min(hi, int(v)))
                except (TypeError, ValueError): return dflt
            if device:              self.device     = device
            if resolution:          self.resolution = resolution
            if bitrate is not None: self.bitrate    = _clampi(bitrate, 100, 20000, self.bitrate)
            if fps is not None:     self.fps        = _clampi(fps, 1, 60, self.fps)
            if quality is not None: self.quality    = _clampi(quality, 1, 100, self.quality)
            if mode:                self.mode       = mode

            # Everything below picks the device and resolves the capture mode
            # from probed hardware, so throw away the memoized inventory first:
            # a (re)start is exactly when the board may have been plugged,
            # unplugged or re-enumerated to a different /dev/videoN.
            self._invalidate_device_cache()

            if not self.device:
                self.device = self.get_best_device()
            if not self.device:
                log.warning("No V4L2 capture device found")
                return False

            # Resolve "auto" mode from the detected capture hardware: the
            # C790/TC358743 CSI board -> hardware H.264 + WebRTC (preferred), a
            # USB UVC dongle (or anything else) -> MJPEG. The h264 launcher still
            # falls back to MJPEG on its own if the Janus-enabled ustreamer isn't
            # present, so this stays safe on a CSI board without WebRTC built.
            if self.mode in (None, "", "auto"):
                dtype = self.device_type(self.device)
                self.mode = "h264" if dtype == "csi" else "mjpeg"
                log.info("Auto capture mode: %s is '%s' -> %s mode",
                         self.device, dtype, self.mode)

            # CSI: follow the LIVE signal, never the configured resolution.
            # The TC358743 re-locks to whatever the source sends, so a config
            # value of 1920x1080 against a source doing 1280x720 means ustreamer
            # asks for a format the device is not in -> zero frames -> the UI
            # says "No Signal" while `--query-dv-timings` clearly shows a signal.
            # That mismatch was also producing the long-standing torn/green
            # frames on the MJPEG path, so it is fixed for both.
            if self.device_type(self.device) == "csi":
                det = self.detect_csi_timings(self.device)
                if det:
                    dw, dh, dfps = det
                    detected = f"{dw}x{dh}"
                    if detected != self.resolution:
                        log.info("CSI signal is %s@%sfps - overriding configured %s "
                                 "(the device follows the source, so we must too)",
                                 detected, dfps or "?", self.resolution)
                        self.resolution = detected
                    # Record the source rate to CAP the request in the launcher
                    # (below), instead of overwriting self.fps. Overwriting it
                    # ratcheted down permanently: once a 30fps source was seen,
                    # self.fps stuck at 30 and never recovered when a 50fps
                    # source returned (30 < 30 is false). self.fps stays the
                    # user's request; _src_fps is the ceiling.
                    self._src_fps = dfps or 0
                else:
                    self._src_fps = 0
                    log.warning("CSI device %s reports no locked timings - "
                                "no HDMI signal? keeping configured %s",
                                self.device, self.resolution)

            # Auto-detect best native MJPEG resolution when not explicitly set.
            # This lets MagicBridge adapt to any laptop/screen resolution automatically:
            # 14" 1080p, 16" 1440p, 4K, etc. Just uses whatever the capture card signals.
            # Skipped in h264 mode: CSI resolution is decided above from the live signal.
            if not resolution and self.mode != "h264":
                best = self.get_best_mjpeg_resolution(self.device)
                if best and best != self.resolution:
                    log.info("Auto-resolution: %s -> %s (native MJPEG from capture card)",
                             self.resolution, best)
                    self.resolution = best

            self._stop_locked()

            if shutil.which("ustreamer"):
                if self.mode == "h264":
                    ok = self._start_ustreamer_h264()
                else:
                    ok = self._start_ustreamer()
            else:
                log.info("ustreamer not found, using ffmpeg fallback")
                ok = self._start_ffmpeg()

            return ok

    def stop(self):
        with self._lock:
            self._stop_locked()

    def restart(self):
        """Re-run the FULL start path, not a relaunch with cached values.

        The old restart() relaunched using self.resolution/self.mode verbatim
        and dropped the lock across a sleep before an unconditional launch. Two
        consequences the audit caught:
          - a source resolution change (1080p50 -> 720p60) left it relaunching
            at the stale resolution => permanent "No Signal" while a signal is
            present, never self-correcting (only start() re-runs
            detect_csi_timings).
          - dropping the lock then launching without a _stop_locked() first
            could orphan a ustreamer still holding the capture device, so the
            new process couldn't open it and the stream stayed dead.
        start() re-detects the device, follows the live signal, re-resolves
        mode, and holds the lock across stop+launch - so delegating to it fixes
        all of the above atomically.
        """
        # Re-probe from scratch: this is a recovery path, so the cached
        # inventory/classification below must not be able to report a board
        # that is no longer there (or miss one that just appeared).
        self._invalidate_device_cache()
        # If the current node vanished (USB re-enumerated to a new /dev/videoN),
        # forget it so start() re-detects instead of looping on a dead node.
        if self.device and not os.path.exists(self.device):
            log.info("capture node %s is gone - re-detecting on restart", self.device)
            self.device = None
        # If mode got stuck at mjpeg on a CSI board from an old transient H.264
        # failure, let auto re-resolve it back to h264.
        if self.mode == "mjpeg" and self.device and self.device_type(self.device) == "csi":
            log.info("CSI device stuck in mjpeg mode - re-resolving via auto")
            self.mode = "auto"
        self.start()

    def update_quality(self, quality: int) -> bool:
        """Change MJPEG quality without full restart (restarts ustreamer)."""
        self.quality = max(1, min(100, quality))
        return self.start()

    # Internal stream launchers

    def _stop_locked(self):
        """Stop process (caller holds lock)."""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def _start_ustreamer(self) -> bool:
        """Launch ustreamer MJPEG server.

        Uses Debian Bookworm apt package flags (ustreamer 4.9).
        Key flags for v4.9:
          --resolution WxH  (replaces old --width / --height)
          --desired-fps N   (still the correct flag in 4.9; NOT --fps)
          --host address    (long form; short form is -s, NOT -H)
          --persistent      (keep running if HDMI unplugged)
        """
        # If ustreamer.service (systemd) owns the process, stop it first so we can
        # launch our own subprocess with the actual requested device/resolution/fps/
        # quality. Previously this just skipped straight to "return True" whenever
        # the systemd unit was active, which meant changing settings from the UI
        # silently did nothing. The systemd unit kept running with its original
        # hardcoded flags while /api/status reported the (unapplied) new settings.
        try:
            import subprocess as _sp
            _r = _sp.run(['systemctl', 'is-active', 'ustreamer.service'],
                         capture_output=True, text=True, timeout=2)
            if _r.stdout.strip() == 'active':
                log.info("Stopping systemd-managed ustreamer.service to apply settings directly")
                _sp.run(['systemctl', 'stop', 'ustreamer.service'],
                        capture_output=True, timeout=5)
                time.sleep(0.3)
        except Exception:
            pass
        self._running = False
        # MJPEG produces no memsink. Clear any sink name left over from an
        # earlier h264 run: it was only ever cleared in _fallback_or_fail, so
        # after a deliberate h264 -> mjpeg switch status() kept reporting the
        # (long gone) sink, and the UI kept ticking WebRTC audio green with it.
        self.h264_sink = None
        cmd = [
            "ustreamer",
            "--device",         self.device,
            "--format",         "MJPEG",         # use native HW MJPEG from capture card (not YUYV software-convert)
            "--resolution",     self.resolution,
            "--desired-fps",    str(self.fps),
            "--quality",        str(self.quality),
            "--host",           STREAM_HOST,
            "--port",           str(self.port),
            "--workers",        "2",
            # Same buffer-starvation fix as the H.264 path above: keep clearly
            # more buffers than workers so a frame is never recycled mid-encode.
            "--buffers",        "6",
            "--persistent",
            # drop-same-frames removed
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(0.8)
            if self.process.poll() is not None:
                log.warning("ustreamer exited immediately, check device/resolution")
                self.process = None
                return False
            log.info("ustreamer: %s %s %dfps q=%d → %s:%d",
                     self.device, self.resolution, self.fps,
                     self.quality, STREAM_HOST, self.port)
            return True
        except FileNotFoundError:
            log.error("ustreamer binary not found")
            return False
        except Exception as e:
            log.error("ustreamer start error: %s", e)
            return False

    def _start_ustreamer_h264(self) -> bool:
        """Launch ustreamer with hardware H.264 encode for the C790/CSI capture
        path, feeding a memsink that the Janus WebRTC gateway's ustreamer
        plugin reads from for near-zero-latency WebRTC delivery.

        Requirements (see MAGICBRIDGE_HANDBOOK.md / video_latency_upgrade
        notes for the full install steps):
          - C790 HDMI-to-CSI2 board wired into the Pi 4 CSI port (not USB)
          - ustreamer built with WITH_JANUS=1 (memsink support) — the plain
            apt package may not have this; install_janus_webrtc.sh builds
            Janus Gateway first (source, pinned tag — its headers are a
            build-time dependency of ustreamer's Janus plugin) then
            ustreamer, installing the result ahead of /usr/bin/ustreamer
            in PATH
          - Pi 4 GPU V4L2 M2M H.264 encoder available (stock Raspberry Pi
            OS Bookworm exposes this automatically; ustreamer auto-selects
            it, override via --h264-m2m-device if needed)
          - Janus WebRTC Gateway installed & running with its ustreamer
            plugin configured to read the same --h264-sink name below

        Safe to call before any of the above exists: if the ustreamer binary
        doesn't understand these flags, or the process dies immediately
        (e.g. still pointed at the old MS2109 USB device), this method logs
        why and falls back to the existing MJPEG path automatically so the
        stream never just goes dark.
        """
        try:
            import subprocess as _sp
            _r = _sp.run(['systemctl', 'is-active', 'ustreamer.service'],
                         capture_output=True, text=True, timeout=2)
            if _r.stdout.strip() == 'active':
                log.info("Stopping systemd-managed ustreamer.service to apply settings directly")
                _sp.run(['systemctl', 'stop', 'ustreamer.service'],
                        capture_output=True, timeout=5)
                time.sleep(0.3)
        except Exception:
            pass

        self._running = False
        sink_name = "magicbridge::h264"
        self.h264_sink = sink_name
        # Effective fps = the user's request capped by what the source actually
        # sends (never overwrite self.fps - see _src_fps). Clamp the H.264
        # resolution to the Pi-4 M2M encoder's 1080p ceiling: detect_csi_timings
        # follows the source with only a lower bound, so a >1080p source (EDID
        # cap bypassed) would otherwise hand 1440p/4K to the encoder, which
        # rejects it -> dead stream.
        # Effective fps must be a CLEAN INTEGER DIVISOR of the source refresh,
        # not just <= it. The PiKVM sibling's worst video regression: asking for
        # 50 fps on a 60 Hz source makes ustreamer decimate 60->50 in an uneven
        # 6:5 keep/drop pattern -> irregular frame spacing -> the WebRTC jitter
        # buffer inflates (~500ms) and counts late frames as loss, triggering a
        # keyframe storm and stutter. Our restricted EDID caps compliant sources
        # at 50 Hz (so 50->50 is already 1:1), but a non-compliant adapter can
        # still present 60 (an iPad HDMI dongle did exactly that here), so snap
        # to the largest divisor of the source that is <= the request.
        _eff_fps = _clean_divisor_fps(self.fps, self._src_fps)
        _eff_fps = max(1, min(60, _eff_fps))
        # Clamp ONLY the value handed to ustreamer's --resolution; do NOT
        # overwrite self.resolution. self.resolution must keep the TRUE detected
        # source resolution because the watchdog (start_watchdog) compares the
        # live signal against it to decide when the source changed. If the clamp
        # falsified self.resolution to 1080p, a >1080p source would read as a
        # PERMANENT mismatch (live 1440p != stored 1080p) and the watchdog would
        # restart every ~30s forever - a Janus-bouncing storm - instead of
        # settling on a stable "can't encode this" dark stream. Keep the source
        # value; only cap what the encoder is asked for.
        cmd_res = self.resolution
        try:
            _w, _h = (int(x) for x in self.resolution.lower().split("x"))
            if _w > 1920 or _h > 1080:
                log.warning("source %s exceeds the Pi-4 H.264 encoder's 1080p max - "
                            "requesting 1920x1080 (a >1080p source can't be captured/"
                            "encoded on the Pi-4 2-lane CSI + M2M path; the stream "
                            "stays dark until the source drops to <=1080p)",
                            self.resolution)
                cmd_res = "1920x1080"
        except (ValueError, AttributeError):
            pass
        # Base capture/encoder flags follow pikvm/ustreamer's documented
        # TC358743-class recipe (README.md "Usage" section) — the C790 is
        # the same class of HDMI-to-CSI2 DV-timings bridge chip. --encoder
        # M2M-IMAGE is hardware JPEG encode for the plain /stream path
        # (kept alive as a fallback even in h264 mode); it is NOT the H.264
        # encoder — that's a separate, always-available GPU M2M path that
        # --h264-sink below turns on regardless of --encoder.
        #
        # --h264-sink-mode/-rm are required per pikvm/ustreamer docs/h264.md
        # so the shared-memory segment has sane permissions and doesn't
        # linger after ustreamer exits (a stale segment from a previous run
        # would otherwise block Janus from reading the new one).
        cmd = [
            "ustreamer",
            "--device",          self.device,
            "--format",          "UYVY",
            "--encoder",         "M2M-IMAGE",
            # ONE worker on the M2M path. M2M-IMAGE is the Pi's HARDWARE JPEG
            # encoder - a single physical block - so extra "workers" do not add
            # throughput, they just contend for it and hand back partially
            # encoded frames. Measured on the unit, same scene, same 3s window:
            #   workers=3 -> median frame 239 KB, 49 Mbit/s, heavy banding
            #   workers=1 -> median frame  73 KB, 25 Mbit/s, body pixel-clean
            # A clean frame of this scene is ~46 KB, so the extra ~190 KB at
            # workers=3 was pure corruption entropy - and it was also what
            # saturated the WiFi link and truncated frames in the browser.
            "--workers",          "1",
            # MORE BUFFERS THAN WORKERS - this is what fixes the torn/green
            # scanlines. ustreamer defaults to 5 (min(cores,4)+1); with 3
            # workers that leaves too little slack, so the driver recycles a
            # buffer while a worker is still encoding it and the JPEG comes out
            # with bands of mismatched data (which in dark scenes decodes green).
            # Proven on the unit: raw CSI frames were clean while ustreamer
            # output was badly banded; --buffers=8 made it pixel-clean with no
            # other change. ~1.8MB per buffer at 720p, ~4MB at 1080p.
            "--buffers",          "8",
            "--persistent",
            "--dv-timings",
            # 10, not 30. This drops up to N IDENTICAL frames before emitting
            # one, so on an idle desktop it decides how often the stream still
            # "ticks". At 30 a static screen could go ~0.6s+ between frames,
            # which starves the WebRTC receiver of anything to decode and makes a
            # healthy-but-idle session look frozen to any liveness check. 10 keeps
            # a few frames per second flowing on a still screen for negligible
            # bandwidth (identical frames compress to almost nothing), and does
            # not affect a screen that is actually changing.
            "--drop-same-frames", "10",
            "--resolution",       cmd_res,
            "--desired-fps",      str(_eff_fps),
            # JPEG quality for the MJPEG FALLBACK side of this h264-mode process.
            # Kept deliberately low, and this is not cosmetic - it is what stops a
            # death spiral. The CSI path must capture at the source resolution
            # (1080p here), and ustreamer cannot downscale it, so the only lever
            # on MJPEG's size is quality. Measured on the unit: at default quality
            # each /stream pull was 3.4-3.7 MB and 1080p MJPEG runs roughly
            # 30-50 Mbit/s, against a WiFi uplink measured as low as 30 Mbit/s.
            # So every fallback saturated the link 100%, which starved the input
            # WebSocket (the cursor sticking), stalled the stream, forced another
            # reconnect, and left no bandwidth for WebRTC to ever recover - 146
            # /stream reconnects in 30 minutes. H.264 at 2 Mbit/s fits this link
            # comfortably; MJPEG at full quality never can. Low quality keeps the
            # safety net from being worse than having no video at all.
            # Does NOT affect H.264 quality (that is --h264-bitrate below).
            "--quality",          str(min(self.quality, 30)),
            "--host",             STREAM_HOST,
            "--port",             str(self.port),
            # THE single biggest latency win found so far. ustreamer documents
            # this only as "Increase encoder performance on PiKVM V4", which is
            # easy to skip on a plain Pi 4B - but it is the same SoC family and
            # it applies. Measured on this unit, 1920x1080, identical scene:
            #   without --h264-boost : 25.0 fps, 40 ms between frames
            #   with    --h264-boost : 42.7-45.6 fps, 22 ms between frames
            # 1.8x the frame rate and the frame interval nearly halved, at no
            # thermal or power cost (37.9 C, throttled=0x0). Validated over a
            # sustained 26s capture: 1109 frames decoded with zero errors after
            # the expected start-of-capture PPS artifact, SPS/IDR still paired
            # 23/23 so late joiners can start.
            "--h264-boost",
            "--h264-sink",        sink_name,
            "--h264-sink-mode",   "660",
            "--h264-sink-rm",
            "--h264-bitrate",     str(self.bitrate),
            # ~0.5s keyframe interval (was ~1s). On a lossy link a dropped packet
            # corrupts the H.264 stream (the green bands) until the next keyframe
            # refreshes it, so a shorter interval halves how long that corruption
            # is visible. It also makes each keyframe smaller and the bitrate
            # smoother (fewer big bursts), which a marginal WiFi uplink tolerates
            # better. Never 0 (an invalid GOP breaks SPS/IDR pairing).
            "--h264-gop",         str(max(1, _eff_fps // 2)),
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(1.0)
            if self.process.poll() is not None:
                log.warning(
                    "ustreamer H.264 mode exited immediately (device=%s res=%s). "
                    "Likely causes: ustreamer binary lacks --h264-sink/M2M support "
                    "(needs the Janus-enabled build), C790 not yet connected, or "
                    "wrong /dev/videoN selected. Falling back to MJPEG.",
                    self.device, self.resolution,
                )
                self.process = None
                return self._fallback_or_fail()
            log.info("ustreamer H.264: %s %s %dfps -> sink=%s (Janus reads this for WebRTC)",
                      self.device, self.resolution, self.fps, sink_name)
            try:
                self._sync_janus_audio_cfg()   # write audio cfg BEFORE (re)starting Janus
            except Exception:
                pass  # audio is additive - never let it affect the video path's return value
            self._tune_janus_ice()             # offer tailscale0 only while the tailnet is up
            self._notify_janus_sink_ready()    # bounce Janus so it reads the fresh cfg + re-attaches
            return True
        except FileNotFoundError:
            log.error("ustreamer binary not found")
            return False
        except Exception as e:
            log.error("ustreamer H.264 start error: %s", e)
            return self._fallback_or_fail()

    def _tune_janus_ice(self) -> bool:
        """Offer the Tailscale interface as a WebRTC path only while the tailnet
        is actually UP. Returns True if the config changed.

        The problem this solves, both directions:
          - tailscale0 stays UP as an interface even when the tailnet is OFFLINE.
            Janus then advertises that address as an ICE candidate, every media
            packet sent to it fails, and the log fills with "only sent -1 bytes"
            while the browser waits for video that can never arrive.
          - But when the tailnet IS up (and especially when it negotiates a
            DIRECT path rather than a DERP relay), that candidate is exactly how
            a REMOTE operator gets low-latency H.264 instead of falling back to
            MJPEG. Blacklisting it permanently would throw that away.
        So decide per stream start instead of hardcoding either answer. Cheap:
        one short `tailscale status` call, and Janus is only restarted when the
        setting actually flips."""
        cfg = "/opt/janus/etc/janus/janus.jcfg"
        if not os.path.isfile(cfg):
            return False
        up = False
        try:
            r = subprocess.run(["tailscale", "status", "--json"],
                               capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                import json as _json
                up = _json.loads(r.stdout).get("BackendState") == "Running"
        except Exception:
            up = False
        want = 'ice_ignore_list = "vmnet"' if up else 'ice_ignore_list = "vmnet,tailscale0"'
        try:
            text = open(cfg).read()
            cur = re.search(r'ice_ignore_list\s*=\s*"[^"]*"', text)
            if cur and cur.group(0) == want:
                return False                      # already correct, leave Janus alone
            new = (re.sub(r'ice_ignore_list\s*=\s*"[^"]*"', want, text, count=1) if cur
                   else re.sub(r'(nat:\s*\{)', r'\1\n\t' + want, text, count=1))
            open(cfg, "w").write(new)
            log.info("Janus ICE: tailnet %s -> %s", "up" if up else "down", want)
            return True
        except Exception as e:
            log.warning("Could not tune Janus ICE list (non-fatal): %s", e)
            return False

    def _notify_janus_sink_ready(self):
        """Best-effort: if the Janus WebRTC gateway is running, bounce it so it
        re-attaches to the freshly (re)created h264 memsink.

        ustreamer's Janus plugin is the memsink CONSUMER; ustreamer is the
        producer. When ustreamer (re)starts - boot ordering, a settings change,
        or a watchdog restart - a Janus that came up first can be left reading a
        stale/gone sink and deliver no pixels to WebRTC clients even though ICE
        stays 'connected'. systemd ordering can't fix this (the unit is
        Type=simple, so 'started' does NOT mean the sink exists yet), so the
        producer notifies the consumer here. Guards make it safe:
          - no-op unless janus-webrtc.service is actually active (so it does
            nothing on this unit until Janus is installed), and
          - it only ever fires right as the sink is (re)created - a moment when
            WebRTC clients must re-attach anyway - so it never disrupts a
            healthy, steady stream.
        Fully swallowed; never affects the video path's own success."""
        active = False
        try:
            r = subprocess.run(["systemctl", "is-active", "janus-webrtc.service"],
                               capture_output=True, text=True, timeout=3)
            active = r.stdout.strip() == "active"
            if active:
                subprocess.run(["systemctl", "try-restart", "janus-webrtc.service"],
                               capture_output=True, timeout=5)
                log.info("Notified Janus of fresh h264 sink (try-restart janus-webrtc.service)")
        except Exception:
            log.debug("Janus sink-ready notify skipped", exc_info=True)
        # Remember whether there is actually a consumer for the sink and the
        # audio config we just wrote. Checked here rather than in status() so
        # the poll path stays fork-free.
        self._janus_active = active

    def _fallback_or_fail(self) -> bool:
        """H.264 launch failed. Fall back to MJPEG ONLY on a device that can
        actually produce it. A C790/CSI board emits UYVY, not MJPEG, so
        "falling back" there launched a second doomed process AND stuck
        self.mode='mjpeg' - after which every watchdog restart retried the
        broken MJPEG path forever, converting a momentary H.264 hiccup into a
        permanently dead stream. On CSI: leave mode alone and return False so
        the next restart retries H.264 once the transient clears."""
        if self.device_type(self.device) == "csi":
            self.h264_sink = None
            log.warning("H.264 start failed on CSI device %s; NOT falling back to "
                        "MJPEG (the board can't produce it) - will retry H.264",
                        self.device)
            return False
        self.mode = "mjpeg"
        self.h264_sink = None
        return self._start_ustreamer()

    def _start_ffmpeg(self) -> bool:
        """
        Launch ffmpeg MJPEG stream as a multipart HTTP stream on STREAM_PORT.
        ffmpeg → pipe → Python HTTP mini-server handles the actual serving.
        """
        if not shutil.which("ffmpeg"):
            log.error("Neither ustreamer nor ffmpeg found, no video stream")
            return False
        w, h = self.resolution.split("x")
        # ffmpeg captures from V4L2 and outputs raw MJPEG to stdout
        cmd = [
            "ffmpeg",
            "-f",         "v4l2",
            "-input_format", "mjpeg",
            "-video_size", f"{w}x{h}",
            "-framerate", str(self.fps),
            "-i",         self.device,
            "-q:v",       str(max(1, min(31, 31 - int(self.quality * 0.3)))),
            "-f",         "mjpeg",
            "pipe:1",
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(0.5)
            if self.process.poll() is not None:
                log.warning("ffmpeg exited immediately, trying mjpeg fallback format")
                # Retry without input_format=mjpeg (some cards output raw)
                cmd2 = [
                    "ffmpeg", "-f", "v4l2",
                    "-video_size", f"{w}x{h}", "-framerate", str(self.fps),
                    "-i", self.device, "-vf", "scale="+self.resolution,
                    "-q:v", "5", "-f", "mjpeg", "pipe:1",
                ]
                self.process = subprocess.Popen(
                    cmd2, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, close_fds=True,
                )
                time.sleep(0.5)
                if self.process.poll() is not None:
                    self.process = None
                    return False
            log.info("ffmpeg: %s %s %dfps → pipe", self.device, self.resolution, self.fps)
            # Start a simple MJPEG HTTP server in a thread
            threading.Thread(
                target=self._serve_ffmpeg_stream,
                daemon=True
            ).start()
            return True
        except Exception as e:
            log.error("ffmpeg start error: %s", e)
            return False

    def _serve_ffmpeg_stream(self):
        """
        Read MJPEG frames from ffmpeg stdout and serve them as
        multipart/x-mixed-replace on STREAM_PORT.
        """
        import socket
        import select as sel

        BOUNDARY = b"--frame"
        HEADER   = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            b"Cache-Control: no-cache, no-store\r\n"
            b"Connection: close\r\n\r\n"
        )

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((STREAM_HOST, self.port))
        except OSError as e:
            log.error("ffmpeg stream server bind failed: %s", e)
            return
        srv.listen(4)
        srv.settimeout(1.0)
        log.info("ffmpeg MJPEG server on %s:%d", STREAM_HOST, self.port)

        buf = b""

        def _read_frame(proc_stdout) -> bytes:
            nonlocal buf
            SOI = b"\xff\xd8"
            EOI = b"\xff\xd9"
            while True:
                chunk = proc_stdout.read(4096)
                if not chunk:
                    return b""
                buf += chunk
                s = buf.find(SOI)
                if s < 0:
                    buf = b""
                    continue
                buf = buf[s:]
                e = buf.find(EOI)
                if e < 0:
                    continue
                frame = buf[:e + 2]
                buf = buf[e + 2:]
                return frame

        clients = []
        while self.process and self.process.poll() is None:
            try:
                r, _, _ = sel.select([srv] + clients, [], [], 0.05)
                for s in r:
                    if s is srv:
                        try:
                            conn, _ = srv.accept()
                            # Drain HTTP request headers
                            conn.recv(4096)
                            conn.sendall(HEADER)
                            clients.append(conn)
                        except Exception:
                            pass
                    else:
                        try:
                            if s.recv(1, socket.MSG_PEEK) == b"":
                                clients.remove(s)
                                s.close()
                        except Exception:
                            if s in clients:
                                clients.remove(s)
                            try:
                                s.close()
                            except Exception:
                                pass
                if clients and self.process:
                    frame = _read_frame(self.process.stdout)
                    if not frame:
                        break
                    part = (BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode()
                            + b"\r\n\r\n" + frame + b"\r\n")
                    dead = []
                    for c in clients:
                        try:
                            c.sendall(part)
                        except Exception:
                            dead.append(c)
                    for d in dead:
                        if d in clients:
                            clients.remove(d)
                        try:
                            d.close()
                        except Exception:
                            pass
            except Exception as e:
                log.debug("ffmpeg stream loop: %s", e)
                break
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        srv.close()

    # Status & watchdog

    def is_running(self) -> bool:
        # Also return True if ustreamer.service manages the stream
        if getattr(self, '_running', False):
            return True
        return self.process is not None and self.process.poll() is None

    def status(self) -> dict:
        streamer = "ustreamer" if shutil.which("ustreamer") else \
                   ("ffmpeg" if shutil.which("ffmpeg") else "none")
        return {
            "running":    self.is_running(),
            "device":     self.device,
            "device_type": self.device_type(self.device) if self.device else None,
            "resolution": self.resolution,
            "bitrate": self.bitrate,
            "fps":        self.fps,
            "quality":    self.quality,
            "mode":       self.mode,
            "port":       self.port,
            "streamer":   streamer,
            "devices":    self.detect_devices(),
            "h264_sink":  self.h264_sink,
            # Audio only counts as configured when something can actually
            # consume it: the h264 sink is up, a Janus plugin config really got
            # the block written, and Janus itself was running when we notified
            # it. Reporting the bare `arecord` probe made the UI tick audio
            # green on a unit where nothing was reading that card at all.
            "audio_device": (getattr(self, "_audio_active", None)
                             if (self.h264_sink
                                 and getattr(self, "_audio_cfg_written", False)
                                 and getattr(self, "_janus_active", False))
                             else None),
        }

    def start_watchdog(self):
        """Background thread that auto-restarts a dead stream, with backoff."""
        # Idempotent: don't spawn a second watchdog if one is already running
        # (start_watchdog is now called unconditionally at boot).
        if getattr(self, "_mon_thr", None) and self._mon_thr.is_alive():
            return
        def _watch():
            fails = 0
            res_check = 0      # throttles the live-timings comparison
            res_miss = 0       # consecutive resolution mismatches (debounce)
            while True:
                # Exponential backoff on consecutive failures so a persistently
                # un-launchable stream doesn't restart-storm every 5 s forever
                # (5, 10, 20, 40, capped 60). Resets to 5 s once healthy.
                time.sleep(min(5 * (2 ** fails), 60))
                needs_restart = False
                reason = ""
                with self._lock:
                    if self.device and not self.is_running():
                        needs_restart = True
                        reason = "process not running"
                # Frame-liveness for CSI. is_running() is process-liveness only,
                # so a "live but dark" stream is invisible to the death check
                # above: ustreamer runs with --persistent and stays alive across
                # a source change (e.g. 1080p50 -> 720p60), but keeps asking for
                # the OLD --resolution the source no longer sends, so zero frames
                # decode and the UI shows "No Signal" while a signal is clearly
                # present. Nothing else recovers this (mb-hdmi-watch is a no-op
                # while the capture node is busy). Every ~15 s, compare the live
                # locked timings to what we launched with; on a CONFIRMED width/
                # height change, restart() re-detects and relaunches. Guards:
                #  - only for CSI (the only path that follows the live signal),
                #  - detect_csi_timings returns None on no signal -> never touch
                #    a stream just because the source is momentarily asleep,
                #  - require two consecutive mismatches so one glitchy query
                #    can't bounce a healthy stream.
                # --query-dv-timings is a read-only query ioctl, safe to issue
                # while ustreamer holds the node.
                if not needs_restart and self.is_running():
                    res_check += 1
                    if res_check >= 3:
                        res_check = 0
                        try:
                            is_csi = bool(self.device) and self.device_type(self.device) == "csi"
                        except Exception:
                            is_csi = False
                        if is_csi:
                            det = self.detect_csi_timings(self.device)
                            if det and (det[0] > 1920 or det[1] > 1080):
                                # A >1080p source can't be captured/encoded on the
                                # Pi-4 2-lane CSI + M2M path, so restart() cannot
                                # help - it would just re-detect the same too-big
                                # timings and bounce forever. Settle on a stable
                                # dark stream instead of a ~30 s restart storm.
                                # (Belt-and-braces: self.resolution is no longer
                                # clamped, so live == self.resolution here anyway.)
                                res_miss = 0
                            elif det:
                                live = "%dx%d" % (det[0], det[1])
                                if live != self.resolution:
                                    res_miss += 1
                                    if res_miss >= 2:
                                        needs_restart = True
                                        reason = "source resolution changed %s -> %s" % (
                                            self.resolution, live)
                                else:
                                    res_miss = 0
                            else:
                                res_miss = 0   # no signal -> leave the live stream alone
                if not needs_restart:
                    fails = 0
                    continue
                log.info("Stream restart (%s), attempt %d...", reason, fails + 1)
                self.restart()
                time.sleep(2)   # let it come up before judging
                fails = 0 if self.is_running() else min(fails + 1, 4)
                res_check = 0
                res_miss = 0
        t = threading.Thread(target=_watch, daemon=True, name="mb-video-watchdog")
        t.start()
        self._mon_thr = t
