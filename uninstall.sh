#!/bin/bash
# =============================================================
#  MagicBridge — Uninstaller
#  Removes all MagicBridge components from the Pi.
#  Leaves the OS, WiFi, SSH, and Tailscale untouched.
# =============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "→ $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash uninstall.sh"; exit 1; }

echo -e "${RED}MagicBridge Uninstaller${NC}"
echo ""
warn "This will stop and remove all MagicBridge services and files."
read -r -p "Continue? [y/N]: " ANS
[[ "${ANS,,}" == "y" ]] || { echo "Aborted."; exit 0; }

# ── Stop & disable services ───────────────────────────────────────────────────
info "Stopping services…"
for svc in magicbridge stealth-dashboard mb-gadget mb-provision mb-mac; do
    systemctl stop    "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
    rm -f "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload
ok "Services removed"

# ── USB gadget teardown ───────────────────────────────────────────────────────
GADGET="/sys/kernel/config/usb_gadget/g1"
if [[ -d "$GADGET" ]]; then
    info "Tearing down USB gadget…"
    echo "" > "$GADGET/UDC" 2>/dev/null || true
    rm -f "$GADGET/configs/c.1/hid.keyboard" \
          "$GADGET/configs/c.1/hid.mouse"    2>/dev/null || true
    for d in functions/hid.keyboard functions/hid.mouse \
              configs/c.1/strings/0x409 configs/c.1 strings/0x409; do
        rmdir "$GADGET/$d" 2>/dev/null || true
    done
    rmdir "$GADGET" 2>/dev/null || true
    ok "USB gadget removed"
fi

# ── nginx ─────────────────────────────────────────────────────────────────────
info "Removing nginx config…"
rm -f /etc/nginx/sites-enabled/magicbridge \
      /etc/nginx/sites-available/magicbridge

# Restore default if it was removed
[[ ! -f /etc/nginx/sites-enabled/default ]] && \
    ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default 2>/dev/null || true

nginx -t 2>/dev/null && systemctl reload nginx || warn "nginx reload failed — check manually"
ok "nginx config removed"

# ── Application files ─────────────────────────────────────────────────────────
info "Removing application files…"
rm -rf /opt/magicbridge
rm -f /usr/local/bin/mb-gadget.sh \
      /usr/local/bin/mb-provision.sh
ok "Application files removed"

# ── Config (ask first) ────────────────────────────────────────────────────────
echo ""
read -r -p "Also remove /etc/magicbridge/ (config, TLS cert, USB identity)? [y/N]: " CONF_ANS
if [[ "${CONF_ANS,,}" == "y" ]]; then
    rm -rf /etc/magicbridge
    ok "/etc/magicbridge removed"
else
    warn "Kept /etc/magicbridge/ — remove manually if needed"
fi

# ── Logs (ask first) ──────────────────────────────────────────────────────────
read -r -p "Also remove MagicBridge log files? [y/N]: " LOG_ANS
if [[ "${LOG_ANS,,}" == "y" ]]; then
    rm -f /var/log/magicbridge-auth.log \
          /var/log/magicbridge-sessions.log \
          /var/log/magicbridge-provision.log \
          /var/log/magicbridge.log \
          /var/log/mb-duckdns.log \
          /var/log/mb-hdmi-init.log \
          /var/log/magicbridge-update.log
    ok "Log files removed"
fi

# ── DuckDNS cron ─────────────────────────────────────────────────────────────
rm -f /etc/cron.d/mb-duckdns

# ── Firewall — restore permissive defaults ────────────────────────────────────
info "Restoring default firewall (ACCEPT all)…"
iptables -P INPUT ACCEPT
iptables -F INPUT
iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
ok "Firewall reset to ACCEPT"

# ── dtoverlay=dwc2 (ask — may be needed for other projects) ──────────────────
echo ""
read -r -p "Remove dtoverlay=dwc2 from /boot/firmware/config.txt? [y/N]: " DWC_ANS
if [[ "${DWC_ANS,,}" == "y" ]]; then
    CFG="/boot/firmware/config.txt"
    [[ -f "$CFG" ]] || CFG="/boot/config.txt"
    sed -i "/^dtoverlay=dwc2/d" "$CFG"
    ok "dtoverlay=dwc2 removed from $CFG"
fi

echo ""
echo -e "${GREEN}MagicBridge uninstalled.${NC}"
echo "nginx and SSH are still running. Tailscale was not touched."
echo "Reboot to complete USB gadget cleanup."
