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

## 1.7.8

- Added a "Max" picture quality above "Sharp", for the crispest image your link
  can carry. On the low-latency mode it uses a little more bandwidth for a
  cleaner picture, which helps most on dark screens.

## 1.8.2

- Cleaned up the Update screen to look like a normal app updater: a simple
  status ("You're up to date" or "Update available"), the version, what's new,
  and a single Update button. Removed the technical bits (commit counts, file
  counts, build IDs) that a customer does not need to see.

## 1.8.1

- Paste popup refinements: when you press Paste it now stays in the middle and
  just shrinks (instead of sliding down), so it is out of your way but still
  centered. And clicking anywhere on the screen stops the paste right away, so
  you can interrupt a long paste the moment you want to. Mouse movement alone
  does not interrupt it, only a click.

## 1.8.0

- Redesigned Paste in fullscreen. Instead of a bar at the bottom, it now opens
  as a small popup in the middle of the screen with a gentle pop, with Paste,
  Cancel, and Close. When you press Paste, the popup slides down a bit and
  shrinks so you can watch the text appear on the other computer, then returns
  to the middle for the next one.

## 1.7.9

- Fixed the custom name not resolving on any device. A privacy tightening in an
  earlier version accidentally switched off the exact mechanism the custom name
  uses to announce itself on your WiFi. It now announces again. If you set a
  name and it stopped working, update and it comes back.

## 1.7.8

- Made the custom name honest about where it works: the ".local" name only
  reaches the box from a device on the same WiFi. Away from home or over
  Tailscale, use the device's IP address. The old message wrongly implied it
  worked everywhere.

## 1.7.7

- Shortened the password-change screens on the control page and the admin page
  so they read cleaner and get to the point.

## 1.7.6

- Trimmed the wordy help text in Settings so the panels read cleaner and more
  professional.

## 1.7.5

- When a new version is available, opening or refreshing the page now shows a
  small "update available" note in the top-right corner for a few seconds, then
  it disappears on its own. Tap it to go straight to the update screen. The
  device checks for this quietly in the background, so it costs nothing while you
  are watching the screen.

## 1.7.4

- The jiggler now waits a full 2 minutes of you being idle before it takes back
  over, so it gives you more room during longer pauses in your work.

## 1.7.3

- The mouse jiggler now stays out of your way. While you are controlling the
  other computer it pauses on its own, and stays paused as long as you keep
  working (short pauses to read or think are fine). It resumes by itself once you
  step away. Any on/off schedule or timer you set keeps running in the
  background, so the jiggler picks up right where the clock left it. The jiggler
  panel shows "Paused, you're using it" while you are active.

## 1.7.2

- Much better on a phone. You can now tap the screen to click, drag two fingers
  to scroll, and reach the fullscreen controls by tapping the top edge. Buttons
  are larger and easier to tap, and stray phone gestures no longer interfere.
- Clearer first-time setup. The setup page no longer says "complete" before it
  has actually connected, warns you about the normal browser "not private"
  message, shows your device's address in bright, readable text, and tells you
  what to do if the WiFi password was mistyped.
- Friendlier screens. The "no picture" message now explains what to check in
  plain words, a short welcome tip appears the first time you open the control
  page, and the buttons read more consistently.
- Easier to use with assistive technology. Connection changes and messages are
  announced to screen readers, controls carry proper labels, the keyboard
  shortcuts can be used without a mouse, and animations respect the system
  "reduce motion" setting.
- Fixes. Pressing Send twice no longer types your text twice, a broken arrow in
  the Network settings is fixed, the small status dots on the side menu stay in
  their corner, and the wake-a-computer feature now accepts the address in any
  common format and helps you find it.

## 1.7.1

- The admin page can now use two-factor sign-in. If you have already turned on
  the authenticator code on the main page, the admin page asks for the same code
  too. If you have not turned it on, nothing changes.
- A closer match to a real wireless receiver for owners who want it, off by
  default and safe to try: it falls back to the proven setup on its own if the
  other computer does not accept it.
- Small honesty and safety cleanups under the hood.

## 1.7.0

- Stronger privacy on your own network. The device no longer carries its product
  name in its security certificate or in any network name it publishes, so
  nothing on your Wi-Fi can tell what it is. Devices already in the field refresh
  their certificate automatically with this update.
