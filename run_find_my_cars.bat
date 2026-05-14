@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

if not exist "find_my_cars_output\logs" mkdir "find_my_cars_output\logs" >nul 2>&1

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"
if not defined TS set "TS=run"
set "LOG_FILE=%cd%\find_my_cars_output\logs\find_my_cars_%TS%.log"

set "EXE_PATH=%cd%\dist\find_my_cars_without_ui.exe"

echo [%date% %time%] Starting Find My Cars full run
echo [%date% %time%] Log file: "%LOG_FILE%"

if exist "%EXE_PATH%" (
    echo [%date% %time%] Running EXE: "%EXE_PATH%"
    "%EXE_PATH%" 1>>"%LOG_FILE%" 2>&1
) else (
    set "VENV_PY=%cd%\.venv\Scripts\python.exe"
    if exist "%VENV_PY%" (
        echo [%date% %time%] Running Python script with project venv: "%VENV_PY%"
        "%VENV_PY%" "%cd%\find_my_cars_without_ui.py" 1>>"%LOG_FILE%" 2>&1
    ) else (
        where py >nul 2>&1
        if %ERRORLEVEL% EQU 0 (
            echo [%date% %time%] Running Python script with: py -3
            py -3 "%cd%\find_my_cars_without_ui.py" 1>>"%LOG_FILE%" 2>&1
        ) else (
            echo [%date% %time%] Running Python script with: python
            python "%cd%\find_my_cars_without_ui.py" 1>>"%LOG_FILE%" 2>&1
        )
    )
)

set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Finished with exit code %EXIT_CODE%
echo [%date% %time%] Log file: "%LOG_FILE%"

if "%NO_PAUSE%"=="0" pause
exit /b %EXIT_CODE%
