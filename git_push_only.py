"""Run git push origin main and log the result."""
import subprocess, sys, os, io

REPO = r"E:\Startup\magicbridge"
LOG  = os.path.join(REPO, "git_push_log.txt")

_log = open(LOG, "w", encoding="utf-8")
sys.stdout = io.TextIOWrapper(_log.buffer, encoding="utf-8", write_through=True)
sys.stderr = sys.stdout
print("=== git_push_only.py started ===")

os.chdir(REPO)

# Delete any stale lock files
for lock in [r".git\index.lock", r".git\HEAD.lock", r".git\refs\heads\main.lock"]:
    if os.path.exists(lock):
        try:
            os.unlink(lock)
            print(f"Removed {lock}")
        except Exception as e:
            print(f"Could not remove {lock}: {e}")

print("Running: git push origin main")
r = subprocess.run(["git", "push", "origin", "main"],
                   capture_output=True, text=True, cwd=REPO)
print("STDOUT:", r.stdout.strip() or "(empty)")
print("STDERR:", r.stderr.strip() or "(empty)")
print("Exit code:", r.returncode)

if r.returncode == 0:
    print("\nSUCCESS - pushed to GitHub!")
else:
    print("\nFAILED")

_log.flush()
_log.close()