- The mouse and keyboard it presents to the other computer now match a real
  wireless receiver even more closely. This takes effect the next time the device
  restarts, and never interrupts the other computer.
- The admin page now locks out repeated wrong-password attempts, and its sign-in
  screen no longer hints at what the device is.
- Updates are safer. If an update file were ever bad, the device now rolls back
  on its own instead of going offline, and a software-only update installs in
  seconds.
- Housekeeping logs are kept in memory only and never written to the card.

## 1.6.0

- New jiggler style, "Human". Instead of a tiny nudge, the pointer makes long,
  natural moves across the screen: it speeds up and slows down, follows a
  slightly curved path, and even overshoots a little and corrects, the way a
  real hand does. It roams around rather than returning to the same spot, and
  never traces a repeating pattern. Pick it under the Mouse Jiggler settings if
  you want the movement to look like someone is actually using the mouse.
- Software-only updates now apply as a quick update instead of a full reinstall,
  so most updates finish in seconds.

## 1.5.1

- Tidied the Custom name box: the "Save name" button no longer wraps onto two
  lines, and it sits neatly next to Clear.

## 1.5.0

- You can now give the device your own name to reach it by. In Settings,
  Network, under "Custom name", type something like "studio" and from then on
  open it at studio.local on your WiFi. Leave it empty to keep the device
  unnamed, which is the default.
- The name is only visible on your own WiFi. The computer it controls never sees
  it, and to your router the device still looks like an ordinary PC. The device
  gently blocks names that would give away what it is.

## 1.4.2

- The paste bar can now be closed. Use its Close button, press the Escape key
  while typing in it, or click back on the screen and it goes away. Before, it
  could be opened from the top bar but not dismissed.

## 1.4.1

- In fullscreen, the control bar at the top no longer pops up by accident. It
  appears only when you move to the top center of the screen and pause for a
  moment, and it hides itself again on its own, so it stops covering what you are
  working on. Moving near a corner or along an edge no longer triggers it.
- The paste bar at the bottom no longer appears on its own. It shows only when
  you press Paste, keeping the bottom of the screen clear.

## 1.4.0

- The mouse jiggler can now switch itself off. Set a time ("stop at 4 pm") or a
  duration ("stop in 1 hour"), with one-tap presets for 15 minutes up to 8
  hours. The panel shows exactly when it will stop and counts down to it.
- Times use the device's own clock, and the panel says which timezone that is so
  there is no guessing. Asking for a time that has already passed today means
  tomorrow.
- The schedule survives a restart or a power cut. If the device was off when the
  stop time passed, it starts with the jiggler already off rather than carrying
  on past the time you set.

## 1.3.2

- Shipping fix: freshly set-up units now build the video engine with the
  bandwidth ceiling that keeps remote control responsive. Without it a new unit
  would have had the old problem back (heavy video on movement, laggy control)
  the moment its owner switched to the low-latency video mode.
- Remote video recovers on its own after a power-on. If the device booted before
  its secure remote link was ready, remote video used to stay black until you
  reopened the stream settings; it now reconnects by itself within about half a
  minute.
- The device now uses its disguised network identity from the very first WiFi
  connection during setup, instead of briefly showing its real one. This only
  concerns your own network; the computer it controls never sees it either way.

## 1.3.1

- The Mouse Cursor buttons now do something while you are controlling. Before,
  every style looked the same during control. Now Crosshair and Dot stay on
  screen as an aiming aid, while Arrow and Hidden show only the target's own
  pointer. Your choice is also remembered between sessions.
- The monitor Serial shown under "How the target sees it" now displays the real
  per-unit serial the target reads, instead of a dash. Nothing the target sees
  changed; the panel was simply reading the wrong place. The USB Serial now says
  "none" with an explanation, because a real wireless receiver has none and
  inventing one would be the giveaway.
- The device now reports a 23.8 inch screen, matching the monitor it imitates,
  instead of a 27 inch one. This only affects newly set-up units; a running unit
  is unchanged until it is next set up fresh.

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
- The heavyweight backup video path now switches itself on only when the device
  actually needs it, instead of being armed on every unit. A unit running normal
  video never opens it, so it cannot flood a slow connection; a unit whose normal
  video genuinely cannot start turns it on by itself and still shows a picture
  rather than a black screen. It also switches back off on its own once normal
  video works again.
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
