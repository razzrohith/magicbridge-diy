#!/bin/bash
# ============================================================
#  MagicBridge USB HID Gadget Setup
#  Creates /dev/hidg0 (keyboard) and /dev/hidg1 (mouse)
#
#  Reads identity from /etc/magicbridge/config.json if present.
#  Falls back to Logitech K120 defaults.
#  Run by mb-gadget.service at boot, before magicbridge.service.
# ============================================================
set -e

GADGET_DIR="/sys/kernel/config/usb_gadget/g1"
CONFIG_FILE="/etc/magicbridge/config.json"

# --rebuild forces a full teardown + recreate even if the gadget is already
# bound (used when the USB mouse mode changes: the mouse HID descriptor differs
# between relative and absolute, so the functions must be rebuilt, not skipped).
FORCE_REBUILD=0
[[ "${1:-}" == "--rebuild" ]] && FORCE_REBUILD=1

# Load identity from config.json. Defaults below are only used if
# config.json is missing or unreadable; a real combo-receiver identity
# (Logitech Unifying Receiver) rather than a keyboard-only model, since
# this gadget always exposes both a keyboard and a mouse HID interface.
# A real Unifying Receiver reports no serial number (iSerial=0) and
# exposes a 3rd idle vendor HID interface alongside keyboard+mouse, so
# those are the defaults here too.
VID="0x046d"
PID="0xc52b"
MFR="Logitech"
PROD="USB Receiver"
SER=""
EXTRA_IFACE="true"
MOUSE_MODE="relative"   # relative (boot mouse, stealthiest) | absolute (pointer)
BCD_USB="0x0200"
BCD_DEV="0x0100"

