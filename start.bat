@echo off
REM start.bat — launch the Reconciliation Console and expose it on the LAN.
REM
REM Usage:
REM   start.bat           # defaults: port 8000, auto-detects LAN IP
REM   set PORT=9000 && start.bat  # use a different port

setlocal EnableDelayedExpansion

REM Default port if not set
if "%PORT%"=="" set PORT=8000

REM Get the directory where this script lives
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

REM Activate venv if it exists
if exist "%APP_DIR%venv\Scripts\activate.bat" (
    call "%APP_DIR%venv\Scripts\activate.bat"
)

REM Get LAN IP address
set "LAN_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
    for /f "tokens=1" %%b in ("%%a") do (
        if "!LAN_IP!"=="" set "LAN_IP=%%b"
    )
)

echo.
echo ==================================================
echo   Reconciliation Console
echo   Local:   http://localhost:%PORT%
if not "%LAN_IP%"=="" (
    echo   Network: http://%LAN_IP%:%PORT%  ^<- share this with other PCs
)
echo ==================================================
echo.

REM Start the server
uvicorn backend.main:app --host 0.0.0.0 --port %PORT% --reload

endlocal
