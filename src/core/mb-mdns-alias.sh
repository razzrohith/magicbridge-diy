#!/bin/bash
# Publishes fixed mDNS aliases via avahi, independent of whatever the Pi's
# actual system hostname is set to.
#
# Why this exists: on 2026-07-09 both magicbridge.local and <hostname>.local
# were found broken. Root cause was two separate faults found on the live
# Pi: (1) avahi-daemon was masked+inactive (systemd-level disabled, not
# just stopped), and (2) the system hostname had been reset to a bogus
# "DESKTOP-XKWQUIV"-style value - almost certainly an SD-card-imaging-tool
# leftover, not anything MagicBridge itself set (install.sh has always set
# it to "magicbridge" correctly, see the HOSTNAME + mDNS section there).
#
# Fixing the hostname makes <hostname>.local work again, but only for
# whatever the hostname happens to be at that moment - it doesn't protect
# any *specific* .local address. If the hostname ever drifts again for any
# reason, whichever name depended on it breaks again right along with it.
# These aliases decouple the two: magicbridge.local (the product's branded
# address, per the handbook) and raj.local (this unit's expected name) both
# keep resolving via their own standing avahi-publish records, no matter
# what the box is actually named at the OS level.
# NOTE for the multi-unit rollout (handbook task #8, other physical units):
# "raj.local" is hardcoded here as this unit's expected name. If a second
# physical MagicBridge unit ever runs this same script on the same LAN,
# both would try to claim raj.local simultaneously - avahi would silently
# rename the loser to raj-2.local rather than erroring, which is confusing
# to debug from the client side. Revisit before cloning this setup: either
# make the alias name a per-unit config value, or drop the raj.local alias
# from units other than this one.
#
# STEALTH DEFAULT (2026): the branded "magicbridge.local" and personal
# "raj.local" aliases are LAN-visible NAME TELLS - a router client list or a
# network scan would surface the product/owner name, breaking the anonymity
# model. They are now OFF by default. avahi-daemon still auto-publishes
# <hostname>.local (a realistic "DESKTOP-XXXXXXX.local"), which - together with
# the IP - is enough to reach the unit without advertising what it is.
#
# Opt in to ONE friendly alias by setting "mdns_alias" in config.json
# (e.g. "office-pc" -> office-pc.local). Choose an innocuous name; leave it
# empty for full stealth.
set -e
CONFIG_FILE="/etc/magicbridge/config.json"
ALIAS=""
if [[ -f "$CONFIG_FILE" ]]; then
    ALIAS=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE')).get('mdns_alias','') or '')" 2>/dev/null || echo "")
fi
if [[ -z "$ALIAS" ]]; then
    echo "mb-mdns-alias: no alias configured (stealth default); nothing to publish"
    exit 0
fi
IP=$(hostname -I | awk '{print $1}')
avahi-publish -a -R "${ALIAS}.local" "$IP" &
PID1=$!
trap 'kill "$PID1" 2>/dev/null' TERM INT
wait "$PID1"
