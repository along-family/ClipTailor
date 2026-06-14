@echo off
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py video_ad_trimmer.py gui
    goto done
)

where python >nul 2>nul
if not errorlevel 1 (
    python video_ad_trimmer.py gui
    goto done
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" video_ad_trimmer.py gui
    goto done
)

echo Python not found. Please install Python 3.11+.
exit /b 1

:done
if errorlevel 1 pause
