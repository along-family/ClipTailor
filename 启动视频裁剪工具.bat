@echo off
cd /d "%~dp0"
py video_ad_trimmer.py gui
if errorlevel 1 pause
