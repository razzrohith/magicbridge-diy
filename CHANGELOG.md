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
