@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=py -3"
) else (
    set "PY_CMD=python"
)

echo Using Python command: %PY_CMD%
echo Installing build dependency: PyInstaller
call %PY_CMD% -m pip install --upgrade pyinstaller
if %ERRORLEVEL% NEQ 0 (
    echo Failed to install PyInstaller.
    pause
    exit /b 1
)

echo Building dist\find_my_cars_without_ui.exe ...
call %PY_CMD% -m PyInstaller --noconfirm --clean --onefile --name find_my_cars_without_ui --collect-all playwright find_my_cars_without_ui.py
if %ERRORLEVEL% NEQ 0 (
    echo EXE build failed.
    pause
    exit /b 1
)

echo.
echo Build completed:
echo %cd%\dist\find_my_cars_without_ui.exe
echo.
pause
exit /b 0
