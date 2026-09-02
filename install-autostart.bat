@echo off
REM Elevates and runs install-autostart.ps1 (static IP + boot task).
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart.ps1"
if %errorlevel% neq 0 (
    echo Install failed.
    pause
    exit /b 1
)
echo.
echo Press any key to close.
pause >nul
