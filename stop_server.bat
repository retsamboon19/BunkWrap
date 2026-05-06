@echo off
title BunkrWrap - Stop Server

echo ================================================
echo   BunkrWrap Server Shutdown
echo ================================================
echo.

REM Method 1: Find and kill python processes running server.py specifically
echo [1/3] Searching for server.py processes...
set FOUND=0
for /f "tokens=2" %%a in ('wmic process where "name='python.exe' and commandline like '%%server.py%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    echo   ^> Stopping PID %%a (server.py)
    taskkill /F /PID %%a >nul 2>&1
    if !errorlevel! equ 0 (
        set FOUND=1
    )
)

if %FOUND%==0 (
    echo   ^> No server.py process found
)

REM Method 2: Kill any process using port 5000
echo.
echo [2/3] Checking port 5000...
set PORT_FOUND=0
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /R ":5000.*LISTENING"') do (
    echo   ^> Releasing port 5000 (PID %%a)
    taskkill /F /PID %%a >nul 2>&1
    if !errorlevel! equ 0 (
        set PORT_FOUND=1
    )
)

if %PORT_FOUND%==0 (
    echo   ^> Port 5000 is free
)

REM Method 3: Verify port is released
echo.
echo [3/3] Verifying shutdown...
timeout /t 1 /nobreak >nul

netstat -ano 2>nul | findstr /R ":5000.*LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo   ^> WARNING: Port 5000 still in use
    echo   ^> Attempting force release...
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /R ":5000"') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    timeout /t 1 /nobreak >nul
) else (
    echo   ^> Port 5000 is free
)

echo.
echo ================================================
echo   Server stopped successfully
echo ================================================
echo.
echo Press any key to close...
pause >nul
