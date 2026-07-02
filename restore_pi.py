"""MagicBridge comprehensive fix — video quality + HID keyboard/mouse."""
import subprocess, sys, time

try:
    import paramiko
except ImportError:
    print("Installing paramiko...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    import paramiko

PASS = "lol"
CANDIDATES = [
    ("172.16.20.197", "admin"),
    ("magicbridge.local", "admin"),
    ("172.16.20.116", "raj"),
    ("raj.local", "raj"),
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

connected = False
for HOST, USER in CANDIDATES:
    try:
        print(f"Trying {USER}@{HOST}...")
        ssh.connect(HOST, username=USER, password=PASS, timeout=8)
        print(f"Connected to {HOST}!\n")
        connected = True
        break
    except Exception as e:
        print(f"  Failed: {e}")

if not connected:
    print("\nERROR: Could not reach the Pi on any known address.")
    print("Make sure the Pi is powered on and on the same network.")
    sys.exit(1)

def run(cmd):
    _, out, err = ssh.exec_command(cmd)
    o = out.read().decode().strip()
    e = err.read().decode().strip()
    return o, e

def sudo(cmd):
    return run(f"echo {PASS} | sudo -S bash -c '{cmd}'")

def sudobash(script):
    """Run multi-line script via sudo — write to /tmp first, then exec."""
    sftp = ssh.open_sftp()
    with sftp.open('/tmp/_mb_gadget.sh', 'w') as fh:
        fh.write(script)
    sftp.close()
    out, err = sudo("bash /tmp/_mb_gadget.sh 2>&1")
    print(out[:2000])
    return 0

# ══════════════════════════════════════════════════════
# 0. DIAGNOSE UDC / dwc2
# ══════════════════════════════════════════════════════
print("=" * 55)
print("0. DWC2 / UDC DIAGNOSIS")
print("=" * 55)
dmesg_dwc, _ = run(f"echo {PASS} | sudo -S dmesg 2>/dev/null | grep -i 'dwc\\|otg\\|gadget\\|udc' | tail -15")
print("dmesg:\n" + (dmesg_dwc or "(none)"))
platform_usb, _ = run("ls /sys/bus/platform/devices/ 2>/dev/null | grep -i 'usb\\|dwc\\|otg'")
print(f"Platform USB devs: {platform_usb or '(none)'}")
udc_ls, _ = run("ls /sys/class/udc/ 2>/dev/null || echo EMPTY")
print(f"/sys/class/udc/: {udc_ls}")
modules_etc, _ = run("cat /etc/modules 2>/dev/null")
print(f"/etc/modules: {modules_etc}")
boot_lines, _ = run("grep -n 'dwc\\|otg' /boot/firmware/config.txt 2>/dev/null || grep -n 'dwc\\|otg' /boot/config.txt 2>/dev/null")
print(f"config.txt: {boot_lines}")
gadget_svc, _ = run(f"echo {PASS} | sudo -S systemctl status mb-gadget 2>/dev/null | head -12")
print(f"mb-gadget svc:\n{gadget_svc or '(not found)'}")

# Also ensure dwc2+libcomposite in /etc/modules for next boot
sudo("grep -q '^dwc2' /etc/modules || echo 'dwc2' >> /etc/modules")
sudo("grep -q '^libcomposite' /etc/modules || echo 'libcomposite' >> /etc/modules")

# ══════════════════════════════════════════════════════
# 1. FIX VIDEO QUALITY — force native MJPEG from card
# ══════════════════════════════════════════════════════
print("=" * 55)
print("1. FIXING VIDEO QUALITY (native MJPEG from capture card)")
print("=" * 55)

# Check current video.py
out, _ = run("grep -n 'format\\|MJPEG\\|resolution\\|desired' /opt/magicbridge/core/video.py | head -15")
print("Current ustreamer flags:\n" + out)

has_mjpeg, _ = run("grep -c '\"--format\"' /opt/magicbridge/core/video.py")
if has_mjpeg.strip() == "0":
    # Use Python to insert --format MJPEG before --resolution in the cmd list
    patch = (
        "import re\n"
        "f='/opt/magicbridge/core/video.py'\n"
        "t=open(f).read()\n"
        "if '\"--format\"' not in t:\n"
        "    t=t.replace('\"--resolution\"',  '\"--format\",\"MJPEG\",\"--resolution\"', 1)\n"
        "    open(f,'w').write(t)\n"
        "    print('PATCHED')\n"
        "else:\n"
        "    print('already_ok')\n"
    )
    # write patch to /tmp then run
    sftp = ssh.open_sftp()
    with sftp.open('/tmp/fix_mjpeg.py', 'w') as fh:
        fh.write(patch)
    sftp.close()
    out2, err2 = sudo("python3 /tmp/fix_mjpeg.py && cp /tmp/fix_mjpeg.py /tmp/fix_mjpeg_done.py")
    print(f"Patch result: {out2} {err2}")
    # verify
    has2, _ = run("grep -c '\"--format\"' /opt/magicbridge/core/video.py")
    if has2.strip() != "0":
        print("OK: Added --format MJPEG")
    else:
        print("ERROR: Could not patch video.py - check manually")
else:
    print("OK: --format MJPEG already present")

# ══════════════════════════════════════════════════════
# 2. CHECK HID GADGET STATUS
# ══════════════════════════════════════════════════════
print("\n" + "=" * 55)
print("2. CHECKING HID GADGET (keyboard + mouse)")
print("=" * 55)

hidg0, _ = run("ls /dev/hidg0 2>/dev/null || echo MISSING")
hidg1, _ = run("ls /dev/hidg1 2>/dev/null || echo MISSING")
dwc2,  _ = run("lsmod | grep -c dwc2 || echo 0")
udc,   _ = run("ls /sys/class/udc/ 2>/dev/null || echo NONE")
gadget,_ = run("ls /sys/kernel/config/usb_gadget/ 2>/dev/null || echo NONE")
config_txt,_ = run("grep -i dwc2 /boot/config.txt 2>/dev/null || grep -i dwc2 /boot/firmware/config.txt 2>/dev/null || echo NOT_IN_CONFIG")
usb_speed,_ = run("cat /sys/class/udc/*/current_speed 2>/dev/null || echo UNBOUND")

print(f"  /dev/hidg0        : {hidg0}")
print(f"  /dev/hidg1        : {hidg1}")
print(f"  dwc2 module loaded: {'YES' if dwc2 != '0' else 'NO'}")
print(f"  UDC devices       : {udc}")
print(f"  USB gadget        : {gadget}")
print(f"  config.txt dwc2   : {config_txt}")
print(f"  USB speed (OTG)   : {usb_speed}")

# ══════════════════════════════════════════════════════
# 3. FIX HID GADGET
# ══════════════════════════════════════════════════════
needs_reboot = False
print("\n" + "=" * 55)
print("3. FIXING HID GADGET")
print("=" * 55)

# Check and fix dwc2 -- must be in [all] section with dr_mode=peripheral
# Pi 4 Bookworm stock config has dtoverlay=dwc2 under [cm5] (Pi 5 only).
# If it's there, Pi 4 never sees it => UDC stays empty => no HID gadget.
print("  Checking dwc2 in boot config...")
cfg_line, _ = run("grep -i 'dtoverlay=dwc2' /boot/firmware/config.txt 2>/dev/null || echo ''")
print(f"  Current line: '{cfg_line.strip()}'")

# Check if dtoverlay=dwc2 is actually in the [all] section (not [cm4]/[cm5])
all_section, _ = run(
    "awk '/^\\[all\\]/{found=1} found{print}' /boot/firmware/config.txt 2>/dev/null || echo ''"
)
in_all_section = "dtoverlay=dwc2" in all_section
print(f"  In [all] section: {in_all_section}")

if not in_all_section or "dr_mode=peripheral" not in cfg_line:
    print("  FIXING: moving dtoverlay=dwc2,dr_mode=peripheral to [all] section")
    # Use sudobash() to avoid shell quoting issues with single quotes in sed/grep
    config_fix = """
set -e
CFG=/boot/firmware/config.txt
# Remove dtoverlay=dwc2 from wherever it is (cm5, cm4, global, anywhere)
sed -i '/^dtoverlay=dwc2/d' "$CFG"
# Ensure [all] section exists at end of file
grep -q '^\\[all\\]' "$CFG" || echo '[all]' >> "$CFG"
# Insert dtoverlay=dwc2,dr_mode=peripheral after [all] using awk (no quote issues)
awk '/^\\[all\\]/{print; print "dtoverlay=dwc2,dr_mode=peripheral"; next} {print}' \
    "$CFG" > /tmp/_mb_config_fixed.txt
cp /tmp/_mb_config_fixed.txt "$CFG"
echo "=== Config tail (last 8 lines) ==="
tail -8 "$CFG"
"""
    sudobash(config_fix)
    needs_reboot = True
else:
    print("  OK: dtoverlay=dwc2,dr_mode=peripheral already in [all] section")
    needs_reboot = False

# Ensure modules are loaded now
sudo("modprobe dwc2 2>/dev/null || true")
sudo("modprobe libcomposite 2>/dev/null || true")
time.sleep(1)

udc2, _ = run("ls /sys/class/udc/ 2>/dev/null || echo NONE")
print(f"  UDC after modprobe: {udc2}")

if "NONE" == udc2.strip() or udc2.strip() == "":
    print("  WARN: No UDC available — Pi USB-C port not in OTG mode yet.")
    print("  Check: is 'dtoverlay=dwc2' in /boot/firmware/config.txt?")
    print("  A reboot will be needed after this script.")
else:
    print(f"  OK: UDC found: {udc2}")

# Setup/reset the HID gadget
print("\n  Setting up USB HID gadget...")
gadget_script = r"""
set -e
modprobe libcomposite 2>/dev/null || true

GADGET=/sys/kernel/config/usb_gadget/magicbridge

# Tear down existing gadget if present
if [ -d "$GADGET" ]; then
    if [ -f "$GADGET/UDC" ] && [ -n "$(cat $GADGET/UDC 2>/dev/null)" ]; then
        echo "" > "$GADGET/UDC" 2>/dev/null || true
    fi
    rm -rf "$GADGET/configs/c.1/hid.keyboard" 2>/dev/null || true
    rm -rf "$GADGET/configs/c.1/hid.mouse" 2>/dev/null || true
    rmdir "$GADGET/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET/functions/hid.keyboard" 2>/dev/null || true
    rmdir "$GADGET/functions/hid.mouse" 2>/dev/null || true
    rmdir "$GADGET/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET" 2>/dev/null || true
fi

mkdir -p "$GADGET"
echo 0x046d > "$GADGET/idVendor"     # Logitech
echo 0xc31c > "$GADGET/idProduct"    # K120 Keyboard
echo 0x0110 > "$GADGET/bcdDevice"
echo 0x0200 > "$GADGET/bcdUSB"
echo 0xef   > "$GADGET/bDeviceClass"
echo 0x02   > "$GADGET/bDeviceSubClass"
echo 0x01   > "$GADGET/bDeviceProtocol"

mkdir -p "$GADGET/strings/0x409"
echo "MAGICBR0001"    > "$GADGET/strings/0x409/serialnumber"
echo "Logitech"       > "$GADGET/strings/0x409/manufacturer"
echo "USB Receiver"   > "$GADGET/strings/0x409/product"

# HID Keyboard function
mkdir -p "$GADGET/functions/hid.keyboard"
echo 1 > "$GADGET/functions/hid.keyboard/protocol"
echo 1 > "$GADGET/functions/hid.keyboard/subclass"
echo 8 > "$GADGET/functions/hid.keyboard/report_length"
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
    > "$GADGET/functions/hid.keyboard/report_desc"

# HID Mouse function
mkdir -p "$GADGET/functions/hid.mouse"
echo 2 > "$GADGET/functions/hid.mouse/protocol"
echo 1 > "$GADGET/functions/hid.mouse/subclass"
echo 5 > "$GADGET/functions/hid.mouse/report_length"
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x05\x15\x00\x25\x01\x95\x05\x75\x01\x81\x02\x95\x01\x75\x03\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
    > "$GADGET/functions/hid.mouse/report_desc"

# Config
mkdir -p "$GADGET/configs/c.1/strings/0x409"
echo "HID Config"  > "$GADGET/configs/c.1/strings/0x409/configuration"
echo 250           > "$GADGET/configs/c.1/MaxPower"
echo 0xa0          > "$GADGET/configs/c.1/bmAttributes"

ln -sf "$GADGET/functions/hid.keyboard" "$GADGET/configs/c.1/" 2>/dev/null || true
ln -sf "$GADGET/functions/hid.mouse"    "$GADGET/configs/c.1/" 2>/dev/null || true

# Bind to UDC
UDC=$(ls /sys/class/udc/ 2>/dev/null | head -1)
if [ -n "$UDC" ]; then
    echo "$UDC" > "$GADGET/UDC" && echo "Bound to UDC: $UDC" || echo "UDC bind failed (try reboot)"
else
    echo "No UDC available - reboot needed"
fi
"""

rc = sudobash(gadget_script)
print(f"  Gadget setup exit code: {rc}")

# Check result
hidg0_new, _ = run("ls -la /dev/hidg* 2>/dev/null || echo 'NO HID DEVICES'")
print(f"  HID devices: {hidg0_new}")

udc_speed, _ = run("cat /sys/class/udc/*/current_speed 2>/dev/null || echo 'UNBOUND'")
print(f"  UDC speed: {udc_speed}")

# Fix permissions on HID devices
sudo("chmod 660 /dev/hidg* 2>/dev/null; chown root:input /dev/hidg* 2>/dev/null || true")
sudo("usermod -aG input magicbridge 2>/dev/null || true")

# ══════════════════════════════════════════════════════
# 4. RESTART MAGICBRIDGE
# ══════════════════════════════════════════════════════
print("\n" + "=" * 55)
print("4. RESTARTING SERVICES")
print("=" * 55)

sudo("pkill -f ustreamer 2>/dev/null; true")
sudo("systemctl restart magicbridge")
print("Waiting 6s for startup...")
time.sleep(6)

# ══════════════════════════════════════════════════════
# 5. VERIFY
# ══════════════════════════════════════════════════════
print("\n" + "=" * 55)
print("5. VERIFICATION")
print("=" * 55)

stream, _ = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/")
print(f"  Stream HTTP  : {stream}")

journal, _ = run(f"echo {PASS} | sudo -S journalctl -u magicbridge -n 8 --no-pager 2>/dev/null | grep -E 'ustreamer|MJPEG|pixelformat|hid|HID|WARNING|ERROR|INFO.*start'")
print(f"  Journal:\n{journal}")

hidg_final, _ = run("ls /dev/hidg* 2>/dev/null || echo 'MISSING - reboot may be needed'")
print(f"  HID devices  : {hidg_final}")

usb_final, _ = run("cat /sys/class/udc/*/current_speed 2>/dev/null || echo 'UNBOUND'")
print(f"  USB OTG speed: {usb_final}")

ssh.close()

print("\n" + "=" * 55)
if needs_reboot:
    print("REBOOTING Pi to apply dtoverlay=dwc2 in [all] section (Pi 4 UDC fix)...")
    # Reconnect briefly just to reboot
    ssh2 = paramiko.SSHClient()
    ssh2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for h, u in CANDIDATES:
        try:
            ssh2.connect(h, username=u, password=PASS, timeout=8)
            ssh2.exec_command(f"echo {PASS} | sudo -S reboot")
            ssh2.close()
            print("Reboot command sent!")
            print("Wait ~30 seconds then refresh https://magicbridge.local")
            print("After reboot: video = MJPEG quality, HID = keyboard+mouse active")
            break
        except Exception:
            pass
elif "MISSING" in hidg_final:
    print("HID devices missing after gadget setup — check output above")
elif stream == "200":
    print("OK: Stream up with MJPEG!")
    print("OK: HID active — keyboard & mouse should work")
else:
    print("Check output above for issues.")
print("=" * 55)
print("\nDone. Output saved to pi_fix_log.txt")
