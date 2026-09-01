@echo off
REM start.bat — launch the Reconciliation Console on the LAN.
REM
REM Usage:
REM   start.bat                 # opens a permanent titled log console window
REM   start.bat --inline        # run in the current terminal (no new window)
REM   set RELOAD=1 && start.bat # development auto-reload (also opens a window
REM                             # unless --inline is passed)
REM   set SCENARIO=1.1 && start.bat --inline   # prefer/create that test scenario DB

setlocal EnableDelayedExpansion

REM Default: permanently open a dedicated log console so uvicorn output stays
REM visible after boot/crash. Pass --inline to keep using this terminal.
if /I "%~1"=="--inline" goto :run
start "Reconciliation Console" cmd /k "%~f0" --inline
exit /b 0

:run
if "%PORT%"=="" set PORT=8000
if "%HOST%"=="" set HOST=0.0.0.0
if "%STATIC_IP%"=="" set STATIC_IP=192.168.1.2
if "%SCENARIO%"=="" set SCENARIO=1.1

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

REM ── Dev / test DB selection ──────────────────────────────────
set "SCENARIO_DIR=%SCENARIO:.=_%"
set "TEST_DB_PATH=%APP_DIR%data\test_%SCENARIO_DIR%\test.db"

if defined COMMISSIONS_DB (
    echo Using COMMISSIONS_DB=%COMMISSIONS_DB%
    goto :launch
)

if exist "%TEST_DB_PATH%" (
    set "COMMISSIONS_DB=%TEST_DB_PATH%"
    echo Found test DB at !COMMISSIONS_DB! — starting in test/dev mode.
    goto :launch
)

echo No test DB found at %TEST_DB_PATH%.
set /p DEV_YN=Create a development test DB and dev user for scenario %SCENARIO%? [Y/n]
if /I "%DEV_YN%"=="n" (
    echo Proceeding with default db\commissions.db.
    goto :launch
)
if /I "%DEV_YN%"=="N" (
    echo Proceeding with default db\commissions.db.
    goto :launch
)

echo Creating test DB ^(scenario %SCENARIO%^)...
python "%APP_DIR%tests\test file generator\generate_test_data.py" --scenario %SCENARIO%
if errorlevel 1 (
    echo Generator failed — starting with default DB instead.
    goto :launch
)
set "COMMISSIONS_DB=%TEST_DB_PATH%"
echo Created !COMMISSIONS_DB!
echo Log in as user "dev" with the password printed above, then open /testmode.

:launch
echo.

if "%RELOAD%"=="1" (
    uvicorn backend.main:app --host %HOST% --port %PORT% --reload
) else (
    uvicorn backend.main:app --host %HOST% --port %PORT%
)

echo.
echo Server stopped. Close this window when finished.
endlocal
