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
# DEFAULT = "magicbridge" (2026-07): publishes magicbridge.local out of the box
# so a headless / OLED-less unit is reachable by name without hunting the router
# for its IP (owner's explicit reachability decision). Set via config.json
# "mdns_alias"; use any innocuous name (e.g. "office-pc" -> office-pc.local), or
# empty string "" for FULL stealth (no branded LAN name at all - avahi still
# auto-publishes the realistic <hostname>.local, e.g. DESKTOP-XXXXXXX.local).
#
# TRADE-OFF: a branded "magicbridge.local" is a LAN-visible name tell, and every
# unit sharing this alias will COLLIDE on one network (avahi renames losers
# magicbridge-2.local). For a fleet / units shipped to others, give each a unique
# innocuous name instead. Only visible on the control LAN - the target (USB/HDMI)
# never sees it.
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

# Pick the REAL LAN IPv4 to advertise. Deliberately skip:
#   - the provisioning setup-AP range 192.168.73.x: while the setup hotspot is
#     up the Pi briefly holds 192.168.73.1, and publishing THAT is exactly the
#     bug that left "${ALIAS}.local -> 192.168.73.1" advertised forever after
#     setup ended (the AP IP is long dead, so the name never worked again);
#   - link-local 169.254.x and IPv6.
pick_ip() {
    local ip
    for ip in $(hostname -I); do
        case "$ip" in
            *:*)          continue ;;   # IPv6
            192.168.73.*) continue ;;   # provisioning setup-AP address
            169.254.*)    continue ;;   # link-local
        esac
        printf '%s' "$ip"; return 0
    done
    printf ''                            # nothing suitable yet
}

# Kill any stale publisher for this alias (a previous run, or a
# provisioning-time invocation that bound the setup-AP IP). Without this a
# leftover `avahi-publish ... ${ALIAS}.local 192.168.73.1` keeps answering
# from the network long after the hotspot is gone.
pkill -f "avahi-publish -a -R ${ALIAS}\.local" 2>/dev/null || true

PUB_PID=""
CUR_IP=""
cleanup() { [[ -n "$PUB_PID" ]] && kill "$PUB_PID" 2>/dev/null; exit 0; }
trap cleanup TERM INT

# Re-publish whenever the LAN IP changes (DHCP reassign / relocation) so
# ${ALIAS}.local always follows the unit. The old code bound the IP once at
# startup and went stale on every change - the "breaks all the time" symptom.
while true; do
    NEW_IP=$(pick_ip)
    if [[ -n "$NEW_IP" && "$NEW_IP" != "$CUR_IP" ]]; then
        [[ -n "$PUB_PID" ]] && kill "$PUB_PID" 2>/dev/null || true
        avahi-publish -a -R "${ALIAS}.local" "$NEW_IP" &
        PUB_PID=$!
        CUR_IP="$NEW_IP"
        echo "mb-mdns-alias: publishing ${ALIAS}.local -> $CUR_IP"
    fi
    sleep 10
done
