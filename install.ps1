[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir = Join-Path $AppRoot "tools"
$VenvDir = Join-Path $AppRoot ".venv"
$PythonVersion = "3.13.14"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-WorkingPython {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += @{ File = $py.Source; Args = @("-3") } }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates += @{ File = $python.Source; Args = @() } }
    $localPython = Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe"
    if (Test-Path $localPython) { $candidates += @{ File = $localPython; Args = @() } }

    foreach ($candidate in $candidates) {
        try {
            $checkArgs = @($candidate.Args) + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)")
            & $candidate.File @checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}

function Install-Python {
    Write-Step "Installing a private-compatible Python runtime"
    $archSuffix = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "-arm64" } else { "-amd64" }
    $installerName = "python-$PythonVersion$archSuffix.exe"
    $installerUrl = "https://www.python.org/ftp/python/$PythonVersion/$installerName"
    $installerPath = Join-Path $env:TEMP $installerName
    Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $installerPath
    $process = Start-Process -FilePath $installerPath -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "Include_pip=1", "Include_launcher=1",
        "Include_test=0", "PrependPath=1", "Shortcuts=0"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Python installer returned error code $($process.ExitCode)."
    }
    Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
}

function Install-FFmpeg {
    $ffmpegDir = Join-Path $ToolsDir "ffmpeg"
    $ffmpegExe = Join-Path $ffmpegDir "ffmpeg.exe"
    $ffprobeExe = Join-Path $ffmpegDir "ffprobe.exe"
    if ((Test-Path $ffmpegExe) -and (Test-Path $ffprobeExe)) {
        Write-Host "FFmpeg is already ready."
        return
    }

    Write-Step "Adding video thumbnail support (FFmpeg)"
    $zipPath = Join-Path $env:TEMP "bunkrwrap-ffmpeg.zip"
    $extractPath = Join-Path $env:TEMP "bunkrwrap-ffmpeg"
    if (Test-Path $extractPath) { Remove-Item -LiteralPath $extractPath -Recurse -Force }
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
    $downloadedFfmpeg = Get-ChildItem -Path $extractPath -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    $downloadedFfprobe = Get-ChildItem -Path $extractPath -Filter "ffprobe.exe" -Recurse | Select-Object -First 1
    if (-not $downloadedFfmpeg -or -not $downloadedFfprobe) { throw "FFmpeg files were not found in the downloaded package." }
    New-Item -ItemType Directory -Force -Path $ffmpegDir | Out-Null
    Copy-Item -LiteralPath $downloadedFfmpeg.FullName -Destination $ffmpegExe -Force
    Copy-Item -LiteralPath $downloadedFfprobe.FullName -Destination $ffprobeExe -Force
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $extractPath -Recurse -Force -ErrorAction SilentlyContinue
}

function Install-7Zip {
    $sevenZipDir = Join-Path $ToolsDir "7zip"
    $sevenZipExe = Join-Path $sevenZipDir "7za.exe"
    if (Test-Path $sevenZipExe) {
        Write-Host "7-Zip is already ready."
        return
    }

    Write-Step "Adding RAR and 7z archive support"
    New-Item -ItemType Directory -Force -Path $sevenZipDir | Out-Null
    $sevenZr = Join-Path $env:TEMP "7zr.exe"
    $extraArchive = Join-Path $env:TEMP "7zip-extra.7z"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.7-zip.org/a/7zr.exe" -OutFile $sevenZr
    $downloadPage = (Invoke-WebRequest -UseBasicParsing -Uri "https://www.7-zip.org/download.html").Content
    $match = [regex]::Match($downloadPage, 'href="a/(7z\d+-extra\.7z)"')
    if (-not $match.Success) { throw "Could not locate the current 7-Zip command-line package." }
    Invoke-WebRequest -UseBasicParsing -Uri ("https://www.7-zip.org/a/" + $match.Groups[1].Value) -OutFile $extraArchive
    & $sevenZr x $extraArchive "-o$sevenZipDir" -y | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $sevenZipExe)) { throw "7-Zip extraction failed." }
    Remove-Item -LiteralPath $sevenZr -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $extraArchive -Force -ErrorAction SilentlyContinue
}

try {
    Write-Host "BunkrWrap one-click setup" -ForegroundColor Green
    Write-Host "This may take several minutes on the first run."
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

    $python = Get-WorkingPython
    if (-not $python) {
        Install-Python
        $python = Get-WorkingPython
    }
    if (-not $python) { throw "Python was installed but could not be started. Restart Windows and run this setup again." }

    Write-Step "Creating BunkrWrap's isolated environment"
    if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
        & $python.File @($python.Args) -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Could not create the app environment." }
    }
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"

    Write-Step "Installing all BunkrWrap features"
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not update the package installer." }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $AppRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install BunkrWrap's Python packages." }

    Write-Step "Installing the private browser engine"
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ToolsDir "playwright"
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Could not install Chromium for BunkrWrap." }

    Install-FFmpeg
    Install-7Zip

    Write-Step "Creating a desktop shortcut"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "BunkrWrap.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $AppRoot "start_server.bat"
    $shortcut.WorkingDirectory = $AppRoot
    $shortcut.Description = "Open BunkrWrap"
    $shortcut.Save()

    Set-Content -LiteralPath (Join-Path $AppRoot ".install-complete") -Value (Get-Date -Format "o")
    Write-Host ""
    Write-Host "Setup complete! A BunkrWrap shortcut is now on your desktop." -ForegroundColor Green
    exit 0
} catch {
    Write-Host ""
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Check your internet connection and run 'Install BunkrWrap.bat' again."
    exit 1
}
