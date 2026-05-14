@echo off
setlocal EnableExtensions

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

set "TASK_NAME=FindMyCars_FullRun_Every5Hours"

echo Removing scheduled task: %TASK_NAME%
schtasks /Delete /F /TN "%TASK_NAME%"
if %ERRORLEVEL% NEQ 0 (
    echo Task was not removed. It may not exist, or you may need higher permissions.
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)

echo Task removed.
if "%NO_PAUSE%"=="0" pause
exit /b 0
