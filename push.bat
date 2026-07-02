@echo off
cd /d "E:\Startup\magicbridge"

:: Remove stale git lock if present
if exist .git\index.lock del /f .git\index.lock

:: Unstage log file, stage everything else
git restore --staged pi_fix_log.txt 2>nul
git add auto_duplicate_on_connect.ps1 autorun_fix.vbs run_fix.bat set_duplicate.bat set_extend.bat install.sh restore_pi.py src/core/video.py .gitignore

git commit -m "fix: HID gadget working -- dtoverlay=dwc2 moved to [all] section

- restore_pi.py: detect and fix dtoverlay in wrong config.txt section
  Pi 4 Bookworm puts it in [cm5] by default; UDC never appears
  sudobash()+awk avoids shell quoting bugs; fix SyntaxWarnings
- install.sh: insert dtoverlay after [all] tag (Pi4+Pi5 compatible)
- src/core/video.py: get_best_mjpeg_resolution() for auto resolution
  detection; start() picks highest native MJPEG res automatically
- set_duplicate.bat, set_extend.bat: Windows display mode switchers
- auto_duplicate_on_connect.ps1: auto-clone on second monitor connect
- run_fix.bat: one-click Pi fix runner with PYTHONIOENCODING fix

Verified on Pi 4 Bookworm:
  fe980000.usb in /sys/class/udc (UDC working)
  /dev/hidg0 keyboard + /dev/hidg1 mouse both active
  Logitech K120 USB identity VID=046d PID=c31c
  ustreamer MJPEG stream HTTP 200
  mb-gadget.service active and bound"

git push origin main
echo.
if %ERRORLEVEL% EQU 0 (echo SUCCESS! Pushed to GitHub.) else (echo FAILED - check errors above)
pause
