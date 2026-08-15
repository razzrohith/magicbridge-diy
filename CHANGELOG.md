# MagicBridge release notes

What the operator sees in Settings, System, Software Update. Written for the
person USING the device, not for developers: no commit hashes, no file paths, no
internal function names.

Versioning, matching how the updater classifies an update:
- **Major** (1.x -> 2.0): a full upgrade, i.e. structural changes that reinstall
  services or boot configuration.
- **Minor** (1.1 -> 1.2): an incremental update, i.e. code and fixes only.

Keep the newest release at the top. Each bullet is one line an owner can act on
or understand.

## 1.3.0

- Fixes the fault behind "it worked for a moment and then went strange". The
  video connection was being torn down and rebuilt every 20 seconds whenever the
  first picture was slow to arrive, and each rebuild sent another full frame down
  the same busy connection that made it slow, so it could never settle. The page
  now waits when video is arriving slowly instead of starting over, and asks the
  device for a fresh picture when it needs one.
- Roughly half the bandwidth for the same picture. Video now sends a full frame
  every two seconds instead of every second, measured 4.18 down to 2.29 Mbit/s,
  which is what brings it inside what a typical remote connection carries.
- Control is no longer dragged down by the picture. Moving things on the target
  screen used to push video far past what a remote connection can carry, and the
  keyboard and mouse queued up behind it. Measured on a real remote link, worst
  case control delay dropped from about 940 ms to about 27 ms, and video now
  stays under the connection's capacity instead of exceeding it four times over.
- A new picture quality setting actually controls bandwidth, unlike the bitrate
  setting which this hardware ignores. Lower quality means a slightly coarser
  picture and much steadier control on a slow connection.
- Fast mouse movement no longer builds a backlog. Moves were being queued into
  the connection faster than it could send them, so the pointer ran behind and
  clicks landed where it used to be. Movement is now held back while the
  connection catches up, and the pointer jumps straight to the right place.
- A working session is no longer dropped just for being slow, and if the
  connection does drop, control comes back in at most 5 seconds instead of 15.
- Known limitation, now documented: the video bitrate setting has no effect on
  this hardware. The Pi's built in encoder ignores it. Asking for 350 kbps and
  asking for 10000 kbps produce the same picture and the same bandwidth. Two
  fixes for it were built and measured and neither worked, so the setting is
  left in place but does nothing; quality is controlled by the frame interval
  instead.

## 1.2.0

- Fixes two ways a brand new unit could come up with working keyboard and mouse
  but a permanently black screen. Affected freshly flashed units and fresh
  installs only; an already working unit was never affected.
- The device keeps its own unique monitor identity through updates. An update
  could previously overwrite it with a shared one, which would have made
  separate units look identical to the computers they control.
- Installing no longer aborts on a slow or offline package mirror, so a unit on
  a poor connection can still be updated.
- Setup no longer leaves the device stranded if nobody completes the WiFi setup
  page and the device gives up waiting.
- A hand-edited or corrupted settings file can no longer stop the device
  starting; it falls back to safe defaults and says so in the log.
- Sharper picture on the default video path, which was quietly running with
  settings that had never been tested.

## 1.1.0

- Video no longer opens a second, heavyweight stream on every page load. That
  hidden stream was using about twice the available bandwidth on a remote
  connection, which is what caused the garbled picture, the long slow period
  after loading, and the laggy mouse.
- Video recovers on its own within seconds when the target computer reboots or
  its screen signal returns, instead of staying broken for about half a minute.
- Ctrl+V now reaches the target computer. It was being captured by this page and
  never forwarded. Ctrl+Shift+V opens the built in paste box.
- Scrolling direction is inverted, and the local mouse pointer is hidden while
  you are controlling, so only the target's own pointer is visible.
- Sharper picture for the same bandwidth: video memory on the device now matches
  the settings a commercial KVM uses, and the desktop graphics driver that was
  competing with the video encoder is disabled.
- Control stays responsive while video is busy: keyboard and mouse packets are
  now prioritised ahead of video on WiFi. Measured worst case latency dropped
  from about 540 ms to about 36 ms.
- WiFi networks that use WPA3, or that hide their name, can now be joined. They
  previously failed with a message that blamed the password.
- Setup now explains the browser's certificate warning instead of leaving it
  looking like something is broken.
- Privacy: the device no longer writes your WiFi network name to the memory
  card, and connection history is kept in memory only.
- Fixed for people building their own images: distributed images no longer
  contain the builder's password, and the image check now verifies this instead
  of always passing.

## 1.0.0

- First release.
