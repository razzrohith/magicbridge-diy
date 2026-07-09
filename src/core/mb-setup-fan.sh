#!/bin/bash
# ============================================================
#  MagicBridge - fan setup (30mm case fan, temperature-controlled)
#
#  Uses the Pi's own built-in gpio-fan device-tree overlay - no custom
#  daemon/code needed at all, the kernel handles on/off based on CPU temp.
#  This is deliberately NOT a Python service: fewer moving parts, and it
#  keeps working even if magicbridge.py or Python itself is down.
#
#  Usage:
#    sudo bash mb-setup-fan.sh <GPIO_PIN> [ON_TEMP_C]
#
#  Example (fan on GPIO14, turns on at 55C):
#    sudo bash mb-setup-fan.sh 14 55
#
#  GPIO_PIN is whichever pin the fan's control wire actually ends up on -
#  this can't be known/hardcoded ahead of the physical build, hence a
#  script argument instead of a fixed value. Common choices that avoid
#  colliding with I2C (OLED, GPIO2/3), the C790's I2S audio (GPIO18-21,
#  see EDID_CLONING_WORKFLOW.md), and the HID gadget's dwc2/USB pins:
#  GPIO14 or GPIO15 (UART pins, unused if the serial console is disabled,
#  which it is by default on a headless Pi) are reasonable defaults if
#  nothing else is planned for them.
#
#  Safe to run before the fan is physically wired: an unconnected GPIO
#  pin toggling on/off does nothing. Requires a reboot to take effect
#  (device-tree overlays are read at boot).
# ============================================================
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash mb-setup-fan.sh <GPIO_PIN> [ON_TEMP_C]"; exit 1; }

GPIO_PIN="${1:-}"
ON_TEMP_C="${2:-55}"

if [[ -z "$GPIO_PIN" ]]; then
    echo "Usage: sudo bash mb-setup-fan.sh <GPIO_PIN> [ON_TEMP_C]"
    echo "Example: sudo bash mb-setup-fan.sh 14 55"
    exit 1
fi

ON_TEMP_MILLIC=$((ON_TEMP_C * 1000))

CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"  # older Raspberry Pi OS layout

OVERLAY_LINE="dtoverlay=gpio-fan,gpiopin=${GPIO_PIN},temp=${ON_TEMP_MILLIC}"

if grep -q '^dtoverlay=gpio-fan' "$CONFIG_TXT" 2>/dev/null; then
    echo "Existing gpio-fan overlay line found in $CONFIG_TXT:"
    grep '^dtoverlay=gpio-fan' "$CONFIG_TXT"
    read -p "Replace it with '${OVERLAY_LINE}'? [y/N] " -n 1 -r; echo
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        sed -i "s|^dtoverlay=gpio-fan.*|${OVERLAY_LINE}|" "$CONFIG_TXT"
        echo "Updated."
    else
        echo "Left unchanged."
        exit 0
    fi
else
    echo "" >> "$CONFIG_TXT"
    echo "# MagicBridge - 30mm case fan, on above ${ON_TEMP_C}C" >> "$CONFIG_TXT"
    echo "$OVERLAY_LINE" >> "$CONFIG_TXT"
    echo "Added '${OVERLAY_LINE}' to $CONFIG_TXT"
fi

echo ""
echo "Reboot required for this to take effect: sudo reboot"
echo "After reboot, verify with: cat /sys/class/thermal/cooling_device*/type"
echo "(should list 'gpio-fan' among the cooling devices)"
