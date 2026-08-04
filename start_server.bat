@echo off
setlocal
cd /d "%~dp0"
title BunkrWrap Launcher

if not exist ".venv\Scripts\python.exe" (
    echo BunkrWrap needs to finish its one-time setup first.
    call "%~dp0Install BunkrWrap.bat"
    exit /b %errorlevel%
)

set "PLAYWRIGHT_BROWSERS_PATH=%~dp0tools\playwright"
echo Starting BunkrWrap...
start "BunkrWrap Server" /min "%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000"
echo BunkrWrap is open in your browser.
