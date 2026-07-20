#!/usr/bin/env bash
# ============================================================
#  mb-firstboot-late.sh — POST-boot first-run finalize (part 2).
#
#  Runs ONCE, AFTER the system is fully up — never in the early boot path. This
#  split is deliberate and load-bearing: mb-firstboot is boot-critical (WiFi
#  provisioning waits on it), so anything slow, or needing a live device, is done
#  HERE. By the time this runs the marker is written and the unit is on the
#  network, so nothing here can block boot or strand the user on the hotspot.
#
#    1. Safety-net the rootfs expansion (ONLINE resize; pishrink's rc.local hook
#       is the primary path, this catches the case where it didn't take).
#    2. Give this unit a UNIQUE EDID serial. Every DIY unit otherwise ships the
#       IDENTICAL Dell EDID, so two units could be cross-linked by their monitor
#       serial. Identity stays a real DELL P2419H; only the serial differs.
#    3. Flag under-voltage (weak PSU causes flakiness unrelated to our code).
#
#  Marker-guarded -> runs exactly once, so the EDID serial and FS size stay
#  STABLE afterward. Every step is best-effort and ALWAYS exits 0: a failure
#  means a smaller disk or the baked EDID serial, never a broken/looping unit.
# ============================================================
set +e
MARKER=/etc/magicbridge/.firstboot-late-done
EDID=/opt/magicbridge/edid/mb-edid-1080p50.hex
LOG=/var/log/magicbridge-firstboot-late.log
exec >> "$LOG" 2>&1
echo "=== [$(date)] mb-firstboot-late starting ==="
[ -e "$MARKER" ] && { echo "already done - nothing to do"; exit 0; }

# ---- 1. Rootfs expand safety net (ONLINE; never unmount root) --------------
# Detect by content, NEVER by hardcoded index: a flashed card can be a different
# size/device (mmcblk0 vs sda) than the golden one.
ROOTDEV="$(findmnt -no SOURCE / 2>/dev/null)"
PNAME="$(basename "$ROOTDEV" 2>/dev/null)"
DNAME="$(lsblk -no pkname "$ROOTDEV" 2>/dev/null | head -1)"
if [ -n "$DNAME" ] && [ -b "$ROOTDEV" ]; then
    DISK="/dev/$DNAME"
    PARTNUM="$(cat "/sys/class/block/$PNAME/partition" 2>/dev/null)"
    start="$(cat "/sys/class/block/$PNAME/start" 2>/dev/null || echo 0)"
    # SAFETY: only ever grow the LAST partition - growing one with anything after
    # it would overwrite that partition.
    last=1
    for s in /sys/class/block/"$DNAME"*/start; do
        [ -e "$s" ] || continue
        o="$(cat "$s" 2>/dev/null || echo 0)"
        [ "$o" -gt "$start" ] && last=0
    done
    disk_sz="$(blockdev --getsz "$DISK" 2>/dev/null || echo 0)"
    part_sz="$(blockdev --getsz "$ROOTDEV" 2>/dev/null || echo 0)"
    free=$(( disk_sz - start - part_sz ))
    if [ "$last" != "1" ]; then
        echo "root is not the last partition - refusing to grow"
    elif [ "$free" -lt 131072 ]; then
        echo "rootfs already fills the card (${free} spare sectors) - nothing to do"
    else
        echo "growing root $ROOTDEV on $DISK (${free} sectors free)"
        if echo ",$(( disk_sz - start ))" | sfdisk -N "$PARTNUM" --force "$DISK" >/dev/null 2>&1; then
            partprobe "$DISK" 2>/dev/null || partx -u "$DISK" 2>/dev/null
            udevadm settle --timeout=10 2>/dev/null; sleep 1
            # ONLINE grow: root is mounted and cannot be unmounted. ext4 online
            # resize is exactly the supported path here; a forced offline fsck
            # would need an unmount we can never get.
            if resize2fs "$ROOTDEV" >/dev/null 2>&1; then
                echo "rootfs grown -> $(df -h / | awk 'NR==2{print $2}')"
            else
                echo "resize2fs failed - leaving size as-is (boot unaffected)"
            fi
        else
            echo "sfdisk grow failed - leaving size as-is (boot unaffected)"
        fi
    fi
else
    echo "could not resolve root device - skipping expand"
fi

# ---- 2. Unique per-unit EDID serial ---------------------------------------
# Patch the 32-bit serial (EDID bytes 12-15) and fix the base-block checksum
# (byte 127 makes bytes 0..127 sum to 0 mod 256). Monitor name/manufacturer are
# untouched, so the target still sees a genuine "DELL P2419H".
if [ -f "$EDID" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$EDID" <<'PY'
import random, re, sys
p = sys.argv[1]
raw = open(p).read()
b = [int(x, 16) for x in re.findall(r'[0-9a-fA-F]{2}', raw)]
if len(b) >= 128:
    ser = random.randint(0x01000000, 0xfffffffe)
    b[12] = ser & 0xFF; b[13] = (ser >> 8) & 0xFF
    b[14] = (ser >> 16) & 0xFF; b[15] = (ser >> 24) & 0xFF
    b[127] = (256 - (sum(b[0:127]) % 256)) % 256      # recompute checksum
    assert sum(b[0:128]) % 256 == 0, "checksum fix failed"
    with open(p, "w", newline="\n") as f:
        for blk in (b[:128], b[128:256]):
            if not blk: continue
            for i in range(0, len(blk), 16):
                f.write(" ".join("%02x" % x for x in blk[i:i+16]) + "\n")
            f.write("\n")
    print("EDID serial randomized -> 0x%08x (checksum OK)" % ser)
PY
    # Re-apply so the change is live for the currently attached source.
    /usr/local/bin/mb-hdmi-init.sh --init >/dev/null 2>&1 && echo "EDID re-applied"
fi

# ---- 3. Under-voltage flag -------------------------------------------------
if command -v vcgencmd >/dev/null 2>&1; then
    T=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    if [ -n "$T" ] && [ "$T" != "0x0" ]; then
        echo "WARNING: get_throttled=$T - under-voltage/throttling seen. Use a 5V/3A supply; weak power causes flakiness unrelated to MagicBridge."
    else
        echo "power OK (get_throttled=${T:-n/a})"
    fi
fi

# ---- 4. Mark done (verified + synced, same lesson as mb-firstboot) ---------
mkdir -p "$(dirname "$MARKER")" 2>/dev/null
date > "$MARKER" 2>/dev/null
sync
[ -s "$MARKER" ] || echo "WARNING: failed to write $MARKER (this step may repeat)"
systemctl disable mb-firstboot-late.service 2>/dev/null || true
echo "=== [$(date)] mb-firstboot-late done ==="
exit 0
