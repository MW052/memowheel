@echo off
title Trip Video
cd /d "%~dp0"
echo.
echo   Starting Trip Video (console / debug mode)...
echo   For an everyday run with NO window, use "Start Trip Video.vbs" instead -
echo   it puts a small icon in the system tray (Open / View log / Quit).
echo.
echo   A browser window will open in a few seconds.
echo   Leave this window open while you use the app; close it when you're done.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py manage.py serve
) else (
    python manage.py serve
)
echo.
echo   Trip Video has stopped. You can close this window.
pause
