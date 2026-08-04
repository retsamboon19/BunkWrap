# BunkrWrap V5

BunkrWrap V5 is a local web app for downloading and organizing Bunkr albums. It includes parallel downloads, previews, automatic image and video thumbnails, archive extraction, download history, and gallery management.

## New one-click installation (Windows 10/11)

No command line or previous Python installation is required.

1. **[Download BunkrWrap V5](https://github.com/retsamboon19/BunkWrap/archive/refs/heads/main.zip)**.
2. Open the downloaded ZIP and choose **Extract all**.
3. Open the extracted `BunkWrap-main` folder.
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

## Updating

Download the newest V5 ZIP, extract it over the existing BunkrWrap folder, and run **`Install BunkrWrap.bat`** again. The installer updates or repairs the dependencies without removing your `Downloads`, `Thumbnails`, or download history.

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
- **A download cannot be resolved:** Confirm that the album works in a normal browser, lower the thread counts, and retry after a few minutes.
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
