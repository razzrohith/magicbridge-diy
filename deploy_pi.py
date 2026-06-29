"""Deploy updated files to Pi and verify everything is working."""
import subprocess, sys, os, time, io

PI_HOST = "raj.local"
PI_USER = "raj"
PI_PASS = "lol"
PI_IP   = "172.16.20.116"
REPO    = r"E:\Startup\magicbridge"
LOG     = os.path.join(REPO, "deploy_log.txt")

_log = open(LOG, "w", encoding="utf-8")
sys.stdout = io.TextIOWrapper(_log.buffer, encoding="utf-8", write_through=True)
sys.stderr = sys.stdout
print("=== deploy_pi.py started ===")

try:
    import paramiko
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "paramiko", "-q"], check=True)
    import paramiko

def ssh_run(ssh, cmd, quiet=False):
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if not quiet:
        print(f"  $ {cmd[:80]}")
        if out: print(f"    {out[:200]}")
        if err and "sudo" not in err: print(f"    ERR: {err[:200]}")
    return out

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect(PI_HOST, username=PI_USER, password=PI_PASS, timeout=10)
    print(f"Connected to {PI_HOST}")
except Exception as e:
    print(f"mDNS failed, trying {PI_IP}...")
    ssh.connect(PI_IP, username=PI_USER, password=PI_PASS, timeout=10)
    print(f"Connected via {PI_IP}")

# 1. Upload video.py via /tmp (avoid permission issue on /opt)
sftp = ssh.open_sftp()
local_video = os.path.join(REPO, "src", "core", "video.py")
sftp.put(local_video, "/tmp/video_new.py")
sftp.close()
print("Uploaded video.py to /tmp/video_new.py")

# 2. sudo move into place
ssh_run(ssh, "echo lol | sudo -S cp /tmp/video_new.py /opt/magicbridge/core/video.py")
print("Deployed -> /opt/magicbridge/core/video.py")

# 3. Restart magicbridge
ssh_run(ssh, "echo lol | sudo -S systemctl restart magicbridge")
time.sleep(3)

# 4. Status check
print("\n=== Verification ===")
print("magicbridge:", ssh_run(ssh, "systemctl is-active magicbridge", quiet=True))
print("mb-gadget:  ", ssh_run(ssh, "systemctl is-active mb-gadget", quiet=True))
print("hidg0/hidg1:", ssh_run(ssh, "ls /dev/hidg0 /dev/hidg1 2>&1", quiet=True))
print("UDC:        ", ssh_run(ssh, "ls /sys/class/udc/", quiet=True))

# 5. Check stream
import urllib.request
try:
    r = urllib.request.urlopen("http://172.16.20.116:8889/stream", timeout=5)
    print("Video stream: HTTP", r.status)
except Exception as e:
    print("Video stream:", e)

print("\n=== DONE ===")
ssh.close()
_log.flush()
_log.close()
