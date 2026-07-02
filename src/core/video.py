#!/usr/bin/env python3
"""
MagicBridge Video Capture Manager

Manages the MJPEG/H264 stream from USB HDMI capture cards.
Primary streamer: ustreamer (apt install ustreamer)
Fallback:         ffmpeg (apt install ffmpeg)

Compatible capture cards (all UVC/V4L2):
  - Generic MS2109 USB HDMI capture cards
  - Elgato Cam Link 4K (UVC-compatible on Linux)
  - UGREEN USB capture cards
  - Any V4L2 VIDEO_CAPTURE device
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


class VideoManager:
    """Start/stop/restart the MJPEG video stream from a V4L2 capture device."""

    def __init__(self):
        self.process    = None
        self.device     = None
        self.resolution = "1920x1080"
        self.fps        = 30
        self.quality    = 90      # MJPEG quality 1-100
        self.mode       = "mjpeg" # "mjpeg" | "h264" (h264 needs ffmpeg)
        self.port       = STREAM_PORT
        self._lock      = threading.Lock()
        self._mon_thr   = None    # watchdog thread

    # Device discovery

    def detect_devices(self) -> list:
        """Return list of V4L2 VIDEO_CAPTURE devices with metadata."""
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
        return devices

    def get_best_device(self) -> str:
        """Return best V4L2 capture device, preferring USB over internal Pi devices."""
        devs = self.detect_devices()
        if not devs:
            return None
        # Prefer USB devices (capture cards). Pi internal devices use "platform:" bus
        usb = [d for d in devs if d["bus"].startswith("usb")]
        if usb:
            log.info("Auto-selected USB capture device: %s (%s)", usb[0]["device"], usb[0]["name"])
            return usb[0]["device"]
        log.info("No USB capture device found, using: %s", devs[0]["device"])
        return devs[0]["device"]

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
              fps: int = None, quality: int = None, mode: str = None) -> bool:
        """Start streaming. Returns True on success. Safe to call repeatedly.

        Resolution selection priority:
          1. Explicit `resolution` argument (e.g. from stealth panel override)
          2. Auto-detect highest native MJPEG resolution from capture card
          3. Self.resolution fallback (default 1280x720)
        """
        with self._lock:
            if device:              self.device     = device
            if resolution:          self.resolution = resolution
            if fps is not None:     self.fps        = int(fps)
            if quality is not None: self.quality    = int(quality)
            if mode:                self.mode       = mode

            if not self.device:
                self.device = self.get_best_device()
            if not self.device:
                log.warning("No V4L2 capture device found")
                return False

            # Auto-detect best native MJPEG resolution when not explicitly set.
            # This lets MagicBridge adapt to any laptop/screen resolution automatically:
            # 14" 1080p, 16" 1440p, 4K, etc. Just uses whatever the capture card signals.
            if not resolution:
                best = self.get_best_mjpeg_resolution(self.device)
                if best and best != self.resolution:
                    log.info("Auto-resolution: %s -> %s (native MJPEG from capture card)",
                             self.resolution, best)
                    self.resolution = best

            self._stop_locked()

            if shutil.which("ustreamer"):
                ok = self._start_ustreamer()
            else:
                log.info("ustreamer not found, using ffmpeg fallback")
                ok = self._start_ffmpeg()

            return ok

    def stop(self):
        with self._lock:
            self._stop_locked()

    def restart(self):
        with self._lock:
            self._stop_locked()
        time.sleep(1)
        with self._lock:
            if self.device:
                if shutil.which("ustreamer"):
                    self._start_ustreamer()
                else:
                    self._start_ffmpeg()

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
            "resolution": self.resolution,
            "fps":        self.fps,
            "quality":    self.quality,
            "mode":       self.mode,
            "port":       self.port,
            "streamer":   streamer,
            "devices":    self.detect_devices(),
        }

    def start_watchdog(self):
        """Background thread that auto-restarts a dead stream every 5 s."""
        def _watch():
            while True:
                time.sleep(5)
                needs_restart = False
                with self._lock:
                    if self.device and not self.is_running():
                        log.info("Stream died, restarting...")
                        needs_restart = True
                if needs_restart:
                    self.restart()
        t = threading.Thread(target=_watch, daemon=True, name="mb-video-watchdog")
        t.start()
        self._mon_thr = t
