@echo off
cd /d "E:\Startup\magicbridge"
echo Deploying to Pi...
python deploy_pi.py
echo Done. Check deploy_log.txt
pause
