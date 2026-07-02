@echo off
echo Switching to KVM Mode (Second Screen Only)...
DisplaySwitch.exe /external
echo Done. Desktop is now on capture card display.
timeout /t 2 /nobreak >nul
