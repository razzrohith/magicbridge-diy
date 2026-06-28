@echo off
cd /d "E:\Startup\magicbridge"
git add -A
git commit -m "Fix nginx: move http2 to listen directive (nginx 1.22 on Bookworm)"
git push origin main --force
echo.
if %ERRORLEVEL% EQU 0 (echo SUCCESS!) else (echo FAILED)
pause
