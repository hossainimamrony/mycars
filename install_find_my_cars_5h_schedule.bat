@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_find_my_cars_5h_schedule.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE% NEQ 0 (
    echo.
    echo Failed to create the scheduled task.
    echo If you see Access Denied, run this file as Administrator.
)

if "%NO_PAUSE%"=="0" pause
exit /b %EXIT_CODE%
