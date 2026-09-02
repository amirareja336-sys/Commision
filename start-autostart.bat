@echo off
REM Delayed launcher used by the Windows startup task.
REM Opens start.bat, which spawns a permanent titled log console.
timeout /t 20 /nobreak >nul
cd /d "%~dp0"
call "%~dp0start.bat"
