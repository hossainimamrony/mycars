@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo === Find My Cars one-click setup (new device) ===
echo.

set "BOOTSTRAP_PY="
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "BOOTSTRAP_PY=py -3"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "BOOTSTRAP_PY=python"
    ) else (
        echo Python was not found. Install Python 3 first, then run this again.
        pause
        exit /b 1
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating project virtual environment (.venv)...
    call %BOOTSTRAP_PY% -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo Failed to create .venv
        pause
        exit /b 1
    )
)

set "VENV_PY=%cd%\.venv\Scripts\python.exe"
echo Using Python: "%VENV_PY%"

echo Installing Python dependencies...
"%VENV_PY%" -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
)

"%VENV_PY%" -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo Failed to install requirements.
    pause
    exit /b 1
)

echo Installing Playwright Chromium browser...
"%VENV_PY%" -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo Playwright browser install failed.
    pause
    exit /b 1
)

echo Creating scheduled task (every 5 hours)...
call "%~dp0install_find_my_cars_5h_schedule.bat" --no-pause
if %ERRORLEVEL% NEQ 0 (
    echo Scheduled task setup failed.
    pause
    exit /b 1
)

echo.
echo Setup complete.
echo Double-click run_find_my_cars.bat for manual runs.
echo Auto-run task name: FindMyCars_FullRun_Every5Hours
echo.
pause
exit /b 0
