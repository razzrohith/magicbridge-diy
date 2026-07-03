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

# Load identity from config.json. Defaults below are only used if
# config.json is missing or unreadable; a real combo-receiver identity
# (Logitech Unifying Receiver) rather than a keyboard-only model, since
# this gadget always exposes both a keyboard and a mouse HID interface.
VID="0x046d"
PID="0xc52b"
MFR="Logitech"
PROD="USB Receiver"
SER="12AB34CD"
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
    SER=$(_py serial "12AB34CD")
fi

# Load kernel modules
modprobe libcomposite 2>/dev/null || { echo "mb-gadget: WARNING libcomposite not loaded"; true; }
modprobe dwc2         2>/dev/null || true
sleep 0.5

# Mount configfs if needed
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config 2>/dev/null || true
fi

# Skip if already bound
if [[ -d "$GADGET_DIR" ]]; then
    UDC_NOW=$(cat "$GADGET_DIR/UDC" 2>/dev/null | tr -d '[:space:]')
    if [[ -n "$UDC_NOW" ]]; then
        echo "mb-gadget: already bound to '$UDC_NOW', skipping"
        exit 0
    fi
    # Gadget dir exists but unbound. Remove and recreate cleanly
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/hid.keyboard" \
          "$GADGET_DIR/configs/c.1/hid.mouse"    2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.keyboard"   2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.mouse"      2>/dev/null || true
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

# Mouse HID function
mkdir -p "$GADGET_DIR/functions/hid.mouse"
echo "2" > "$GADGET_DIR/functions/hid.mouse/protocol"     # 2 = mouse
echo "1" > "$GADGET_DIR/functions/hid.mouse/subclass"     # 1 = boot interface
echo "4" > "$GADGET_DIR/functions/hid.mouse/report_length"

# Mouse HID descriptor (52 bytes):
#   3 button bits + 5 padding bits + X (signed) + Y (signed) + Wheel (signed)
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x75\x01\x95\x03\x81\x02\x75\x05\x95\x01\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
    > "$GADGET_DIR/functions/hid.mouse/report_desc"

ln -sf "$GADGET_DIR/functions/hid.mouse" \
       "$GADGET_DIR/configs/c.1/hid.mouse"

# Bind to USB Device Controller
UDC=$(ls /sys/class/udc 2>/dev/null | head -1)
if [[ -n "$UDC" ]]; then
    echo "$UDC" > "$GADGET_DIR/UDC"
    echo "mb-gadget: bound to $UDC"
    echo "mb-gadget: /dev/hidg0 = keyboard    /dev/hidg1 = mouse"
    echo "mb-gadget: USB identity: '$MFR' '$PROD' (VID=$VID PID=$PID)"
else
    echo "mb-gadget: WARNING, no UDC found"
    echo "  Ensure the Pi USB-C OTG port is connected to the target computer."
    echo "  Check that dtoverlay=dwc2 is in /boot/firmware/config.txt"
    exit 1
fi
