#!/bin/bash
# MagicBridge Tailscale-only network lockdown toggle
#
# Usage: mb-lockdown.sh on|off|status
#
# When "on": ports 80/443 (the whole web UI + API surface behind nginx)
# only accept connections arriving via the tailscale0 interface. Every
# other interface gets dropped on those two ports.
#
# SSH (port 22) is never touched by this script on purpose, so toggling
# this on can never lock you out over SSH even if Tailscale isn't set up
# yet. The API layer that calls this script also refuses to enable
# lockdown unless Tailscale is confirmed connected first.
set -e

ACTION="${1:-status}"
TAG="mb-lockdown"

_remove_rules() {
    while iptables -C INPUT -p tcp --dport 80  -m comment --comment "$TAG" -j DROP 2>/dev/null; do
        iptables -D INPUT -p tcp --dport 80  -m comment --comment "$TAG" -j DROP
    done
    while iptables -C INPUT -p tcp --dport 443 -m comment --comment "$TAG" -j DROP 2>/dev/null; do
        iptables -D INPUT -p tcp --dport 443 -m comment --comment "$TAG" -j DROP
    done
    while iptables -C INPUT -i tailscale0 -p tcp --dport 80  -m comment --comment "$TAG" -j ACCEPT 2>/dev/null; do
        iptables -D INPUT -i tailscale0 -p tcp --dport 80  -m comment --comment "$TAG" -j ACCEPT
    done
    while iptables -C INPUT -i tailscale0 -p tcp --dport 443 -m comment --comment "$TAG" -j ACCEPT 2>/dev/null; do
        iptables -D INPUT -i tailscale0 -p tcp --dport 443 -m comment --comment "$TAG" -j ACCEPT
    done
}

case "$ACTION" in
    on)
        _remove_rules
        # Inserted at the top of INPUT so these are evaluated before the
        # general "allow 80/443 from anywhere" rules install.sh sets up.
        iptables -I INPUT 1 -i tailscale0 -p tcp --dport 80  -m comment --comment "$TAG" -j ACCEPT
        iptables -I INPUT 2 -i tailscale0 -p tcp --dport 443 -m comment --comment "$TAG" -j ACCEPT
        iptables -I INPUT 3 -p tcp --dport 80  -m comment --comment "$TAG" -j DROP
        iptables -I INPUT 4 -p tcp --dport 443 -m comment --comment "$TAG" -j DROP
        iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
        echo "lockdown: ON (web access restricted to Tailscale only, SSH unaffected)"
        ;;
    off)
        _remove_rules
        iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
        echo "lockdown: OFF (LAN access restored)"
        ;;
    status)
        if iptables -C INPUT -p tcp --dport 443 -m comment --comment "$TAG" -j DROP 2>/dev/null; then
            echo "on"
        else
            echo "off"
        fi
        ;;
    *)
        echo "Usage: mb-lockdown.sh on|off|status" >&2
        exit 1
        ;;
esac
