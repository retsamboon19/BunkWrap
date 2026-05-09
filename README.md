[README.md](https://github.com/user-attachments/files/27552765/README.md)
# BunkrWrap v3.1.0

A powerful web-based downloader for Bunkr albums with automatic thumbnail generation, multi-threaded downloads, and advanced gallery management.

---

## 📋 Table of Contents
- [For Complete Beginners](#-for-complete-beginners)
- [Quick Start (Experienced Users)](#-quick-start-experienced-users)
- [Features](#-features)
- [Usage Guide](#-usage-guide)
- [Troubleshooting](#-troubleshooting)
- [Advanced Features](#-advanced-features)

---

## 🎯 For Complete Beginners

**Never used Python before? No problem!** Follow these step-by-step instructions:

### Step 1: Install Python

1. **Download Python**:
   - Go to [python.org/downloads](https://www.python.org/downloads/)
   - Click the big yellow "Download Python" button
   - Download version **3.8 or newer** (3.11+ recommended)

2. **Install Python**:
   - **Windows**: 
     - Run the downloaded installer
     - ⚠️ **IMPORTANT**: Check the box "Add Python to PATH" at the bottom
     - Click "Install Now"
   - **Mac**: 
     - Open the downloaded .pkg file
     - Follow the installation wizard
   - **Linux**: 
     - Python is usually pre-installed
     - If not: `sudo apt install python3 python3-pip` (Ubuntu/Debian)

3. **Verify Installation**:
   - Open Command Prompt (Windows) or Terminal (Mac/Linux)
   - Type: `python --version` and press Enter
   - You should see something like "Python 3.11.x"
   - If that doesn't work, try: `python3 --version`

### Step 2: Download BunkrWrap

1. **Download this project**:
   - Click the green "Code" button on GitHub
   - Select "Download ZIP"
   - Extract the ZIP file to a folder (e.g., `C:\BunkrWrap` or `~/BunkrWrap`)

2. **Open the folder**:
   - **Windows**: Open Command Prompt, then type:
     ```cmd
     cd C:\BunkrWrap
     ```
     (Replace with your actual folder path)
   - **Mac/Linux**: Open Terminal, then type:
     ```bash
     cd ~/BunkrWrap
     ```
     (Replace with your actual folder path)

### Step 3: Install Required Software

1. **Install Core Requirements** (Required - the app won't work without these):
   ```bash
   pip install flask requests beautifulsoup4
   ```
   
   If that doesn't work, try:
   ```bash
   python -m pip install flask requests beautifulsoup4
   ```
   
   Or on Mac/Linux:
   ```bash
   pip3 install flask requests beautifulsoup4
   ```

2. **Install Optional Features** (Recommended - adds thumbnails and better compatibility):
   ```bash
   pip install pillow playwright
   playwright install chromium
   ```
   
   - **Pillow**: Enables thumbnail generation for images
   - **Playwright**: Handles JavaScript-heavy pages (some Bunkr pages need this)

### Step 4: Start BunkrWrap

**Windows Users** (Easiest):
- Double-click `start_server.bat` in the BunkrWrap folder
- Your browser should open automatically to `http://localhost:5000`

**Mac/Linux Users** (or Windows alternative):
1. Open Terminal/Command Prompt in the BunkrWrap folder
2. Type:
   ```bash
   python server.py
   ```
3. Open your web browser and go to: `http://localhost:5000`

### Step 5: Download Your First Album

1. Find a Bunkr album URL (looks like: `https://bunkr.cr/a/XXXXXXXX`)
2. Paste it into the text box at the top
3. Click the blue **DOWNLOAD** button
4. Watch the progress in real-time!
5. Your files will be saved in the `Downloads` folder

### Step 6: Stop the Server

**Windows**:
- Double-click `stop_server_ps.bat` (most reliable)
- Or double-click `stop_server.bat`

**Mac/Linux**:
- Press `Ctrl+C` in the Terminal window where the server is running

---

## 🚀 Quick Start (Experienced Users)

### 1. Install Dependencies
```bash
# Required
pip install flask requests beautifulsoup4

# Optional but recommended
pip install pillow playwright
playwright install chromium
```

### 2. Start Server
**Windows:**
```cmd
start_server.bat
```

**Linux/Mac:**
```bash
python server.py
```

### 3. Open Browser
Navigate to: `http://localhost:5000`

### 4. Download
1. Paste a Bunkr album URL
2. Adjust thread settings (Images: 5, Videos: 2)
3. Click DOWNLOAD
4. Watch progress in real-time

---

## ✨ Features

### Core Features
- **🚀 Multi-threaded Downloads**: Download multiple files simultaneously
  - Separate thread controls for images (1-20 threads) and videos (1-20 threads)
  - Optimized defaults: 5 threads for images, 2 for videos
  - Prevents rate limiting while maximizing speed

- **📊 Real-time Progress Tracking**: 
  - Live download speed graphs
  - Individual file progress bars
  - Success/failure counters
  - Estimated time remaining

- **🖼️ Automatic Thumbnail Generation**:
  - Creates lightweight thumbnails (300px, 85% quality)
  - 10-50x faster gallery loading
  - Works for both images and videos (requires ffmpeg)
  - Reduces memory usage by 80%

- **💾 Smart Download Management**:
  - Automatically skips already-downloaded files
  - Resume interrupted downloads
  - Retry failed downloads with one click
  - Handles paginated albums (multi-page albums)

### Gallery Features
- **🎨 Advanced Gallery Management**:
  - Drag-and-drop files between albums
  - Create nested sub-albums
  - Rename albums directly in the UI
  - Delete files with keyboard shortcuts (Delete key)
  - Multi-select with Shift+Click and Ctrl+Click

- **🔍 Filtering & Sorting**:
  - Filter by file type (All, Images, Videos, Archives)
  - Sort by name, date, or size
  - Quick search through albums

- **🖱️ Keyboard Shortcuts**:
  - `Ctrl+A`: Select all files in current album
  - `Shift+Click`: Select range of files
  - `Ctrl+Click`: Toggle individual file selection
  - `Delete`: Delete selected files
  - `F1`: Open help documentation

### Advanced Features
- **📦 Archive Extraction**:
  - Auto-extract ZIP, RAR, 7z, TAR files
  - Organizes extracted files into sub-albums
  - Preserves source links for extracted files

- **📜 Download History**:
  - Tracks all downloads with timestamps
  - Shows file counts and source URLs
  - Prevents duplicate downloads

- **🔗 Source Link Tracking**:
  - Every file remembers its source URL
  - Click link icons to revisit original pages
  - Useful for re-downloading or sharing

- **⚡ Browser Pool**:
  - Pre-warmed headless browsers for JavaScript-heavy pages
  - Handles dynamic content automatically
  - Visual indicator shows browser availability

- **🛡️ Error Handling**:
  - Automatic retry with exponential backoff
  - Detailed error messages
  - Separate failed files list for easy retry

---

## 📁 Project Structure

```
BunkrWrap/
├── server.py              # Main Flask backend (Python)
├── index.html             # Web UI (HTML/CSS/JavaScript)
├── .bunkrwrap_history.json # Download history (auto-created)
│
├── start_server.bat       # Windows launcher (double-click to start)
├── stop_server_ps.bat     # Stop server - PowerShell (most reliable)
├── stop_server.bat        # Stop server - Batch script
├── stop_server.ps1        # PowerShell stop script
│
├── Downloads/             # Your downloaded files (auto-created)
│   └── [Album Name]/      # Each album gets its own folder
│       ├── .bunkrinfo     # Album metadata (source URLs)
│       └── [files]        # Your downloaded media files
│
├── Thumbnails/            # Generated thumbnails (auto-created)
│   └── [Album Name]/      # Mirrors Downloads structure
│
├── documentation/         # Documentation folder
│   ├── README.md          # This file
│   ├── CHANGELOG.md       # Version history
│   ├── HELP.md            # Comprehensive user guide
│   ├── docs/              # Additional guides
│   └── scripts/           # Utility scripts
│
└── backup/                # Backup versions (optional)
```

---

## 📖 Usage Guide

### Basic Download Workflow

1. **Start the server** (see Step 4 in Beginners Guide)
2. **Paste album URL** in the text box at the top
3. **Adjust settings** (optional):
   - Image threads: 1-20 (default: 5)
   - Video threads: 1-20 (default: 2)
4. **Click DOWNLOAD** button
5. **Monitor progress** in the log panel
6. **View files** in the Gallery tab

### Optimal Thread Settings

**Fast connection (100+ Mbps)**:
- Images: 8-10 threads
- Videos: 3-4 threads

**Normal connection (25-100 Mbps)**:
- Images: 5 threads (default)
- Videos: 2 threads (default)

**Slow connection or rate-limited**:
- Images: 2-3 threads
- Videos: 1 thread
- Wait 5-10 minutes between large downloads

### Gallery Management

**Organize files**:
- Drag files from one album to another
- Drag files to "DROP HERE → NEW ALBUM" to create sub-albums
- Right-click album names to rename or delete

**Multi-select files**:
- `Ctrl+A`: Select all files in current album
- `Shift+Click`: Select range from last clicked to current
- `Ctrl+Click`: Add/remove individual files from selection
- `Delete`: Delete all selected files

**Filter and sort**:
- Use toolbar buttons to filter by type (All/Images/Videos/Archives)
- Use dropdown to sort by name, date, or size

### Handling Failed Downloads

1. Check the **FAILED** column in the log panel
2. Click **RETRY** button next to individual files, or
3. Click **RETRY FAILED** button to retry all failed files
4. If still failing:
   - Lower thread counts
   - Wait 5-10 minutes (rate limiting)
   - Check if album is still accessible

---

## 🛠️ Troubleshooting

### Common Issues

#### "Download won't start" or "No files found"
**Possible causes**:
- Album is private or deleted
- Bunkr is blocking requests
- Network connection issues

**Solutions**:
1. Verify the album URL is accessible in your browser
2. Check browser console (press F12) for errors
3. Try lowering thread counts
4. Wait 5-10 minutes if you've been downloading heavily
5. Install Playwright for better compatibility:
   ```bash
   pip install playwright
   playwright install chromium
   ```

#### "Server won't stop" or "Port 5000 already in use"
**Solutions**:
1. **Windows**: Run `stop_server_ps.bat` (most reliable)
2. **Alternative**: Run `stop_server.bat`
3. **Manual**: Open Task Manager, find `python.exe` running `server.py`, end task
4. See `documentation/docs/SERVER_SHUTDOWN_GUIDE.md` for detailed instructions

#### "Thumbnails not generating"
**For images**:
```bash
pip install pillow
```
Then click "GENERATE THUMBNAILS" in Settings tab

**For videos**:
- Install ffmpeg:
  - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html), add to PATH
  - **Mac**: `brew install ffmpeg`
  - **Linux**: `sudo apt install ffmpeg`

#### "Rate limiting" or "Server unavailable (429/503)"
**Solutions**:
1. Lower thread counts (Images: 2-3, Videos: 1)
2. Wait 5-10 minutes before retrying
3. Use the **RETRY FAILED** button after waiting
4. Avoid downloading multiple large albums back-to-back

#### "Missing source links for extracted ZIP files"
**Solution**:
```bash
python documentation/scripts/fix_extracted_metadata.py
```
This fixes metadata for files extracted from archives.

#### "Python not found" or "pip not found"
**Solutions**:
1. Reinstall Python and check "Add Python to PATH" during installation
2. Try using `python3` and `pip3` instead of `python` and `pip`
3. On Windows, try: `py -m pip install <package>`

### Getting Help

1. **In-app help**: Press `F1` or click the HELP tab
2. **Detailed guides**: Check `documentation/docs/` folder
3. **Troubleshooting**: See `documentation/docs/TROUBLESHOOTING.md`
4. **Server shutdown**: See `documentation/docs/SERVER_SHUTDOWN_GUIDE.md`

---

## 🔧 Advanced Features

### Utility Scripts

Located in `documentation/scripts/` folder:

**bunkrwrap.py** - CLI version for terminal use:
```bash
python documentation/scripts/bunkrwrap.py <album-url>
```

**fix_extracted_metadata.py** - Fix source links for extracted ZIP files:
```bash
python documentation/scripts/fix_extracted_metadata.py
```

**test_album_fetch.py** - Test if an album URL is accessible:
```bash
python documentation/scripts/test_album_fetch.py
```

**stop_server.py** - Cross-platform server shutdown:
```bash
python documentation/scripts/stop_server.py
```

### Preview Mode

Before downloading, you can preview album contents:
1. Paste album URL
2. Click **PREVIEW** button (purple)
3. View file list with sizes
4. Download individual files or entire album

### Diagnostic Export

If you encounter issues:
1. Click **EXPORT DIAGNOSTIC** in Settings tab
2. Saves detailed report with:
   - Browser and system info
   - Python version and dependencies
   - Job details and logs
   - Failed file information
3. Share this file when reporting issues

---

## ⚙️ System Requirements

### Minimum Requirements
- **Python**: 3.8 or newer (3.11+ recommended)
- **RAM**: 2GB minimum, 4GB recommended
- **Disk Space**: Depends on downloads (thumbnails use ~1-5% of original size)
- **Browser**: Modern browser (Chrome, Firefox, Edge, Safari)
- **Internet**: Stable connection (speed determines optimal thread count)

### Required Python Packages
```bash
pip install flask requests beautifulsoup4
```

### Optional Python Packages
```bash
# For thumbnails and better compatibility
pip install pillow playwright
playwright install chromium
```

### Optional External Tools
- **ffmpeg**: For video thumbnail generation
  - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html)
  - Mac: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

- **7-Zip**: For better archive extraction (Windows)
  - Download from [7-zip.org](https://www.7-zip.org/)
  - BunkrWrap will auto-detect if installed

---

## 🛡️ Privacy & Safety

- **Local only**: All processing happens on your computer
- **No tracking**: No analytics or telemetry
- **No cloud**: Files are never uploaded anywhere
- **Open source**: All code is visible and auditable

---

## 📝 Tips & Best Practices

### Avoiding Rate Limits
- Use default thread settings (Images: 5, Videos: 2)
- Wait 5-10 minutes between large album downloads
- Lower threads if you see 429/503 errors
- Don't run multiple instances simultaneously

### Organizing Downloads
- Albums are automatically organized into folders
- Use drag-and-drop to reorganize files
- Create sub-albums for better organization
- Rename albums to meaningful names

### Performance Optimization
- Generate thumbnails for faster gallery loading
- Close unused browser tabs while downloading
- Ensure sufficient disk space (check header stats)
- Use SSD for better performance with many small files

### Backup & Maintenance
- The `backup/` folder contains old versions (safe to delete)
- `.bunkrwrap_history.json` tracks download history (don't delete)
- Thumbnails can be regenerated if deleted
- `.bunkrinfo` files track source URLs (don't delete)

---

## 📚 Documentation

- **README.md** (this file) - Overview and installation
- **CHANGELOG.md** - Version history and updates  
- **HELP.md** - Comprehensive user guide (also in app via F1)
- **ESSENTIAL_FILES.md** - What files are safe to delete
- **docs/TROUBLESHOOTING.md** - Common issues and solutions
- **docs/SERVER_SHUTDOWN_GUIDE.md** - Server stop methods
- **docs/ZIP_METADATA_FIX.md** - Fix extracted file links

---

## 🔗 Quick Reference

### Starting the Server
| Platform | Command |
|----------|---------|
| Windows (easy) | Double-click `start_server.bat` |
| Windows (manual) | `python server.py` |
| Mac/Linux | `python server.py` or `python3 server.py` |

### Stopping the Server
| Platform | Method | Reliability |
|----------|--------|-------------|
| Windows | `stop_server_ps.bat` | ⭐⭐⭐⭐⭐ Best |
| Windows | `stop_server.bat` | ⭐⭐⭐⭐ Good |
| All | `Ctrl+C` in terminal | ⭐⭐⭐ OK |
| All | `python documentation/scripts/stop_server.py` | ⭐⭐⭐⭐ Good |

### Essential Commands
```bash
# Install core requirements
pip install flask requests beautifulsoup4

# Install optional features
pip install pillow playwright
playwright install chromium

# Start server
python server.py

# Test album URL
python documentation/scripts/test_album_fetch.py

# Fix ZIP metadata
python documentation/scripts/fix_extracted_metadata.py
```

---

## 📄 License

This project is for educational purposes. Respect content creators and copyright laws.

---

**Version**: 3.1.0  
**Last Updated**: May 7, 2026  
**Python**: 3.8+ required, 3.11+ recommended

---

## 🆘 Need Help?

1. **Press F1** in the app for comprehensive help
2. **Check CHANGELOG.md** for version history and recent fixes
3. **See docs/ folder** for detailed troubleshooting guides
4. **Check browser console** (F12) for error messages