if [[ -f "$CONFIG_FILE" ]]; then
    _py() { python3 -c "
import json,sys
try:
    c=json.load(open('$CONFIG_FILE'))
    u=c.get('usb',{})
    print(u.get('$1','$2'))
except: print('$2')
" 2>/dev/null || echo "$2"; }
    VID=$(_py idVendor  "0x046d")
    PID=$(_py idProduct "0xc52b")
    MFR=$(_py manufacturer "Logitech")
    PROD=$(_py product "USB Receiver")
    SER=$(_py serial "")
    EXTRA_IFACE=$(_py extra_iface "true")
    MOUSE_MODE=$(_py mouse_mode "relative")
fi
MOUSE_MODE=$(echo "$MOUSE_MODE" | tr '[:upper:]' '[:lower:]')
# Normalize case (Python may print True/False) so the comparison below
# works regardless of how the value ended up stored in config.json.
EXTRA_IFACE=$(echo "$EXTRA_IFACE" | tr '[:upper:]' '[:lower:]')

# Load kernel modules
modprobe libcomposite 2>/dev/null || { echo "mb-gadget: WARNING libcomposite not loaded"; true; }
modprobe dwc2         2>/dev/null || true
sleep 0.5

# Mount configfs if needed
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config 2>/dev/null || true
fi

# Skip if already bound (unless --rebuild forces a fresh build for a mode change)
if [[ -d "$GADGET_DIR" ]]; then
    UDC_NOW=$(cat "$GADGET_DIR/UDC" 2>/dev/null | tr -d '[:space:]')
    if [[ -n "$UDC_NOW" && "$FORCE_REBUILD" == "0" ]]; then
        echo "mb-gadget: already bound to '$UDC_NOW', skipping"
        exit 0
    fi
    # Unbind first if bound (--rebuild path), then remove and recreate cleanly.
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/hid.keyboard" \
          "$GADGET_DIR/configs/c.1/hid.mouse"    \
          "$GADGET_DIR/configs/c.1/hid.aux"       2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.keyboard"   2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.mouse"      2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.aux"        2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1"              2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409"            2>/dev/null || true
    rmdir "$GADGET_DIR"                          2>/dev/null || true
fi

# Create gadget
mkdir -p "$GADGET_DIR"

# USB IDs
echo "$VID"    > "$GADGET_DIR/idVendor"
echo "$PID"    > "$GADGET_DIR/idProduct"
echo "$BCD_DEV" > "$GADGET_DIR/bcdDevice"
echo "$BCD_USB" > "$GADGET_DIR/bcdUSB"
echo "0x00"    > "$GADGET_DIR/bDeviceClass"
echo "0x00"    > "$GADGET_DIR/bDeviceSubClass"
echo "0x00"    > "$GADGET_DIR/bDeviceProtocol"

# Cap negotiated speed to full-speed (12 Mbps). Left unset, dwc2/configfs
# defaults this to its max ceiling (shows as "super-speed-plus" even though
# the Pi 4's OTG controller physically tops out at high-speed) and the host
# negotiates high-speed. Real wireless keyboard/mouse combo receivers -
# including the Logitech Unifying Receiver this gadget impersonates -
# enumerate at full-speed, not high-speed: confirmed via kernel log capture
# ("new full-speed USB device") on real hardware. A HID gadget answering at
# high-speed is a mismatch a curious target could notice even without any
# active probing tool. Must be written before UDC bind - the kernel rejects
# changing it on an already-bound gadget.
echo "full-speed" > "$GADGET_DIR/max_speed" 2>/dev/null || echo "mb-gadget: WARNING could not set max_speed, continuing at controller default"

# Strings (manufacturer / product / serial)
mkdir -p "$GADGET_DIR/strings/0x409"
printf '%s' "$MFR"  > "$GADGET_DIR/strings/0x409/manufacturer"
printf '%s' "$PROD" > "$GADGET_DIR/strings/0x409/product"
printf '%s' "$SER"  > "$GADGET_DIR/strings/0x409/serialnumber"

# Configuration descriptor
mkdir -p "$GADGET_DIR/configs/c.1/strings/0x409"
echo "Config 1" > "$GADGET_DIR/configs/c.1/strings/0x409/configuration"
echo "250"      > "$GADGET_DIR/configs/c.1/MaxPower"   # 250 mA

# Keyboard HID function
mkdir -p "$GADGET_DIR/functions/hid.keyboard"
echo "1" > "$GADGET_DIR/functions/hid.keyboard/protocol"    # 1 = keyboard
echo "1" > "$GADGET_DIR/functions/hid.keyboard/subclass"    # 1 = boot interface
echo "8" > "$GADGET_DIR/functions/hid.keyboard/report_length"

# Keyboard HID descriptor (63 bytes, max keycode 0x73 = F24):
#   modifier byte (8 bits) + reserved byte + 6 key slots
#   + LED output report
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x01\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x01\x95\x06\x75\x08\x15\x00\x25\x73\x05\x07\x19\x00\x29\x73\x81\x00\xc0' \
    > "$GADGET_DIR/functions/hid.keyboard/report_desc"

ln -sf "$GADGET_DIR/functions/hid.keyboard" \
       "$GADGET_DIR/configs/c.1/hid.keyboard"

# Mouse HID function - relative (boot mouse, default/stealthiest) or absolute
# (pointer) per MOUSE_MODE. The two use DIFFERENT report descriptors and report
# lengths, so hid.py must be told which one is live (set_absolute) or the target
# will ignore the reports.
mkdir -p "$GADGET_DIR/functions/hid.mouse"
if [[ "$MOUSE_MODE" == "absolute" ]]; then
    # Absolute pointer: 3 buttons + 5 pad + 16-bit X + 16-bit Y (0..32767) +
    # signed wheel = 6-byte report. NOT a boot mouse (protocol/subclass 0): a
    # real Logitech receiver mouse is relative, so absolute is an opt-in,
    # slightly-less-stealthy identity (see CLAUDE.md anonymity note).
    echo "0" > "$GADGET_DIR/functions/hid.mouse/protocol"
    echo "0" > "$GADGET_DIR/functions/hid.mouse/subclass"
    echo "6" > "$GADGET_DIR/functions/hid.mouse/report_length"
    # Includes PHYSICAL_MIN/MAX (0x36/0x46) alongside LOGICAL_MIN/MAX, matching
    # the reference absolute-pointer descriptor Windows maps most reliably.
    printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x36\x00\x00\x46\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' \
        > "$GADGET_DIR/functions/hid.mouse/report_desc"
    echo "mb-gadget: mouse mode = ABSOLUTE (6-byte pointer report)"
else
    # Relative boot mouse: 3 buttons + 5 pad + signed X + signed Y + signed
    # wheel = 4-byte report. Matches a real receiver mouse.
    echo "2" > "$GADGET_DIR/functions/hid.mouse/protocol"     # 2 = mouse
    echo "1" > "$GADGET_DIR/functions/hid.mouse/subclass"     # 1 = boot interface
    echo "4" > "$GADGET_DIR/functions/hid.mouse/report_length"
    printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x75\x01\x95\x03\x81\x02\x75\x05\x95\x01\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
        > "$GADGET_DIR/functions/hid.mouse/report_desc"
    echo "mb-gadget: mouse mode = RELATIVE (4-byte boot-mouse report)"
fi

ln -sf "$GADGET_DIR/functions/hid.mouse" \
       "$GADGET_DIR/configs/c.1/hid.mouse"

# Optional 3rd (idle) vendor HID interface. Real Logitech Unifying
# Receivers expose 3 USB interfaces, not 2 - this one carries no traffic
# from our code (matches how a plain keyboard/mouse setup, without
# Logitech's own software installed, never talks to it either) and is
# purely for interface-count realism. Best-effort: if anything here
# fails, the keyboard/mouse functions above are already bound and this
# is skipped without affecting them.
if [[ "$EXTRA_IFACE" == "true" ]]; then
    (
        set -e
        mkdir -p "$GADGET_DIR/functions/hid.aux"
        echo "0" > "$GADGET_DIR/functions/hid.aux/protocol"       # 0 = none (non-boot vendor iface)
        echo "0" > "$GADGET_DIR/functions/hid.aux/subclass"       # 0 = non-boot
        echo "7" > "$GADGET_DIR/functions/hid.aux/report_length"  # 1 report-id byte + 6 data bytes
        printf '\x06\x00\xff\x09\x01\xa1\x01\x85\x10\x75\x08\x95\x06\x15\x00\x26\xff\x00\x09\x01\x81\x02\x09\x01\x91\x02\xc0' \
            > "$GADGET_DIR/functions/hid.aux/report_desc"
        ln -sf "$GADGET_DIR/functions/hid.aux" "$GADGET_DIR/configs/c.1/hid.aux"
    ) || echo "mb-gadget: WARNING could not create aux interface, continuing with keyboard+mouse only"
fi

# Bind to USB Device Controller
UDC=$(ls /sys/class/udc 2>/dev/null | head -1)
if [[ -n "$UDC" ]]; then
    echo "$UDC" > "$GADGET_DIR/UDC"
    echo "mb-gadget: bound to $UDC"
    echo "mb-gadget: /dev/hidg0 = keyboard    /dev/hidg1 = mouse"
    if [[ -e "$GADGET_DIR/functions/hid.aux" ]]; then
        echo "mb-gadget: /dev/hidg2 = aux (idle vendor interface)"
    fi
    echo "mb-gadget: USB identity: '$MFR' '$PROD' (VID=$VID PID=$PID, serial=${SER:-<none>})"
else
    echo "mb-gadget: WARNING, no UDC found"
    echo "  Ensure the Pi USB-C OTG port is connected to the target computer."
    echo "  Check that dtoverlay=dwc2 is in /boot/firmware/config.txt"
    exit 1
fi
