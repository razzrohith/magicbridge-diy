@echo off
cd /d "E:\Startup\magicbridge"
git add -A
git commit -m "Remove personal names - use magicbridge/admin everywhere"
git push origin main --force
echo.
if %ERRORLEVEL% EQU 0 (echo SUCCESS!) else (echo FAILED - see error above)
pause
