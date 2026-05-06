# BunkrWrap Server Shutdown Script (PowerShell)
# More reliable and faster than batch script

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  BunkrWrap Server Shutdown" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

$stopped = $false

# Method 1: Find and kill python processes running server.py
Write-Host "[1/3] Searching for server.py processes..." -ForegroundColor Yellow

try {
    $serverProcesses = Get-WmiObject Win32_Process -Filter "name='python.exe'" | 
        Where-Object { $_.CommandLine -like "*server.py*" }
    
    if ($serverProcesses) {
        foreach ($proc in $serverProcesses) {
            Write-Host "  > Stopping PID $($proc.ProcessId) (server.py)" -ForegroundColor Green
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped = $true
        }
    } else {
        Write-Host "  > No server.py process found" -ForegroundColor Gray
    }
} catch {
    Write-Host "  > Error checking processes: $_" -ForegroundColor Red
}

# Method 2: Kill any process using port 5000
Write-Host ""
Write-Host "[2/3] Checking port 5000..." -ForegroundColor Yellow

try {
    $connections = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    
    if ($connections) {
        foreach ($conn in $connections) {
            $pid = $conn.OwningProcess
            Write-Host "  > Releasing port 5000 (PID $pid)" -ForegroundColor Green
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            $stopped = $true
        }
    } else {
        Write-Host "  > Port 5000 is free" -ForegroundColor Gray
    }
} catch {
    # Fallback to netstat if Get-NetTCPConnection fails
    $netstatOutput = netstat -ano | Select-String ":5000.*LISTENING"
    if ($netstatOutput) {
        foreach ($line in $netstatOutput) {
            if ($line -match "\s+(\d+)$") {
                $pid = $matches[1]
                Write-Host "  > Releasing port 5000 (PID $pid)" -ForegroundColor Green
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        }
    } else {
        Write-Host "  > Port 5000 is free" -ForegroundColor Gray
    }
}

# Method 3: Verify shutdown
Write-Host ""
Write-Host "[3/3] Verifying shutdown..." -ForegroundColor Yellow
Start-Sleep -Milliseconds 500

try {
    $stillListening = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    
    if ($stillListening) {
        Write-Host "  > WARNING: Port 5000 still in use" -ForegroundColor Red
        Write-Host "  > Attempting force release..." -ForegroundColor Yellow
        
        foreach ($conn in $stillListening) {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        
        Start-Sleep -Milliseconds 500
        $stillListening = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
        
        if ($stillListening) {
            Write-Host "  > ERROR: Could not release port 5000" -ForegroundColor Red
        } else {
            Write-Host "  > Port 5000 is now free" -ForegroundColor Green
        }
    } else {
        Write-Host "  > Port 5000 is free" -ForegroundColor Green
    }
} catch {
    # Fallback verification
    $netstatCheck = netstat -ano | Select-String ":5000.*LISTENING"
    if ($netstatCheck) {
        Write-Host "  > WARNING: Port 5000 may still be in use" -ForegroundColor Red
    } else {
        Write-Host "  > Port 5000 is free" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
if ($stopped) {
    Write-Host "  Server stopped successfully" -ForegroundColor Green
} else {
    Write-Host "  No server was running" -ForegroundColor Yellow
}
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press any key to close..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
