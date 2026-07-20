<#
=============================================================================
 mb-rescue.ps1 - diagnose (and optionally fix) a MagicBridge unit that is
 stuck on its setup hotspot.

 WHY THIS EXISTS
 A unit that fails first-boot only exists on its OWN access point
 ("MagicBridge-Setup", 192.168.73.1). To reach it you must join that hotspot -
 which cuts your laptop's internet, so you cannot ask for help while connected.
 This script is fully OFFLINE and self-contained: join the hotspot, run it, it
 writes a report to a FILE, then you rejoin your normal WiFi and share the file.

 USAGE (PowerShell, from your laptop, while joined to MagicBridge-Setup):
     powershell -ExecutionPolicy Bypass -File mb-rescue.ps1
     powershell -ExecutionPolicy Bypass -File mb-rescue.ps1 -Fix

 -Fix applies the known remedies for the "endless please-wait / join-hotspot"
 loop: it forces the first-boot done-markers to exist (so first-boot can never
 re-run and re-wipe the WiFi you just entered), disables both first-boot units,
 makes /boot/firmware nofail, and runs the online rootfs grow.
=============================================================================
#>
param(
  [string]$PiIp    = "192.168.73.1",
  [string]$User    = "raj",
  [string]$Password= "lol",
  [string]$Report  = "$env:USERPROFILE\Desktop\magicbridge-rescue-report.txt",
  [switch]$Fix
)

function Say($m){ Write-Host $m }
Say "MagicBridge rescue - target $PiIp  (report -> $Report)"

# --- reachability ----------------------------------------------------------
if (-not (Test-Connection -ComputerName $PiIp -Count 2 -Quiet -ErrorAction SilentlyContinue)) {
  Say "ERROR: $PiIp is not reachable."
  Say "  Are you joined to the 'MagicBridge-Setup' WiFi? (it has no password)"
  Say "  Windows may warn 'no internet' - that is expected, stay connected."
  exit 1
}
Say "Unit is reachable."

# --- host key: a fresh unit generates its own, so discover it (offline) -----
$hkArgs = @()
try {
  $scan = & ssh-keyscan -T 8 $PiIp 2>$null
  if ($scan) {
    $tmp = [IO.Path]::GetTempFileName(); $scan | Set-Content $tmp -Encoding ascii
    foreach ($l in (& ssh-keygen -lf $tmp 2>$null)) {
      $fp = ($l -split '\s+')[1]
      if ($fp -like 'SHA256:*') { $hkArgs += @('-hostkey', $fp) }
    }
    Remove-Item $tmp -ErrorAction SilentlyContinue
  }
} catch {}
if ($hkArgs.Count -eq 0) { Say "WARN: could not read host key; will try anyway." }

