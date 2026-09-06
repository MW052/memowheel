@echo off
title Install Trip Video
cd /d "%~dp0"
echo.
echo   Installing Trip Video (one time). This can take a few minutes.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python -m pip install -r requirements.txt
    ) else (
        echo   Python was not found. Install Python 3 from https://www.python.org/downloads/
        echo   ^(tick "Add python.exe to PATH" during install^), then run this again.
        pause
        exit /b 1
    )
)
echo.
echo   You also need ffmpeg on your PATH ^(https://ffmpeg.org^) for video export.
echo   Done. Double-click "Start Trip Video.bat" to launch the app.
pause
