# BunkrWrap V5

[![Tests](https://github.com/retsamboon19/BunkWrap/actions/workflows/quality.yml/badge.svg)](https://github.com/retsamboon19/BunkWrap/actions/workflows/quality.yml)
[![Latest release](https://img.shields.io/github/v/release/retsamboon19/BunkWrap?label=download)](https://github.com/retsamboon19/BunkWrap/releases/latest)

BunkrWrap V5 is a local web app for downloading and organizing Bunkr albums. It includes parallel downloads, previews, automatic image and video thumbnails, archive extraction, download history, and gallery management.

### V5.1.0: faster transfers and durable downloads

Downloads now keep album cookies across worker sessions, reuse pooled connections, avoid a redundant size request for every new file, and stop probing an unavailable resolver API after the first few album-wide misses. Larger stream chunks reduce disk and Python overhead, while image/video thumbnails are generated in a separate background pool so transfers can immediately continue.

Interrupted files remain in the persistent queue and resume from their existing byte count after pausing, restarting the app, or reopening the browser. Explicit CDN rate limits still coordinate a short host cooldown, while an isolated broken stream retries only that file instead of unnecessarily slowing the entire album.

No downloader can guarantee that a third-party CDN will provide full speed or accept 10 simultaneous transfers. BunkrWrap does not rotate IP addresses or use proxy networks to bypass server limits.

## New one-click installation (Windows 10/11)

No command line or previous Python installation is required.

1. Open **[BunkrWrap Releases](https://github.com/retsamboon19/BunkWrap/releases/latest)** and download the Windows ZIP under **Assets**.
2. Open the downloaded ZIP and choose **Extract all**.
3. Open the extracted `BunkrWrap-v5.x.x` folder.
4. Double-click **`Install BunkrWrap.bat`**.
5. Wait for **Setup complete**. BunkrWrap will open automatically and a desktop shortcut will be created.

The new installer automatically downloads and configures every dependency and feature:

- Python and an isolated BunkrWrap environment;
- all required Python packages;
- Chromium for JavaScript-heavy Bunkr pages;
- FFmpeg and FFprobe for video thumbnails;
- 7-Zip tools for RAR and 7z extraction;
- a **BunkrWrap** desktop shortcut.

The first installation can take several minutes and requires an internet connection. If Windows asks for network access, allow Python and PowerShell. Setup is safe to run again if it is interrupted.

After installation, open BunkrWrap using the desktop shortcut or by double-clicking `start_server.bat`.

> BunkrWrap runs only on your computer at `http://127.0.0.1:5000`. It does not include analytics or upload your files.

### If Windows Smart App Control blocks the installer

`Install BunkrWrap.bat` is an open-source batch file and is not digitally signed. Windows 11 Smart App Control may therefore show **“Smart App Control blocked a file that may be unsafe.”**

First try unblocking only the downloaded ZIP:

1. Delete the folder you already extracted.
2. Right-click the downloaded `BunkWrap-main.zip` and select **Properties**.
3. On the **General** tab, select **Unblock**, then **Apply** and **OK**.
4. Extract the ZIP again and run `Install BunkrWrap.bat`.

Only unblock files downloaded from this official repository. You can scan the ZIP with Microsoft Defender before continuing.

If Smart App Control still blocks the installer, you may need to temporarily disable it:

1. Open **Windows Security**.
2. Select **App & browser control**.
3. Select **Smart App Control settings**.
4. Set Smart App Control to **Off**, then run `Install BunkrWrap.bat` again.

Disabling Smart App Control reduces Windows protection against unknown applications. Re-enable it after installation if your Windows version offers that option. Microsoft does not currently provide a per-app exception for Smart App Control. See [Microsoft's Smart App Control FAQ](https://support.microsoft.com/en-US/Windows/Security/Threat-Malware-Protection/smart-app-control-frequently-asked-questions) for current details.

## Updating

Open the [release list](https://github.com/retsamboon19/BunkWrap/releases), choose the version you want, download its Windows ZIP, extract it over the existing BunkrWrap folder, and run **`Install BunkrWrap.bat`** again. The installer updates or repairs dependencies without removing your `Downloads`, `Thumbnails`, persistent queue, or download history.

Every version tag automatically creates a separate GitHub release and attached Windows ZIP, so older versions remain available from the same release list.

## Manual setup (advanced users)

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\tools\playwright"
.venv\Scripts\python -m playwright install chromium
```

FFmpeg and 7-Zip are optional in a manual setup. Put `ffmpeg.exe` and `ffprobe.exe` in `tools/ffmpeg`, and `7za.exe` in `tools/7zip`, or install them on your system PATH.

## Using BunkrWrap

1. Start BunkrWrap and paste a Bunkr album URL.
2. Choose **Preview** to inspect the album or **Download** to begin.
3. Follow progress in the activity panel.
4. Open the Gallery to view, sort, move, rename, or remove downloaded files.

Downloads are saved under `Downloads/<album name>`. Generated previews are stored under `Thumbnails`.

## Stopping the app

Double-click `stop_server_ps.bat`. You can start it again from the desktop shortcut.

## Troubleshooting

- **Setup stopped:** Check the internet connection, temporarily allow Python/PowerShell through security software, then run `Install BunkrWrap.bat` again. Setup is safe to repeat.
- **Browser did not open:** Visit [http://127.0.0.1:5000](http://127.0.0.1:5000).
- **Port 5000 is already in use:** Run `stop_server_ps.bat`, then start BunkrWrap again.
- **A download cannot be resolved:** Confirm that the album works in a normal browser, wait a few minutes, then use **Retry Failed**. Partial files resume instead of restarting.
- **Video thumbnails are missing:** Run `Install BunkrWrap.bat` again so FFmpeg can be repaired.
- **RAR/7z extraction is unavailable:** Run the installer again and check that `tools/7zip/7za.exe` exists afterward.

## Files and privacy

Do not upload these user-specific folders/files when reporting an issue:

- `Downloads/`
- `Thumbnails/`
- `.bunkrwrap_history.json`

They are excluded from Git by default. BunkrWrap is intended for content you are authorized to download; users are responsible for following applicable laws and site terms.

## License

No license file is currently included. All rights remain with the repository owner unless a license is added.
