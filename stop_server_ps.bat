@echo off
REM BunkrWrap Server Shutdown (PowerShell Wrapper)
REM This runs the PowerShell script which is more reliable

title BunkrWrap - Stop Server

REM Check if PowerShell is available
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo PowerShell not found. Using fallback method...
    call stop_server.bat
    exit /b
)

REM Run PowerShell script
powershell -ExecutionPolicy Bypass -File "%~dp0stop_server.ps1"

exit /b