# --- the remote diagnostic bundle -----------------------------------------
$diag = @'
echo "######## MAGICBRIDGE RESCUE REPORT ########"
echo "date: $(date)   host: $(hostname)   uptime: $(uptime -p)"
echo
echo "==== 1. IS THE ROOTFS WRITABLE? (a read-only root makes the done-marker fail silently) ===="
mount | grep " / "
echo
echo "==== 2. FIRST-BOOT MARKERS (missing => first-boot re-runs => wipes WiFi => LOOP) ===="
ls -l /etc/magicbridge/.firstboot-done /etc/magicbridge/.firstboot-late-done 2>&1
echo "mb-firstboot      enabled=$(systemctl is-enabled mb-firstboot 2>&1) active=$(systemctl is-active mb-firstboot 2>&1)"
echo "mb-firstboot-late enabled=$(systemctl is-enabled mb-firstboot-late 2>&1) active=$(systemctl is-active mb-firstboot-late 2>&1)"
echo
echo "==== 3. HOW MANY TIMES HAS FIRST-BOOT RUN? (>1 == the loop) ===="
grep -c "mb-firstboot starting" /var/log/magicbridge-firstboot.log 2>/dev/null
echo "---- last 25 lines of first-boot log ----"
tail -25 /var/log/magicbridge-firstboot.log 2>&1
echo "---- last 15 lines of first-boot-late log ----"
tail -15 /var/log/magicbridge-firstboot-late.log 2>&1
echo
echo "==== 4. SAVED WIFI (empty after you entered it == it was wiped) ===="
ls -l /etc/NetworkManager/system-connections/ 2>&1
cat /etc/magicbridge/.provision-wifi 2>/dev/null | head -1
nmcli -t -f NAME,TYPE connection show 2>&1 | head
echo
echo "==== 5. DID THE ROOTFS GROW TO FILL THE CARD? ===="
df -h / | tail -1
lsblk -bno NAME,SIZE,TYPE 2>&1 | head
echo
echo "==== 6. BOOT SAFETY: fstab (is /boot/firmware nofail?) ===="
grep -vE "^\s*#|^\s*$" /etc/fstab
echo
echo "==== 7. POWER (under-voltage causes flakiness unrelated to our code) ===="
vcgencmd get_throttled 2>&1
journalctl -b 2>/dev/null | grep -i "voltage" | tail -5
echo
echo "==== 8. FAILED UNITS ===="
systemctl --failed --no-legend 2>&1 | head
echo
echo "==== 9. PROVISIONING AP STATE ===="
systemctl is-active mb-provision 2>&1; ip -o addr show wlan0 2>&1
echo "######## END REPORT ########"
'@

$fix = @'
echo "######## APPLYING FIXES ########"
mount -o remount,rw / 2>/dev/null
mkdir -p /etc/magicbridge
# Stop the loop: guarantee both markers exist so first-boot can never re-run and
# re-wipe the WiFi. This is the single most important fix.
date > /etc/magicbridge/.firstboot-done 2>/dev/null
date > /etc/magicbridge/.firstboot-late-done 2>/dev/null
sync
systemctl disable mb-firstboot 2>/dev/null
systemctl disable mb-firstboot-late 2>/dev/null
echo "markers: $(ls -l /etc/magicbridge/.firstboot*done 2>&1 | wc -l) present; first-boot disabled"
# Boot safety: a non-essential mount must never be able to block boot.
if grep -qE "^[^#].*/boot/firmware" /etc/fstab && ! grep -qE "^[^#].*/boot/firmware.*nofail" /etc/fstab; then
  sed -i -E "/^[^#].*[[:space:]]\/boot\/firmware[[:space:]]/ s/(vfat[[:space:]]+)([^[:space:]]+)/\1\2,nofail,x-systemd.device-timeout=15s/" /etc/fstab
  echo "/boot/firmware made nofail"
fi
# Online rootfs grow (safe no-op if already full).
[ -x /usr/local/bin/mb-firstboot-late.sh ] && bash /usr/local/bin/mb-firstboot-late.sh
echo "######## FIXES APPLIED - reboot the unit, then rejoin your normal WiFi ########"
'@

$script = $diag
if ($Fix) { $script = "$diag`n$fix" }

Say "Collecting diagnostics$(if($Fix){' and applying fixes'})..."
$plink = (Get-Command plink -ErrorAction SilentlyContinue).Source
if (-not $plink) { Say "ERROR: plink (PuTTY) not found on PATH - install PuTTY."; exit 1 }

$out = $script | & $plink -batch -ssh "$User@$PiIp" -pw $Password @hkArgs "sudo -S bash -s" 2>&1

$header = @(
  "MagicBridge rescue report",
  "generated: $(Get-Date)",
  "target   : $PiIp",
  "mode     : $(if($Fix){'DIAGNOSE + FIX'}else{'DIAGNOSE ONLY'})",
  ("=" * 60)
)
($header + $out) | Set-Content -Path $Report -Encoding utf8

Say ""
Say "Done. Report saved to:"
Say "   $Report"
Say ""
Say "Next: rejoin your normal WiFi, then send that file."
if (-not $Fix) { Say "If it shows the loop (first-boot ran more than once), re-run with  -Fix" }
