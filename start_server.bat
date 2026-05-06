@echo off
title BunkrWrap Server

echo Stopping any process running on port 5000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr /R ":5000 "') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting BunkrWrap server...
start "" python server.py
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5000"
echo Server running at http://127.0.0.1:5000
echo Close this window to stop the server.
pause
