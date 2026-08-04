@echo off
setlocal
cd /d "%~dp0"
title BunkrWrap Setup

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo Setup did not finish. Read the message above, then try again.
    pause
    exit /b 1
)

echo.
echo BunkrWrap is ready. Opening it now...
call "%~dp0start_server.bat"
