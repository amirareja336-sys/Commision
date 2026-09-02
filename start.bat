@echo off
REM start.bat — launch the Reconciliation Console on the LAN.
REM
REM Usage:
REM   start.bat                 # opens a permanent titled log console window
REM   start.bat --inline        # run in the current terminal (no new window)
REM   set RELOAD=1 && start.bat # development auto-reload (also opens a window
REM                             # unless --inline is passed)

setlocal EnableDelayedExpansion

REM Default: permanently open a dedicated log console so uvicorn output stays
REM visible after boot/crash. Pass --inline to keep using this terminal.
if /I "%~1"=="--inline" goto :run
start "Reconciliation Console" cmd /k "%~f0" --inline
exit /b 0

:run
if "%PORT%"=="" set PORT=8080
if "%HOST%"=="" set HOST=0.0.0.0
if "%STATIC_IP%"=="" set STATIC_IP=192.168.1.2

set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"
title Reconciliation Console

if exist "%APP_DIR%venv\Scripts\activate.bat" (
    call "%APP_DIR%venv\Scripts\activate.bat"
)

echo.
echo ==================================================
echo   Reconciliation Console
echo   Local:   http://localhost:%PORT%
echo   Network: http://%STATIC_IP%:%PORT%
echo ==================================================
echo.
echo Log window stays open after the process stops.
echo.

if "%RELOAD%"=="1" (
    uvicorn backend.main:app --host %HOST% --port %PORT% --reload
) else (
    uvicorn backend.main:app --host %HOST% --port %PORT%
)

echo.
echo Server stopped. Close this window when finished.
endlocal
