@echo off
cd /d "E:\Startup\magicbridge"
echo Deploying to Pi...
python deploy_pi.py
echo.
echo Pushing to GitHub...
python git_push_only.py
echo.
echo Done. Check deploy_log.txt and git_push_log.txt
pause
