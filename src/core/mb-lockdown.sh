#!/bin/bash
# MagicBridge Tailscale-only network lockdown toggle
#
# Usage: mb-lockdown.sh on|off|status
#
# When "on": ports 80/443 (the whole web UI + API surface behind nginx)
# only accept connections arriving via the tailscale0 interface. Every
# other interface gets dropped on those two ports.
#
# Covers BOTH IPv4 and IPv6 (B5): nginx listens on [::]:443 and the install
# firewall now accepts v6 443, so an IPv4-only lockdown left the whole web/admin
# surface reachable over IPv6 from any L2 host - a full bypass. Every rule is
# mirrored with ip6tables and persisted to rules.v6.
#
# SSH (port 22) is never touched by this script on purpose, so toggling
# this on can never lock you out over SSH even if Tailscale isn't set up
# yet. The API layer that calls this script also refuses to enable
# lockdown unless Tailscale is confirmed connected first.
set -e

ACTION="${1:-status}"
TAG="mb-lockdown"

# Apply to IPv4 always, and IPv6 when ip6tables is present (it is on RaspiOS).
FAMILIES="iptables"
command -v ip6tables >/dev/null 2>&1 && FAMILIES="iptables ip6tables"

_remove_rules() {
    local ipt port
    for ipt in $FAMILIES; do
        for port in 80 443; do
            while "$ipt" -C INPUT -p tcp --dport "$port" -m comment --comment "$TAG" -j DROP 2>/dev/null; do
                "$ipt" -D INPUT -p tcp --dport "$port" -m comment --comment "$TAG" -j DROP
            done
            while "$ipt" -C INPUT -i tailscale0 -p tcp --dport "$port" -m comment --comment "$TAG" -j ACCEPT 2>/dev/null; do
                "$ipt" -D INPUT -i tailscale0 -p tcp --dport "$port" -m comment --comment "$TAG" -j ACCEPT
            done
        done
    done
}

_persist() {
    iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
    command -v ip6tables-save >/dev/null 2>&1 && ip6tables-save > /etc/iptables/rules.v6 2>/dev/null || true
}

case "$ACTION" in
    on)
        _remove_rules
        # Inserted at the top of INPUT so these are evaluated before the
        # general "allow 80/443 from anywhere" rules install.sh sets up.
        for ipt in $FAMILIES; do
            "$ipt" -I INPUT 1 -i tailscale0 -p tcp --dport 80  -m comment --comment "$TAG" -j ACCEPT
            "$ipt" -I INPUT 2 -i tailscale0 -p tcp --dport 443 -m comment --comment "$TAG" -j ACCEPT
            "$ipt" -I INPUT 3 -p tcp --dport 80  -m comment --comment "$TAG" -j DROP
            "$ipt" -I INPUT 4 -p tcp --dport 443 -m comment --comment "$TAG" -j DROP
        done
        _persist
        echo "lockdown: ON (web access restricted to Tailscale only, IPv4+IPv6, SSH unaffected)"
        ;;
    off)
        _remove_rules
        _persist
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
