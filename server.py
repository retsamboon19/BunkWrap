#!/usr/bin/env python3
"""
BunkrWrap Server v2 — Flask backend for the web UI
Run: python server.py
Then open: http://localhost:5000

Requires: pip install flask requests beautifulsoup4 pillow playwright
          playwright install chromium
Optional:  ffmpeg/ffprobe and 7-Zip in tools/ or PATH
"""

import re
import sys
import time
import json
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import subprocess
import math
import base64
import html
import random
import zipfile
import tarfile
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote, urlunparse, parse_qs, urlencode
from collections import deque

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, Response

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

_BUNDLED_FFMPEG_DIR = Path(__file__).resolve().parent / "tools" / "ffmpeg"
_BUNDLED_FFMPEG = _BUNDLED_FFMPEG_DIR / "ffmpeg.exe"
_BUNDLED_FFPROBE = _BUNDLED_FFMPEG_DIR / "ffprobe.exe"
FFMPEG_CMD = str(_BUNDLED_FFMPEG) if _BUNDLED_FFMPEG.is_file() else (shutil.which("ffmpeg") or "ffmpeg")
FFPROBE_CMD = str(_BUNDLED_FFPROBE) if _BUNDLED_FFPROBE.is_file() else (shutil.which("ffprobe") or "ffprobe")
FFMPEG_OK = Path(FFMPEG_CMD).is_file() and Path(FFPROBE_CMD).is_file()

def _find_7zip():
    bundled = Path(__file__).resolve().parent / "tools" / "7zip" / "7za.exe"
    if bundled.is_file():
        return str(bundled)
    for cmd in ("7z", "7za", "7zz"):
        p = shutil.which(cmd)
        if p:
            return p
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None

_7Z_CMD   = _find_7zip()
SEVENZIP_OK = _7Z_CMD is not None

app = Flask(__name__)

# ─── Task 21.2: File Operation Error Classes ──────────────────────────────────

class FileOperationError(Exception):
    """Base class for file operation errors."""
    pass

class FileNotFoundError(FileOperationError):
    """Source file does not exist."""
    pass

class PermissionError(FileOperationError):
    """Insufficient permissions for file operation."""
    pass

class DiskSpaceError(FileOperationError):
    """Insufficient disk space for operation."""
    pass

class AlbumNotFoundError(FileOperationError):
    """Target album directory does not exist."""
    pass

# ─── Config ────────────────────────────────────────────────────────────────────

VERSION = "5.0.2"

DOWNLOADS_DIR = Path("./Downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

THUMBS_DIR = Path("./Thumbnails")
THUMBS_DIR.mkdir(exist_ok=True)

HISTORY_FILE = Path("./.bunkrwrap_history.json")

# Thumbnail cache settings
THUMB_MAX_SIZE = 300  # Max dimension for thumbnails
THUMB_QUALITY = 85    # JPEG quality for thumbnails

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://bunkr.cr/",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts"}
ZIP_EXTS   = {".zip", ".rar", ".7z", ".tar", ".gz", ".tar.gz", ".tar.bz2"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | ZIP_EXTS

DEFAULT_CONCURRENCY_IMAGES = 5
DEFAULT_CONCURRENCY_VIDEOS = 10
MAX_CONCURRENCY_IMAGES = 10
MAX_CONCURRENCY_VIDEOS = 10
CHUNK_SIZE = 1024 * 512  # 512 KB

BASE_RETRY_DELAY = 2
DEFAULT_MAX_RETRIES = 6
PLAYWRIGHT_GOTO_TIMEOUT_MS = 15000
PLAYWRIGHT_SELECTOR_TIMEOUT_MS = 5000

# Preview: parallel workers (static HTML only — no Playwright per row)
PREVIEW_MAX_WORKERS = 12

CDN_HOSTS = re.compile(
    r'(?:cdn\d*|i\d*|media\d*|delivery\d*|files?\d*|storage\d*|s\d*)\.'
    r'(?:bunkr|bunkrr|bunkr\.ru|bunkr\.black|bunkr\.site|bunkr\.ws|bunkr\.ph|'
    r'bunkr\.cr|bunkr\.is|bunkr\.to|bunkr\.cat|bunkr\.black|bunkr\.fi|'
    r'bunkrr\.su|bunkr\.ws|big-tits\.ru|milkshake\.pics|taquito\.pics|'
    r'coffeelocal\.pics|kebab\.pics|burger\.pics|pizza\.pizza|'
    r'nakedslut\.pics|join\.bitwarden\.com|remilia\.org)',
    re.I
)

# ─── Job store ─────────────────────────────────────────────────────────────────

jobs = {}
jobs_lock = threading.Lock()
bunkrinfo_lock = threading.Lock()

# ─── Global Thread Pools ───────────────────────────────────────────────────────
# Single resolver pool handles all URL resolutions concurrently.
# Two adaptive gates enforce per-type concurrency independently.


class AdaptiveDownloadGate:
    """Concurrency gate using additive recovery and multiplicative decrease."""

    def __init__(self, limit, recovery_successes=2):
        self.configured_limit = max(1, int(limit))
        self.current_limit = self.configured_limit
        self.active = 0
        self.success_streak = 0
        self.recovery_successes = max(1, int(recovery_successes))
        self._condition = threading.Condition()

    def acquire(self):
        with self._condition:
            while self.active >= self.current_limit:
                self._condition.wait()
            self.active += 1
        return True

    def release(self):
        with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify_all()

    def record_throttle(self):
        with self._condition:
            old_limit = self.current_limit
            self.current_limit = max(1, self.current_limit // 2)
            self.success_streak = 0
            if self.current_limit != old_limit:
                self._condition.notify_all()
            return self.current_limit

    def record_success(self):
        with self._condition:
            if self.current_limit >= self.configured_limit:
                self.success_streak = 0
                return self.current_limit
            self.success_streak += 1
            if self.success_streak >= self.recovery_successes:
                self.current_limit += 1
                self.success_streak = 0
                self._condition.notify_all()
            return self.current_limit

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

global_resolver_pool  = None   # ThreadPoolExecutor(image_threads + video_threads)
global_image_semaphore = None  # AdaptiveDownloadGate(image_threads)
global_video_semaphore = None  # AdaptiveDownloadGate(video_threads)
_current_image_threads = 0
_current_video_threads = 0
global_pool_lock = threading.Lock()
_session_local = threading.local()

# Bunkr's CDN rate limits all files on the same host together.  Coordinate
# request starts and cooldowns across every worker instead of letting each
# thread retry independently and create a thundering herd.
_cdn_lock = threading.Lock()
_cdn_cooldown_until = {}
_cdn_next_request_at = {}
_cdn_rate_strikes = {}
CDN_REQUEST_START_SPACING = 0.35
CDN_MAX_COOLDOWN = 90


def _thread_session():
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        _session_local.session = session
    return session


def _cdn_host(url):
    return (urlparse(url).hostname or "").lower()


def wait_for_cdn_slot(url, job=None, file_index=None):
    """Stagger requests and honor a host-wide cooldown after throttling."""
    host = _cdn_host(url)
    if not host:
        return
    with _cdn_lock:
        now = time.monotonic()
        ready_at = max(
            now,
            _cdn_cooldown_until.get(host, 0),
            _cdn_next_request_at.get(host, 0),
        )
        _cdn_next_request_at[host] = ready_at + CDN_REQUEST_START_SPACING
    delay = max(0, ready_at - time.monotonic())
    if delay > 0:
        if job is not None and delay >= 2:
            job["log"].append({
                "type": "warn",
                "msg": f"[{file_index}] CDN cooldown: waiting {math.ceil(delay)}s before resuming...",
            })
        time.sleep(delay)


def register_cdn_throttle(url, retry_after=None, severe=False, adaptive_gate=None):
    """Apply exponential, shared backoff for 429/503 and broken streams."""
    host = _cdn_host(url)
    if not host:
        return 5
    with _cdn_lock:
        strikes = min(6, _cdn_rate_strikes.get(host, 0) + 1)
        _cdn_rate_strikes[host] = strikes
        base = 15 if severe else 5
        calculated = min(CDN_MAX_COOLDOWN, base * (2 ** (strikes - 1)))
        delay = max(float(retry_after or 0), calculated) + random.uniform(0.5, 2.0)
        until = time.monotonic() + delay
        _cdn_cooldown_until[host] = max(_cdn_cooldown_until.get(host, 0), until)
    if adaptive_gate is not None:
        adaptive_gate.record_throttle()
    return math.ceil(delay)


def register_cdn_success(url, adaptive_gate=None):
    host = _cdn_host(url)
    if not host:
        return
    with _cdn_lock:
        strikes = _cdn_rate_strikes.get(host, 0)
        if strikes > 0:
            _cdn_rate_strikes[host] = strikes - 1
    if adaptive_gate is not None:
        adaptive_gate.record_success()

# ─── Persistent browser pool ────────────────────────────────────────────────────
POOL_SIZE = 2  # browser resolvers are expensive; independent of download streams
POOL_MAX  = 15

class BrowserPool:
    def __init__(self):
        self._queue  = queue.Queue()
        self._target = 0   # desired live-browser count
        self._alive  = 0   # browsers actually created (in-queue or in-use)
        self._lock   = threading.Lock()
        self.ready   = False

    def _spawn_one(self, label=""):
        """Each browser MUST run in its own thread — sync_playwright() creates an asyncio
        event loop internally, and only one loop is allowed per thread."""
        try:
            pw      = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            ctx     = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page    = ctx.new_page()
            try:
                page.goto("https://bunkr.cr", timeout=20000, wait_until="domcontentloaded")
            except Exception:
                pass
            with self._lock:
                self._alive += 1
            self._queue.put((pw, browser, page))
            print(f"  [BrowserPool] Browser ready ({label})")
        except Exception as e:
            print(f"  [BrowserPool] Browser failed ({label}): {e}")

    def start(self, size):
        if not PLAYWRIGHT_OK or size < 1:
            return
        with self._lock:
            self._target = size
        print(f"  [BrowserPool] Starting {size} browser(s) — warming up on bunkr.cr...")
        threads = [
            threading.Thread(target=self._spawn_one, args=(f"{i + 1}/{size}",), daemon=True)
            for i in range(size)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.ready = True
        print(f"  [BrowserPool] {self._alive}/{size} browser(s) standing by")

    def resize(self, new_size):
        if not PLAYWRIGHT_OK:
            return
        new_size = max(1, min(POOL_MAX, new_size))
        with self._lock:
            old_target   = self._target
            self._target = new_size
        delta = new_size - old_target
        if delta > 0:
            for i in range(delta):
                threading.Thread(
                    target=self._spawn_one, args=(f"+{i + 1}",), daemon=True
                ).start()
        # Shrinking: fetch()'s finally block discards slots when alive > target

    def fetch(self, url):
        try:
            pw, browser, page = self._queue.get(timeout=90)
        except queue.Empty:
            return None
        html    = None
        discard = False
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass  # domcontentloaded timeout — page may still have usable content
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            try:
                page.wait_for_selector(
                    "video, source, a[href$='.zip'], a[href$='.mp4'], a[href$='.rar']",
                    timeout=5000,
                )
            except Exception:
                pass
            try:
                js_url = page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        if (v && v.src && v.src.startsWith('http')) return v.src;
                        const s = document.querySelector('source[src]');
                        if (s && s.src && s.src.startsWith('http')) return s.src;
                        try {
                            const nd = document.getElementById('__NEXT_DATA__');
                            if (nd) {
                                const m = nd.textContent.match(/"(https?:[^\\"]++\\.(?:mp4|mkv|mov|webm|avi|m4v|jpg|jpeg|png|gif|webp)(?:\\?[^\\"]*)?)"/i);
                                if (m) return m[1];
                            }
                        } catch(e) {}
                        const allMedia = Array.from(document.querySelectorAll('[src]')).map(el => el.src || el.getAttribute('src')).filter(s => s && /\\.(mp4|mkv|mov|webm|avi|m4v|jpg|jpeg|png|gif|webp)(\\?|$)/i.test(s));
                        return allMedia[0] || null;
                    }
                """)
                if js_url and get_ext(js_url) in VIDEO_EXTS:
                    escaped_url = html.escape(js_url)
                    html_content = f'<html><body><video src="{escaped_url}"></video></body></html>'
                    html = html_content
                elif js_url and get_ext(js_url) in IMAGE_EXTS:
                    escaped_url = html.escape(js_url)
                    html_content = f'<html><body><img src="{escaped_url}"></body></html>'
                    html = html_content
                else:
                    html = page.content()
            except Exception:
                try:
                    html = page.content()
                except Exception:
                    html = None
        except Exception as e:
            print(f"  [BrowserPool] fetch error: {e}")
            try:
                page.close()
                page = browser.new_page()
            except Exception:
                discard = True
        finally:
            with self._lock:
                over = self._alive > self._target
            if over or discard:
                with self._lock:
                    self._alive -= 1
                try: browser.close()
                except: pass
                try: pw.stop()
                except: pass
            else:
                self._queue.put((pw, browser, page))
        return html

    def stop(self):
        with self._lock:
            self._target = 0
        while True:
            try:
                pw, browser, _ = self._queue.get_nowait()
                try: browser.close()
                except: pass
                try: pw.stop()
                except: pass
            except queue.Empty:
                break

    @property
    def status(self):
        with self._lock:
            return {"target": self._target, "alive": self._alive, "queued": self._queue.qsize()}

browser_pool = BrowserPool()

# Incremental preview: sizes resolved in background, streamed via SSE
preview_sessions = {}
preview_sessions_lock = threading.Lock()
PREVIEW_SESSION_MAX_AGE = 600  # seconds — gc stale sessions

# ─── Helpers ───────────────────────────────────────────────────────────────────

def _bunkrinfo_read(d):
    p = d / ".bunkrinfo"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _bunkrinfo_write(d, data):
    (d / ".bunkrinfo").write_text(json.dumps(data), encoding="utf-8")


def update_bunkrinfo_remove(album_dir, filename):
    """
    Remove a file entry from an album's .bunkrinfo metadata.
    
    Args:
        album_dir: Path object for the album directory
        filename: Name of the file to remove from metadata
    
    Returns:
        The URL associated with the removed file, or None if not found
    
    Note:
        This function now handles locking internally for thread safety.
    """
    with bunkrinfo_lock:
        info = _bunkrinfo_read(album_dir)
        files = info.get("files", {})
        url = files.pop(filename, None)
        if url is not None:
            info["files"] = files
            _bunkrinfo_write(album_dir, info)
        return url


def update_bunkrinfo_add(album_dir, filename, url):
    """
    Add a file entry to an album's .bunkrinfo metadata.
    
    Args:
        album_dir: Path object for the album directory
        filename: Name of the file to add to metadata
        url: Source URL associated with the file
    
    Note:
        This function now handles locking internally for thread safety.
    """
    with bunkrinfo_lock:
        info = _bunkrinfo_read(album_dir)
        info.setdefault("files", {})[filename] = url
        _bunkrinfo_write(album_dir, info)


def update_bunkrinfo_batch_remove(album_dir, filenames):
    """
    Task 22.3: Remove multiple file entries from an album's .bunkrinfo metadata in a single write.
    
    Args:
        album_dir: Path object for the album directory
        filenames: List of filenames to remove from metadata
    
    Returns:
        Dictionary mapping filename to URL for removed files
    
    Note:
        This function now handles locking internally for thread safety.
    """
    with bunkrinfo_lock:
        info = _bunkrinfo_read(album_dir)
        files = info.get("files", {})
        removed_urls = {}
        
        for filename in filenames:
            url = files.pop(filename, None)
            if url is not None:
                removed_urls[filename] = url
        
        if removed_urls:
            info["files"] = files
            _bunkrinfo_write(album_dir, info)
        
        return removed_urls


def update_bunkrinfo_batch_add(album_dir, file_url_map):
    """
    Task 22.3: Add multiple file entries to an album's .bunkrinfo metadata in a single write.
    
    Args:
        album_dir: Path object for the album directory
        file_url_map: Dictionary mapping filename to URL
    
    Note:
        This function now handles locking internally for thread safety.
    """
    if not file_url_map:
        return
    
    with bunkrinfo_lock:
        info = _bunkrinfo_read(album_dir)
        files = info.setdefault("files", {})
        files.update(file_url_map)
        info["files"] = files
        _bunkrinfo_write(album_dir, info)


def sanitize(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()

def validate_album_path(album_name):
    """
    Validate album name to prevent path traversal attacks.
    
    Args:
        album_name: The album name to validate
    
    Returns:
        True if valid, False otherwise
    
    Raises:
        ValueError: If the album name contains path traversal sequences
    """
    if not album_name or not isinstance(album_name, str):
        raise ValueError("Album name must be a non-empty string")
    
    # Check for path traversal sequences
    if ".." in album_name or "/" in album_name or "\\" in album_name:
        raise ValueError("Album name contains invalid path traversal sequences")
    
    # Check for absolute paths
    if album_name.startswith(("/", "\\")):
        raise ValueError("Album name cannot be an absolute path")
    
    # Resolve the path and ensure it's within DOWNLOADS_DIR
    try:
        album_path = (DOWNLOADS_DIR / album_name).resolve()
        downloads_path = DOWNLOADS_DIR.resolve()
        
        # Check if the resolved path is within DOWNLOADS_DIR
        if not str(album_path).startswith(str(downloads_path)):
            raise ValueError("Album path resolves outside of downloads directory")
        
        # Check for symlinks
        if album_path.is_symlink():
            raise ValueError("Album path is a symlink")
            
    except Exception as e:
        raise ValueError(f"Invalid album path: {e}")
    
    return True

def unique_album_dir(album_name, album_url):
    base = sanitize(album_name) or "bunkr_album"
    album_id = urlparse(strip_page_param(album_url)).path.rstrip("/").split("/")[-1]
    suffix = sanitize(album_id)[:8] if album_id else uuid.uuid4().hex[:8]
    normalized_url = strip_page_param(album_url).rstrip("/")
    candidate = DOWNLOADS_DIR / base
    if not candidate.exists():
        return base, candidate

    # Re-running the same album is a repair/resume operation, not a new album.
    # Reuse its directory so partial files continue from their current byte and
    # completed files are skipped. Older builds always created a suffixed copy,
    # which prevented recovery after restarting BunkrWrap.
    existing_info = _bunkrinfo_read(candidate)
    existing_url = strip_page_param(existing_info.get("url", "")).rstrip("/")
    if existing_url and existing_url == normalized_url:
        return base, candidate

    # Also recover a prior suffixed attempt of this same album.
    for existing_dir in DOWNLOADS_DIR.iterdir():
        if not existing_dir.is_dir():
            continue
        if not existing_dir.name.startswith(f"{base} [{suffix}"):
            continue
        existing_info = _bunkrinfo_read(existing_dir)
        existing_url = strip_page_param(existing_info.get("url", "")).rstrip("/")
        if existing_url and existing_url == normalized_url:
            return existing_dir.name, existing_dir

    unique_name = f"{base} [{suffix}]"
    candidate = DOWNLOADS_DIR / unique_name
    n = 2
    while candidate.exists():
        unique_name = f"{base} [{suffix}-{n}]"
        candidate = DOWNLOADS_DIR / unique_name
        n += 1
    return unique_name, candidate

def get_ext(url):
    path = urlparse(url).path
    if path.lower().endswith('.tar.gz'):
        return '.tar.gz'
    if path.lower().endswith('.tar.bz2'):
        return '.tar.bz2'
    return Path(path).suffix.lower()

def is_media(url):
    return get_ext(url) in MEDIA_EXTS

def is_archive(url):
    return get_ext(url) in ZIP_EXTS

def is_thumbnail(url):
    low = url.lower()
    return any(x in low for x in ["thumb", "poster", "preview", "cover", "thumbnail"])

def is_bunkr_download_step(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    old_step = re.search(r'(^|\.)get\.bunkr+r?\.su$', host, re.I)
    current_step = host.lower() == "dl.bunkr.cr"
    return bool((old_step or current_step) and re.search(r'^/file/\d+', parsed.path))

def filename_from_page_html(html):
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("h1") or soup.find("title")
    if not title_el:
        return None
    name = sanitize(title_el.get_text(strip=True))
    return name if get_ext(name) in MEDIA_EXTS else None

def resolve_download_step_response(url, session, extra_headers=None, stream=False):
    headers = {**HEADERS, "Referer": "https://bunkr.cr/"}
    if extra_headers:
        headers.update(extra_headers)
    r = session.get(url, headers=headers, timeout=(15, 30), allow_redirects=True, stream=stream)
    ctype = (r.headers.get("content-type") or "").lower()
    if "text/html" not in ctype:
        return r
    html = r.text
    if stream:
        r.close()
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if form:
        action = urljoin(url, form.get("action") or url)
        method = (form.get("method") or "get").lower()
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")
        if method == "post":
            return session.post(action, headers=headers, data=data, timeout=(15, 30), allow_redirects=True, stream=stream)
        return session.get(action, headers=headers, params=data, timeout=(15, 30), allow_redirects=True, stream=stream)
    link = extract_media_from_html(html, url, session)
    if link and link != url:
        return session.get(link, headers=headers, timeout=(15, 30), allow_redirects=True, stream=stream)
    # Return None instead of raising exception to allow graceful error handling
    return None

def format_size(num_bytes):
    if num_bytes is None:
        return "?"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"

def get_free_disk_bytes():
    try:
        return shutil.disk_usage(DOWNLOADS_DIR).free
    except Exception:
        return None

def exponential_backoff(attempt):
    return min(30, BASE_RETRY_DELAY * (2 ** attempt))

def filter_pages_by_only(file_pages, only_file):
    """Keep only file page(s) whose URL slug matches only_file (preview row name)."""
    if not only_file or not str(only_file).strip():
        return file_pages
    target = str(only_file).strip()
    tl = target.lower()
    out = []
    for p in file_pages:
        slug = p.rstrip("/").split("/")[-1]
        candidates = (slug, unquote(slug))
        if target in candidates or tl in {s.lower() for s in candidates}:
            out.append(p)
    return out

def strip_page_param(url):
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=""))

def build_page_url(base_url, page_num):
    return f"{strip_page_param(base_url)}?page={page_num}"

def detect_bunkr_page_issue(html):
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    if "server under maintenance" in text or "temporarily unavailable for maintenance" in text:
        return "Bunkr file server is under maintenance; downloads are temporarily disabled. Retry later."
    if "playback and downloads are disabled" in text:
        return "Bunkr says playback and downloads are disabled for this file. Retry later."
    return None

# ─── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_page(url, session, max_retries=None, job_log=None):
    if max_retries is None:
        max_retries = DEFAULT_MAX_RETRIES
    last_error = None
    for attempt in range(max_retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}"
            if job_log is not None and attempt == max_retries - 1:
                job_log.append({"type": "error", "msg": f"HTTP {e.response.status_code} error fetching page"})
            time.sleep(exponential_backoff(attempt))
        except requests.exceptions.Timeout:
            last_error = "Timeout"
            if job_log is not None and attempt == max_retries - 1:
                job_log.append({"type": "error", "msg": "Request timeout - Bunkr may be slow or blocking requests"})
            time.sleep(exponential_backoff(attempt))
        except requests.exceptions.ConnectionError:
            last_error = "Connection error"
            if job_log is not None and attempt == max_retries - 1:
                job_log.append({"type": "error", "msg": "Connection error - check your internet connection"})
            time.sleep(exponential_backoff(attempt))
        except Exception as e:
            last_error = str(e)
            if job_log is not None and attempt == max_retries - 1:
                job_log.append({"type": "error", "msg": f"Error fetching page: {e}"})
            time.sleep(exponential_backoff(attempt))
    return None

def fetch_page_js(url):
    if not PLAYWRIGHT_OK:
        return None
    return browser_pool.fetch(url)

# ─── Archive URL resolver ─────────────────────────────────────────────────────

def resolve_archive_url_from_html(html, page_url, session):
    """Dedicated extractor for archive (zip/rar/7z/tar) download URLs."""
    soup = BeautifulSoup(html, "html.parser")

    # Explicit download anchor links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(page_url, href)
        if is_archive(full) and not is_thumbnail(full):
            return full

    # data-* attributes
    for tag in soup.find_all(True):
        for attr in ("data-src", "data-url", "data-href", "data-download", "data-file"):
            val = tag.get(attr, "")
            if val and is_archive(val):
                return urljoin(page_url, val)

    # Regex: explicit archive extensions in raw HTML
    for m in re.findall(
        r'https?://[^\s"\'<>]+\.(?:zip|rar|7z|tar\.gz|tar\.bz2|tar|gz)(?:\?[^\s"\'<>]*)?',
        html, re.I
    ):
        if not is_thumbnail(m):
            return m

    # CDN pattern with archive extension
    for m in re.findall(r'https?://[^\s"\'<>]+', html, re.I):
        ext = get_ext(m)
        if ext in ZIP_EXTS and not is_thumbnail(m):
            host = urlparse(m).hostname or ""
            if CDN_HOSTS.search(host):
                return m

    return None

# ─── Album scraper with multi-page support ────────────────────────────────────

def extract_file_links(album_url, session, job_log=None):
    """
    Handles:
      /a/XXXX  — album pages listing /f/ /v/ /i/ /d/ file pages
      /f/XXXX  — single file page
      Paginated albums: crawls ?page=1, ?page=2, ... until empty
    """
    if job_log is None:
        job_log = []

    # Single file URL
    if re.search(r"/(f|v|i|d)/[a-zA-Z0-9_\-]+", album_url):
        file_id = album_url.rstrip("/").split("/")[-1]
        return sanitize(file_id), [album_url]

    base_url = strip_page_param(album_url)
    parsed = urlparse(album_url)
    base_netloc = f"{parsed.scheme}://{parsed.netloc}"

    # Starting page
    start_page = int(parse_qs(parsed.query).get("page", [1])[0])

    seen, all_links = set(), []
    album_name = None

    page_num = start_page
    while True:
        page_url_to_fetch = base_url if page_num == 1 else build_page_url(base_url, page_num)
        job_log.append({"type": "info", "msg": f"Fetching page {page_num}..."})
        html = fetch_page(page_url_to_fetch, session, job_log=job_log)
        if not html:
            if page_num == start_page:
                job_log.append({"type": "error", "msg": f"Failed to fetch album page after {DEFAULT_MAX_RETRIES} attempts. The album may be private, deleted, or Bunkr may be blocking requests."})
                return None, []
            break

        soup = BeautifulSoup(html, "html.parser")

        # Grab album name from first page
        if album_name is None:
            title = soup.find("h1") or soup.find("title")
            raw_name = title.get_text(strip=True) if title else "bunkr_album"
            # Strip trailing page indicators
            raw_name = re.sub(r'\s*[-–|]\s*[Pp]age\s*\d+\s*$', '', raw_name).strip()
            album_name = sanitize(raw_name) or "bunkr_album"

        page_links = []
        for a in soup.find_all("a", href=True):
            full = urljoin(base_netloc, a["href"])
            if re.search(r"/(f|v|i|d)/[a-zA-Z0-9_\-]+", full) and full not in seen:
                seen.add(full)
                page_links.append(full)

        if not page_links:
            if page_num == start_page:
                # Maybe a direct zip page
                direct = extract_media_from_html(html, album_url, session)
                if direct:
                    job_log.append({"type": "info", "msg": f'Album: "{album_name}" — 1 file found (direct)'})
                    return album_name, [album_url]
                # No links found at all
                job_log.append({"type": "error", "msg": "No file links found in album page. The album may be empty, private, or the page structure may have changed."})
            break

        all_links.extend(page_links)

        # Probe next page
        next_page_url = build_page_url(base_url, page_num + 1)
        probe_html = fetch_page(next_page_url, session, max_retries=1)
        if probe_html:
            probe_soup = BeautifulSoup(probe_html, "html.parser")
            probe_links = [
                urljoin(base_netloc, a["href"])
                for a in probe_soup.find_all("a", href=True)
                if re.search(r"/(f|v|i|d)/[a-zA-Z0-9_\-]+", a["href"])
                and urljoin(base_netloc, a["href"]) not in seen
            ]
            if probe_links:
                page_num += 1
                continue

        break

    if all_links:
        job_log.append({"type": "info", "msg": f'Album: "{album_name}" — {len(all_links)} files found (across {page_num - start_page + 1} page(s))'})

    return album_name, all_links

# ─── Media URL extractor ───────────────────────────────────────────────────────

def extract_media_from_html(html, page_url=None, session=None):
    """Multi-strategy media URL extraction."""
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 0a: __NEXT_DATA__ / embedded JSON script tags (Next.js SSR)
    for script in soup.find_all("script", id="__NEXT_DATA__"):
        raw = script.string or ""
        for m in re.findall(r'"(https?:[^"\\]+\.(?:mp4|mkv|mov|webm|avi|m4v|jpg|jpeg|png|gif|webp)(?:\?[^"\\]*)?)"', raw, re.I):
            if not is_thumbnail(m):
                return m
    # Strategy 0b: any <script> containing a CDN media URL
    for script in soup.find_all("script"):
        raw = script.string or ""
        for m in re.findall(r'"(https?://[^"\\]+\.(?:mp4|mkv|mov|webm|avi|m4v)(?:\?[^"\\]*)?)"', raw, re.I):
            if not is_thumbnail(m):
                host = urlparse(m).hostname or ""
                if CDN_HOSTS.search(host):
                    return m

    # Strategy 0: og:video / og:video:url meta tags — page-specific, server-side rendered
    for meta in soup.find_all("meta", property=re.compile(r"og:video", re.I)):
        content = meta.get("content", "")
        if content and get_ext(content) in VIDEO_EXTS and not is_thumbnail(content):
            return content

    # Strategy 1: <video>/<source> tags
    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-video-src", "data-url"):
            src = tag.get(attr, "")
            if src and get_ext(src) in VIDEO_EXTS and not is_thumbnail(src):
                return src

    # Strategy 2: Regex for video URLs — CDN hosts only (avoids picking up unrelated embeds)
    for m in re.findall(
        r'https?://[^\s"\'<>]+\.(?:mp4|mkv|mov|webm|avi|m4v|ts)(?:\?[^\s"\'<>]*)?', html, re.I
    ):
        if not is_thumbnail(m):
            host = urlparse(m).hostname or ""
            if CDN_HOSTS.search(host):
                return m

    # Strategy 2b: Regex for video URLs — any host (fallback)
    for m in re.findall(
        r'https?://[^\s"\'<>]+\.(?:mp4|mkv|mov|webm|avi|m4v|ts)(?:\?[^\s"\'<>]*)?', html, re.I
    ):
        if not is_thumbnail(m):
            return m

    # Strategy 3: Anchors with direct media extension + download keyword
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(page_url, href) if page_url else href
        ext = get_ext(full)
        text = a.get_text(strip=True).lower()
        if ext in MEDIA_EXTS and not is_thumbnail(full):
            if any(kw in text for kw in ["download", "dl", "get", "zip", "save"]):
                return full

    # Strategy 3b: Any anchor with a direct media extension (e.g. "Enlarge image" CDN links)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(page_url, href) if page_url else href
        ext = get_ext(full)
        if ext in MEDIA_EXTS and not is_thumbnail(full):
            return full

    # Strategy 4: CDN URL patterns
    for m in re.findall(r'https?://[^\s"\'<>]+', html, re.I):
        ext = get_ext(m)
        if ext in MEDIA_EXTS and not is_thumbnail(m):
            host = urlparse(m).hostname or ""
            if CDN_HOSTS.search(host):
                return m

    # Strategy 5: Any URL with media extension
    for m in re.findall(r'https?://[^\s"\'<>]+', html, re.I):
        ext = get_ext(m)
        if ext in MEDIA_EXTS and not is_thumbnail(m):
            return m

    # Strategy 6: JSON-embedded URLs
    for m in re.findall(r'"(https?://[^"]+\.(?:mp4|mkv|mov|webm|zip|rar|7z|tar|jpg|jpeg|png|gif)[^"]*)"', html, re.I):
        if not is_thumbnail(m):
            return m

    # Strategy 7: og: meta tags
    for meta in soup.find_all("meta", property=re.compile(r"og:(video|image|url)", re.I)):
        content = meta.get("content", "")
        if content and is_media(content) and not is_thumbnail(content):
            return content

    return None

def resolve_bunkr_api(slug, session):
    """Call bunkr's /api/vs endpoint and decrypt the CDN URL (XOR cipher, key is public)."""
    try:
        r = session.post(
            "https://bunkr.cr/api/vs",
            json={"slug": slug},
            headers={**HEADERS, "Content-Type": "application/json", "Referer": "https://bunkr.cr/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        timestamp = data.get("timestamp")
        encrypted_url = data.get("url")
        if not timestamp or not encrypted_url:
            return None
        secret_key = f"SECRET_KEY_{math.floor(timestamp / 3600)}"
        enc_bytes = list(base64.b64decode(encrypted_url))
        key_bytes = list(secret_key.encode("utf-8"))
        decrypted = "".join(
            chr(enc_bytes[i] ^ key_bytes[i % len(key_bytes)])
            for i in range(len(enc_bytes))
        )
        return decrypted if decrypted.startswith("http") else None
    except Exception:
        return None


def _sign_bunkr_cdn_url(raw_url, sign_url, page_url, session):
    """Add the token/ex parameters required by Bunkr's current CDN."""
    try:
        raw_parsed = urlparse(raw_url)
        sign_parsed = urlparse(sign_url)
        if raw_parsed.scheme not in ("http", "https") or not raw_parsed.netloc:
            return None
        if sign_parsed.scheme != "https" or not sign_parsed.netloc:
            return None

        r = session.get(
            sign_url,
            params={"path": unquote(raw_parsed.path)},
            headers={**HEADERS, "Referer": page_url or "https://bunkr.cr/"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("token")
        expires = data.get("ex")
        if not token or expires is None:
            return None

        query = parse_qs(raw_parsed.query, keep_blank_values=True)
        query["token"] = [str(token)]
        query["ex"] = [str(expires)]
        return urlunparse(raw_parsed._replace(query=urlencode(query, doseq=True)))
    except (ValueError, TypeError, requests.RequestException):
        return None


def resolve_bunkr_signed_media(html, page_url, session):
    """Resolve Bunkr's current jsCDN + signing-endpoint delivery flow."""
    if not html:
        return None

    cdn_match = re.search(
        r'\b(?:var|let|const)\s+jsCDN\s*=\s*("(?:\\.|[^"\\])*")',
        html,
        re.I,
    )
    sign_match = re.search(
        r'\b(?:var|let|const)\s+signUrl\s*=\s*("(?:\\.|[^"\\])*")',
        html,
        re.I,
    )
    if not cdn_match or not sign_match:
        return None

    try:
        # json.loads correctly decodes JavaScript's escaped forward slashes.
        raw_url = json.loads(cdn_match.group(1))
        sign_url = json.loads(sign_match.group(1))
    except (ValueError, TypeError):
        return None
    return _sign_bunkr_cdn_url(raw_url, sign_url, page_url, session)


def resolve_bunkr_download_link(html, page_url, session):
    """Resolve files that Bunkr serves through its dl.bunkr.cr metadata page."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    download_url = None
    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(page_url, anchor["href"])
        if is_bunkr_download_step(candidate):
            download_url = candidate
            break
    if not download_url:
        return None

    try:
        r = session.get(
            download_url,
            headers={**HEADERS, "Referer": page_url},
            timeout=15,
        )
        r.raise_for_status()
        step_html = r.text
        step_soup = BeautifulSoup(step_html, "html.parser")
        button = step_soup.find(id="download-btn")
        file_id = button.get("data-id") if button else None
        if not file_id:
            file_id = urlparse(download_url).path.rstrip("/").split("/")[-1]
        if not str(file_id).isdigit():
            return None

        step_parsed = urlparse(download_url)
        api_url = urlunparse(step_parsed._replace(path="/api/_001_v2", params="", query="", fragment=""))
        meta_response = session.post(
            api_url,
            json={"id": str(file_id)},
            headers={**HEADERS, "Referer": download_url},
            timeout=15,
        )
        meta_response.raise_for_status()
        meta = meta_response.json()
        media_base = meta.get("mediafiles")
        media_path = meta.get("path")
        if not media_base or not media_path:
            return None
        raw_url = urljoin(media_base.rstrip("/") + "/", str(media_path).lstrip("/"))
        original = meta.get("original")
        if original:
            parsed = urlparse(raw_url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query["n"] = [str(original)]
            raw_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

        sign_match = re.search(
            r"\bSIGN_SERVICE_URL\s*=\s*['\"](https://[^'\"]+)['\"]",
            step_html,
            re.I,
        )
        sign_url = sign_match.group(1) if sign_match else "https://glb-apisign.cdn.cr/sign"
        return _sign_bunkr_cdn_url(raw_url, sign_url, download_url, session)
    except (ValueError, TypeError, requests.RequestException):
        return None


def resolve_direct_url(page_url, session, job_log, max_retries=None, allow_playwright=True, return_reason=False):
    """Resolve a file-page URL to a direct download URL. Handles archives specially.

    When allow_playwright is False (e.g. album preview), only static HTML is used — fast and
    parallel-safe. Full downloads still use Playwright when needed.
    """
    if max_retries is None:
        max_retries = DEFAULT_MAX_RETRIES

    # Try bunkr's native API first — fastest, no browser needed
    slug = unquote(page_url.rstrip("/").split("/")[-1])
    if slug:
        api_result = resolve_bunkr_api(slug, session)
        if api_result:
            job_log.append({"type": "info", "msg": f"  Resolved via API: {slug}"})
            return (api_result, None) if return_reason else api_result

    html = fetch_page(page_url, session, max_retries)
    if html:
        issue = detect_bunkr_page_issue(html)
        if issue:
            job_log.append({"type": "error", "msg": f"  {issue}"})
            return (None, issue) if return_reason else None

        # Current Bunkr pages expose an escaped jsCDN URL and a separate signing
        # endpoint.  The resulting token/ex query parameters are required by the CDN.
        signed_result = resolve_bunkr_signed_media(html, page_url, session)
        if signed_result:
            job_log.append({"type": "info", "msg": f"  Resolved via signed CDN: {slug}"})
            return (signed_result, None) if return_reason else signed_result

        download_result = resolve_bunkr_download_link(html, page_url, session)
        if download_result:
            job_log.append({"type": "info", "msg": f"  Resolved via download page: {slug}"})
            return (download_result, None) if return_reason else download_result

        # Check if this is an archive page
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find("h1") or soup.find("title")
        title_text = title_el.get_text(strip=True).lower() if title_el else ""

        archive_hint = any(ext in title_text for ext in ['.zip', '.rar', '.7z', '.tar', '.gz'])
        if not archive_hint:
            for a in soup.find_all("a", href=True):
                if get_ext(a["href"]) in ZIP_EXTS:
                    archive_hint = True
                    break

        if archive_hint:
            result = resolve_archive_url_from_html(html, page_url, session)
            if result:
                return (result, None) if return_reason else result

        result = extract_media_from_html(html, page_url, session)

        if result:
            return (result, None) if return_reason else result

        if allow_playwright:
            job_log.append({"type": "warn", "msg": "  No media in static HTML — trying headless browser..."})

    if allow_playwright and PLAYWRIGHT_OK:
        job_log.append({"type": "info", "msg": f"  Headless browser resolving {page_url.rstrip('/').split('/')[-1]}..."})
        html_js = fetch_page_js(page_url)
        if html_js:
            issue = detect_bunkr_page_issue(html_js)
            if issue:
                job_log.append({"type": "error", "msg": f"  {issue}"})
                return (None, issue) if return_reason else None

            # Try archive resolver first on JS-rendered page
            result = resolve_archive_url_from_html(html_js, page_url, session)
            if result:
                return (result, None) if return_reason else result
            result = extract_media_from_html(html_js, page_url, session)
            if result:
                return (result, None) if return_reason else result
            job_log.append({"type": "error", "msg": "  Headless browser also found no media URL"})
        else:
            job_log.append({"type": "error", "msg": "  Headless browser render failed or timed out"})
    elif allow_playwright and not PLAYWRIGHT_OK:
        job_log.append({
            "type": "warn",
            "msg": "  Playwright not installed — run: pip install playwright && playwright install chromium"
        })

    return (None, "Could not resolve media URL") if return_reason else None

# ─── Pre-flight size check ─────────────────────────────────────────────────────

def preflight_size(url, session):
    """
    Remote file size: HEAD Content-Length, then GET Range 0-0 + Content-Range total.
    Matches Referer used by downloads so CDNs behave consistently.
    """
    dl_headers = {**HEADERS, "Referer": "https://bunkr.cr/"}
    try:
        r = session.head(url, headers=dl_headers, timeout=15, allow_redirects=True)
        cl = r.headers.get("content-length")
        if cl:
            try:
                return int(cl)
            except ValueError:
                pass
    except Exception:
        pass

    r = None
    try:
        r = session.get(
            url,
            headers={**dl_headers, "Range": "bytes=0-0"},
            timeout=20,
            allow_redirects=True,
            stream=True,
        )
        cr = r.headers.get("content-range") or r.headers.get("Content-Range")
        if cr:
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
        cl = r.headers.get("content-length")
        if cl:
            try:
                return int(cl)
            except ValueError:
                pass
    except Exception:
        pass
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
    return None

# ─── Thumbnail generation ──────────────────────────────────────────────────────

# ─── Thumbnail generation ──────────────────────────────────────────────────────

def generate_image_thumbnail(image_path, album_name):
    """Generate a thumbnail for an image file using PIL/Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    
    try:
        thumb_dir = THUMBS_DIR / album_name
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / (image_path.stem + "_thumb.jpg")
        
        # Skip 0-byte files (e.g. downloads stopped before any data written)
        if image_path.stat().st_size == 0:
            return None
        # Skip if thumbnail already exists and is newer than source
        if thumb_path.exists() and thumb_path.stat().st_mtime >= image_path.stat().st_mtime:
            return f"/thumbs/{album_name}/{thumb_path.name}"
        
        with Image.open(image_path) as img:
            # Convert RGBA to RGB if needed
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Calculate thumbnail size maintaining aspect ratio
            img.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
            img.save(thumb_path, 'JPEG', quality=THUMB_QUALITY, optimize=True)
        
        return f"/thumbs/{album_name}/{thumb_path.name}"
    except Exception as e:
        print(f"  [Thumbnail] Failed to generate image thumbnail for {image_path.name}: {e}")
        return None

def generate_video_thumbnail(video_path, album_name):
    if not FFMPEG_OK:
        return None
    try:
        thumb_dir = THUMBS_DIR / album_name
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / (video_path.stem + ".jpg")
        # Skip 0-byte files (e.g. downloads stopped before any data written)
        if video_path.stat().st_size == 0:
            return None
        # Skip if thumbnail already exists and is newer than source
        if thumb_path.exists() and thumb_path.stat().st_mtime >= video_path.stat().st_mtime:
            return f"/thumbs/{album_name}/{thumb_path.name}"
        probe = subprocess.run(
            [FFPROBE_CMD, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 10.0
        seek = max(0, duration / 2)
        # Use scale and crop to create a square thumbnail that fills the entire space
        # First scale to ensure one dimension is THUMB_MAX_SIZE, then crop to square
        subprocess.run(
            [FFMPEG_CMD, "-y", "-ss", str(seek), "-i", str(video_path),
             "-vframes", "1", "-q:v", "3", 
             "-vf", f"scale={THUMB_MAX_SIZE}:{THUMB_MAX_SIZE}:force_original_aspect_ratio=increase,crop={THUMB_MAX_SIZE}:{THUMB_MAX_SIZE}", 
             str(thumb_path)],
            capture_output=True, timeout=30
        )
        if thumb_path.exists():
            return f"/thumbs/{album_name}/{thumb_path.name}"
        return None
    except Exception as e:
        print(f"  [Thumbnail] Failed to generate video thumbnail for {video_path.name}: {e}")
        return None

# ─── Downloader ────────────────────────────────────────────────────────────────

def download_file(url, dest, session, job, file_index, max_retries=None, adaptive_gate=None):
    if max_retries is None:
        max_retries = DEFAULT_MAX_RETRIES

    attempt = 0
    rate_limit_events = 0
    max_rate_limit_events = 3

    while attempt < max_retries:
        try:
            wait_for_cdn_slot(url, job, file_index)
            existing_size = dest.stat().st_size if dest.exists() else 0
            dl_headers = {**HEADERS, "Referer": "https://bunkr.cr/"}
            if existing_size > 0:
                dl_headers["Range"] = f"bytes={existing_size}-"

            with session.get(url, headers=dl_headers, stream=True, timeout=(20, 90)) as r:
                # Handle rate limiting
                if r.status_code == 429:
                    retry_header = r.headers.get("Retry-After")
                    retry_after = int(retry_header) if retry_header and retry_header.isdigit() else None
                    delay = register_cdn_throttle(url, retry_after, severe=True, adaptive_gate=adaptive_gate)
                    rate_limit_events += 1
                    job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN rate limit (429); all downloads cooling down for {delay}s..."})
                    if rate_limit_events >= max_rate_limit_events:
                        job["log"].append({"type": "error", "msg": f"[{file_index}] CDN repeatedly rejected this file; leaving it for Retry Failed."})
                        return False
                    continue
                
                # Handle server errors
                if r.status_code == 503:
                    delay = register_cdn_throttle(url, severe=False, adaptive_gate=adaptive_gate)
                    job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN unavailable (503); cooling down for {delay}s..."})
                    attempt += 1
                    continue
                
                if r.status_code == 416:
                    content_range = r.headers.get("Content-Range", "")
                    match = re.search(r"\*/(\d+)$", content_range)
                    remote_size = int(match.group(1)) if match else preflight_size(url, session)
                    if remote_size and existing_size >= remote_size:
                        register_cdn_success(url, adaptive_gate=adaptive_gate)
                        return True
                    job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN rejected the resume offset; keeping the partial file and retrying."})
                    register_cdn_throttle(url, severe=False, adaptive_gate=adaptive_gate)
                    attempt += 1
                    continue
                r.raise_for_status()

                # A resumed request must return a matching 206 response. If the
                # CDN ignores Range and returns 200, overwrite instead of appending
                # a second full copy and silently corrupting the file.
                if existing_size > 0 and r.status_code == 206:
                    content_range = r.headers.get("Content-Range", "")
                    expected_prefix = f"bytes {existing_size}-"
                    if not content_range.lower().startswith(expected_prefix.lower()):
                        job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN returned a mismatched resume range; restarting file safely."})
                        if dest.exists():
                            dest.unlink()
                        attempt += 1
                        continue
                elif existing_size > 0 and r.status_code == 200:
                    job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN ignored resume request; restarting file safely."})
                    existing_size = 0

                total_from_header = r.headers.get("content-length")
                total_size = (int(total_from_header) + existing_size) if total_from_header else None
                speed_window = deque(maxlen=10)
                downloaded = existing_size
                mode = "ab" if existing_size > 0 else "wb"
                with open(dest, mode) as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        # Check for stop or pause request during download
                        if job.get("stop_requested"):
                            # Clean up partial/0-byte files when stopped
                            f.close()
                            if dest.exists() and dest.stat().st_size == 0:
                                dest.unlink()
                            return False
                        if job.get("pause_requested"):
                            job["log"].append({"type": "warn", "msg": f"[{file_index}] ⏸ Paused mid-download: {dest.name}"})
                            # Clean up 0-byte files when paused
                            f.close()
                            if dest.exists() and dest.stat().st_size == 0:
                                dest.unlink()
                            return False  # Return False to indicate incomplete download
                        
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        speed_window.append((now, downloaded))
                        if len(speed_window) >= 2:
                            elapsed = speed_window[-1][0] - speed_window[0][0]
                            bytes_in_window = speed_window[-1][1] - speed_window[0][1]
                            speed_bps = bytes_in_window / elapsed if elapsed > 0 else 0
                        else:
                            speed_bps = 0
                        with jobs_lock:
                            job["file_speeds"][file_index] = speed_bps
                            job["file_progress"][file_index] = {
                                "downloaded": downloaded, "total": total_size, "speed": speed_bps,
                            }
                if total_size is not None and downloaded < total_size:
                    raise requests.exceptions.ChunkedEncodingError(
                        f"response ended at {downloaded} of {total_size} bytes"
                    )
                # Small delay after successful download to reduce rate limiting
                register_cdn_success(url, adaptive_gate=adaptive_gate)
                time.sleep(0.5)
                return True

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                retry_header = e.response.headers.get("Retry-After")
                retry_after = int(retry_header) if retry_header and retry_header.isdigit() else None
                delay = register_cdn_throttle(url, retry_after, severe=True, adaptive_gate=adaptive_gate)
                rate_limit_events += 1
                job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN rate limit (429); all downloads cooling down for {delay}s..."})
                if rate_limit_events >= max_rate_limit_events:
                    job["log"].append({"type": "error", "msg": f"[{file_index}] CDN repeatedly rejected this file; leaving it for Retry Failed."})
                    return False
                continue
            elif e.response.status_code == 503:
                delay = register_cdn_throttle(url, severe=False, adaptive_gate=adaptive_gate)
                attempt += 1
                job["log"].append({"type": "warn", "msg": f"[{file_index}] CDN unavailable (503), retry {attempt}/{max_retries} after {delay}s"})
            else:
                attempt += 1
                print(f"  [Download] HTTP {e.response.status_code} attempt {attempt}: {e}")
                job["log"].append({"type": "warn", "msg": f"[{file_index}] HTTP {e.response.status_code} attempt {attempt}/{max_retries}"})
                time.sleep(exponential_backoff(attempt - 1))
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            attempt += 1
            delay = register_cdn_throttle(url, severe=False, adaptive_gate=adaptive_gate)
            partial_size = dest.stat().st_size if dest.exists() else 0
            print(f"  [Download] Interrupted attempt {attempt}: {e}")
            job["log"].append({"type": "warn", "msg": f"[{file_index}] Connection interrupted at {format_size(partial_size)}; resume {attempt}/{max_retries} after {delay}s"})
        except Exception as e:
            attempt += 1
            print(f"  [Download] Attempt {attempt} failed: {e}")
            job["log"].append({"type": "warn", "msg": f"[{file_index}] attempt {attempt}/{max_retries} failed: {e}"})
            time.sleep(exponential_backoff(attempt - 1))
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()

    return False

# ─── Worker thread ─────────────────────────────────────────────────────────────

def process_task(job_id, task, session, worker_idx):
    job = jobs[job_id]
    idx, page_url, out_dir, album_name, total = task
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)

    # Avoid stale speed from a previous file on this worker (was inflating total_speed)
    with jobs_lock:
        job["file_speeds"][str(idx)] = 0

    file_id = page_url.rstrip("/").split("/")[-1]
    job["log"].append({"type": "info", "msg": f"[{idx}/{total}] Resolving {file_id}..."})

    direct_url, fail_reason = resolve_direct_url(page_url, session, job["log"], max_retries, return_reason=True)

    if not direct_url:
        job["log"].append({"type": "error", "msg": f"[{idx}] ✗ {fail_reason} — skipping"})
        with jobs_lock:
            job["failed"] += 1
            job["done"] += 1
            job["failed_tasks"].append({
                "idx": idx, "page_url": page_url, "album_name": album_name,
                "out_dir": str(out_dir), "total": total, "reason": fail_reason
            })
        return False

    cdn_name = sanitize(Path(urlparse(direct_url).path).name)
    if not cdn_name:
        cdn_name = sanitize(unquote(Path(urlparse(direct_url).path).name))
    ext = get_ext(direct_url) or ".bin"
    if not cdn_name or re.match(r'^[0-9a-f\-]{32,}(\.[\w]+)?$', cdn_name):
        filename = f"{file_id}{ext}"
    else:
        filename = cdn_name

    dest = out_dir / filename
    file_type = "zip" if ext in ZIP_EXTS else ("image" if ext in IMAGE_EXTS else "video")

    file_size = preflight_size(direct_url, session)
    with jobs_lock:
        job["file_sizes"][idx] = file_size

    if dest.exists() and file_size and dest.stat().st_size >= file_size:
        job["log"].append({"type": "skip", "msg": f"[{idx}] ↷ Skipped (exists): {filename}"})
        with jobs_lock:
            job["skipped"] += 1
            job["done"] += 1
            job["files"].append({"name": filename, "album": album_name, "type": file_type, "size": file_size, "thumb": None})
        with bunkrinfo_lock:
            _info = _bunkrinfo_read(out_dir)
            _info.setdefault("files", {})[filename] = page_url
            _bunkrinfo_write(out_dir, _info)
        return True

    job["log"].append({"type": "info", "msg": f"[{idx}] ↓ {filename}" + (f" ({format_size(file_size)})" if file_size else "")})

    with jobs_lock:
        job["file_progress"][str(idx)] = {"downloaded": 0, "total": file_size, "speed": 0}

    ok = download_file(direct_url, dest, session, job, str(idx), max_retries)

    if ok:
        # Auto-extract archives into a named subfolder, then delete the archive
        _can_extract = (
            ext == ".zip" or
            ext in {".tar", ".gz", ".tar.gz", ".tar.bz2"} or
            (ext in {".rar", ".7z"} and SEVENZIP_OK)
        )
        if ext in {".rar", ".7z"} and not SEVENZIP_OK:
            job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ 7-Zip not found — keeping {filename} as-is"})
        if _can_extract and dest.exists():
            # Try to get real name from bunkr page (CDN renames files to opaque slugs)
            try:
                _title_html = fetch_page(page_url, session, max_retries=1)
                _orig_title = filename_from_page_html(_title_html)
                if _orig_title and get_ext(_orig_title) in ZIP_EXTS:
                    zip_stem = sanitize(Path(_orig_title).stem) or sanitize(Path(filename).stem) or f"zip_{idx}"
                else:
                    zip_stem = sanitize(Path(filename).stem) or f"zip_{idx}"
            except Exception:
                zip_stem = sanitize(Path(filename).stem) or f"zip_{idx}"
            extract_dir = out_dir / zip_stem
            nested_album = f"{album_name}/{zip_stem}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                extracted_files = []
                if ext == ".zip":
                    with zipfile.ZipFile(dest, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            mname = Path(member.filename).name
                            if not mname or mname.startswith("."):
                                continue
                            target = extract_dir / mname
                            if target.exists():
                                stem2, sfx2 = Path(mname).stem, Path(mname).suffix
                                target = extract_dir / f"{stem2}_{uuid.uuid4().hex[:6]}{sfx2}"
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            ext2 = target.suffix.lower()
                            ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                            if ftype2:
                                extracted_files.append({"name": target.name, "album": nested_album, "type": ftype2, "size": target.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                elif ext in {".tar", ".gz", ".tar.gz", ".tar.bz2"}:
                    with tarfile.open(dest, "r:*") as tf:
                        for member in tf.getmembers():
                            if not member.isfile():
                                continue
                            mname = Path(member.name).name
                            if not mname or mname.startswith("."):
                                continue
                            target = extract_dir / mname
                            if target.exists():
                                stem2, sfx2 = Path(mname).stem, Path(mname).suffix
                                target = extract_dir / f"{stem2}_{uuid.uuid4().hex[:6]}{sfx2}"
                            src_f = tf.extractfile(member)
                            if src_f:
                                with src_f, open(target, "wb") as dst:
                                    shutil.copyfileobj(src_f, dst)
                            ext2 = target.suffix.lower()
                            ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                            if ftype2:
                                extracted_files.append({"name": target.name, "album": nested_album, "type": ftype2, "size": target.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                elif ext in {".rar", ".7z"}:
                    result = subprocess.run(
                        [_7Z_CMD, "e", str(dest), f"-o{extract_dir}", "-y"],
                        capture_output=True, text=True, timeout=300
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip()[:300] or f"7-Zip exited with code {result.returncode}")
                    for ef in sorted(extract_dir.iterdir()):
                        if not ef.is_file() or ef.name.startswith("."):
                            continue
                        ext2 = ef.suffix.lower()
                        ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                        if ftype2:
                            extracted_files.append({"name": ef.name, "album": nested_album, "type": ftype2, "size": ef.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                dest.unlink()
                job["log"].append({"type": "ok", "msg": f"[{idx}] ✓ Extracted: {filename} → {zip_stem}/ ({len(extracted_files)} file(s))"})
                with jobs_lock:
                    job["files"].extend(extracted_files)
                    job["success"] += 1
                    job["zip_folders"].append({"dir": str(extract_dir), "album": nested_album})
                with jobs_lock:
                    job["done"] += 1
                    job["file_progress"][str(idx)] = {"downloaded": 0, "total": 0, "speed": 0}
                    job["file_speeds"][str(idx)] = 0
                with bunkrinfo_lock:
                    _einfo = _bunkrinfo_read(extract_dir)
                    _einfo["url"] = page_url  # Set the album URL to the ZIP file's page
                    _efiles = _einfo.setdefault("files", {})
                    for ef in extracted_files:
                        _efiles[ef["name"]] = page_url
                    _bunkrinfo_write(extract_dir, _einfo)
                return True
            except Exception as ex:
                job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ Extraction failed, keeping archive: {ex}"})
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                except Exception:
                    pass
        thumb_rel = None
        if file_type == "video" and FFMPEG_OK:
            thumb_rel = generate_video_thumbnail(dest, album_name)
            if thumb_rel:
                job["log"].append({"type": "info", "msg": f"[{idx}] 🖼 Video thumbnail: {thumb_rel}"})
            else:
                job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ Video thumbnail generation failed"})
        elif file_type == "image":
            thumb_rel = generate_image_thumbnail(dest, album_name)
            if thumb_rel:
                job["log"].append({"type": "info", "msg": f"[{idx}] 🖼 Image thumbnail: {thumb_rel}"})
        job["log"].append({"type": "ok", "msg": f"[{idx}] ✓ {filename}"})
        with bunkrinfo_lock:
            _info = _bunkrinfo_read(out_dir)
            _info.setdefault("files", {})[filename] = page_url
            _bunkrinfo_write(out_dir, _info)
        with jobs_lock:
            job["files"].append({"name": filename, "album": album_name, "type": file_type, "size": file_size, "thumb": thumb_rel})
            job["success"] += 1
    else:
        job["log"].append({"type": "error", "msg": f"[{idx}] ✗ Download failed: {filename}"})
        with jobs_lock:
            job["failed"] += 1
            job["failed_tasks"].append({
                "idx": idx, "page_url": page_url, "album_name": album_name,
                "out_dir": str(out_dir), "total": total, "reason": "Download failed after retries"
            })

    with jobs_lock:
        job["done"] += 1
        job["file_progress"][str(idx)] = {"downloaded": 0, "total": 0, "speed": 0}
        job["file_speeds"][str(idx)] = 0

    return ok


def worker(job_id, task_queue, session, semaphore, worker_idx):
    """Worker that processes tasks from the queue using the appropriate global thread pool."""
    job = jobs[job_id]
    while True:
        try:
            task = task_queue.get(block=False)
        except Exception:
            break
        while job.get("pause_requested"):
            job["paused"] = True
            time.sleep(0.5)
        job["paused"] = False
        if job.get("stop_requested"):
            # Mark task as done even when stopping to prevent queue hang
            task_queue.task_done()
            break
        try:
            with semaphore:
                process_task(job_id, task, session, worker_idx)
        except Exception as e:
            idx, page_url, out_dir, album_name, total = task
            reason = f"Internal worker error: {e}"
            job["log"].append({"type": "error", "msg": f"[{idx}] ✗ {reason}"})
            with jobs_lock:
                job["failed"] += 1
                job["done"] += 1
                job["failed_tasks"].append({
                    "idx": idx, "page_url": page_url, "album_name": album_name,
                    "out_dir": str(out_dir), "total": total, "reason": reason
                })
                job["file_progress"][str(idx)] = {"downloaded": 0, "total": 0, "speed": 0}
                job["file_speeds"][str(idx)] = 0
        finally:
            task_queue.task_done()
        time.sleep(0.5)


def _ensure_global_pools(concurrency_images, concurrency_videos):
    """Initialize or resize the resolver pool and adaptive download gates.

    The resolver pool is sized to image_threads + video_threads so that URL
    resolution always has enough threads to stay ahead of the download queues.
    Once a file type is known, the worker acquires the appropriate gate. The
    gate starts at the user's maximum, halves after throttling/broken streams,
    and adds slots back gradually after successful transfers.
    """
    global global_resolver_pool, global_image_semaphore, global_video_semaphore
    global _current_image_threads, _current_video_threads

    total_workers = concurrency_images + concurrency_videos

    with global_pool_lock:
        # ── Resolver pool ──────────────────────────────────────────────────────
        if global_resolver_pool is None:
            global_resolver_pool = ThreadPoolExecutor(
                max_workers=total_workers, thread_name_prefix="ResolverPool"
            )
            print(f"  [GlobalPool] Created resolver pool with {total_workers} threads "
                  f"({concurrency_images} image + {concurrency_videos} video/zip)")
        elif _current_image_threads + _current_video_threads != total_workers:
            old_total = _current_image_threads + _current_video_threads
            global_resolver_pool.shutdown(wait=False)
            global_resolver_pool = ThreadPoolExecutor(
                max_workers=total_workers, thread_name_prefix="ResolverPool"
            )
            print(f"  [GlobalPool] Resized resolver pool {old_total} → {total_workers} threads "
                  f"({concurrency_images} image + {concurrency_videos} video/zip)")

        # ── Adaptive image gate ───────────────────────────────────────────────
        if global_image_semaphore is None or _current_image_threads != concurrency_images:
            global_image_semaphore = AdaptiveDownloadGate(concurrency_images)
            print(f"  [GlobalPool] Adaptive image gate: up to {concurrency_images} download(s)")

        # ── Adaptive video/zip gate ───────────────────────────────────────────
        if global_video_semaphore is None or _current_video_threads != concurrency_videos:
            global_video_semaphore = AdaptiveDownloadGate(concurrency_videos)
            print(f"  [GlobalPool] Adaptive video/zip gate: up to {concurrency_videos} download(s)")

        _current_image_threads = concurrency_images
        _current_video_threads = concurrency_videos


def _process_task_with_pool_selection(job_id, task, session):
    """Process a task by first resolving the URL to determine file type, then downloading directly."""
    idx, page_url, out_dir, album_name, total = task
    # requests.Session is not guaranteed to be safe when mutated concurrently.
    # Reuse one session per resolver thread instead of sharing a single job session.
    session = _thread_session()
    job = jobs[job_id]
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)
    
    # Avoid stale speed from a previous file on this worker
    with jobs_lock:
        job["file_speeds"][str(idx)] = 0

    file_id = page_url.rstrip("/").split("/")[-1]
    job["log"].append({"type": "info", "msg": f"[{idx}/{total}] Resolving {file_id}..."})

    # Resolve URL to determine file type
    direct_url, fail_reason = resolve_direct_url(page_url, session, job["log"], max_retries, return_reason=True)

    if not direct_url:
        job["log"].append({"type": "error", "msg": f"[{idx}] ✗ {fail_reason} — skipping"})
        with jobs_lock:
            job["failed"] += 1
            job["done"] += 1
            job["failed_tasks"].append({
                "idx": idx, "page_url": page_url, "album_name": album_name,
                "out_dir": str(out_dir), "total": total, "reason": fail_reason
            })
        return False

    # Determine file type
    ext = get_ext(direct_url) or ".bin"
    file_type = "zip" if ext in ZIP_EXTS else ("image" if ext in IMAGE_EXTS else "video")

    # Acquire the per-type adaptive gate. Images use global_image_semaphore;
    # videos and zips use global_video_semaphore.
    # Holding a reference at this point is safe: if settings change mid-job the
    # old semaphore is simply released by its holders and the new one takes over.
    sem = global_image_semaphore if file_type == "image" else global_video_semaphore
    with sem:
        return _download_file_task(
            job_id, idx, page_url, direct_url, out_dir, album_name, total,
            session, file_type, adaptive_gate=sem,
        )


def _download_file_task(job_id, idx, page_url, direct_url, out_dir, album_name, total, session, file_type, adaptive_gate=None):
    """Download a file after URL has been resolved and type determined."""
    job = jobs[job_id]
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)
    
    file_id = page_url.rstrip("/").split("/")[-1]
    
    cdn_name = sanitize(Path(urlparse(direct_url).path).name)
    if not cdn_name:
        cdn_name = sanitize(unquote(Path(urlparse(direct_url).path).name))
    ext = get_ext(direct_url) or ".bin"
    if not cdn_name or re.match(r'^[0-9a-f\-]{32,}(\.[\w]+)?$', cdn_name):
        filename = f"{file_id}{ext}"
    else:
        filename = cdn_name

    dest = out_dir / filename

    file_size = preflight_size(direct_url, session)
    with jobs_lock:
        job["file_sizes"][idx] = file_size

    if dest.exists() and file_size and dest.stat().st_size >= file_size:
        job["log"].append({"type": "skip", "msg": f"[{idx}] ↷ Skipped (exists): {filename}"})
        with jobs_lock:
            job["skipped"] += 1
            job["done"] += 1
            job["files"].append({"name": filename, "album": album_name, "type": file_type, "size": file_size, "thumb": None})
        with bunkrinfo_lock:
            _info = _bunkrinfo_read(out_dir)
            _info.setdefault("files", {})[filename] = page_url
            _bunkrinfo_write(out_dir, _info)
        return True

    job["log"].append({"type": "info", "msg": f"[{idx}] ↓ {filename}" + (f" ({format_size(file_size)})" if file_size else "")})

    with jobs_lock:
        job["file_progress"][str(idx)] = {"downloaded": 0, "total": file_size, "speed": 0}

    ok = download_file(
        direct_url, dest, session, job, str(idx), max_retries,
        adaptive_gate=adaptive_gate,
    )

    if ok:
        # Auto-extract archives into a named subfolder, then delete the archive
        _can_extract = (
            ext == ".zip" or
            ext in {".tar", ".gz", ".tar.gz", ".tar.bz2"} or
            (ext in {".rar", ".7z"} and SEVENZIP_OK)
        )
        if ext in {".rar", ".7z"} and not SEVENZIP_OK:
            job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ 7-Zip not found — keeping {filename} as-is"})
        if _can_extract and dest.exists():
            # Try to get real name from bunkr page (CDN renames files to opaque slugs)
            try:
                _title_html = fetch_page(page_url, session, max_retries=1)
                _orig_title = filename_from_page_html(_title_html)
                if _orig_title and get_ext(_orig_title) in ZIP_EXTS:
                    zip_stem = sanitize(Path(_orig_title).stem) or sanitize(Path(filename).stem) or f"zip_{idx}"
                else:
                    zip_stem = sanitize(Path(filename).stem) or f"zip_{idx}"
            except Exception:
                zip_stem = sanitize(Path(filename).stem) or f"zip_{idx}"
            extract_dir = out_dir / zip_stem
            nested_album = f"{album_name}/{zip_stem}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                extracted_files = []
                if ext == ".zip":
                    with zipfile.ZipFile(dest, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            mname = Path(member.filename).name
                            if not mname or mname.startswith("."):
                                continue
                            target = extract_dir / mname
                            if target.exists():
                                stem2, sfx2 = Path(mname).stem, Path(mname).suffix
                                target = extract_dir / f"{stem2}_{uuid.uuid4().hex[:6]}{sfx2}"
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            ext2 = target.suffix.lower()
                            ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                            if ftype2:
                                extracted_files.append({"name": target.name, "album": nested_album, "type": ftype2, "size": target.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                elif ext in {".tar", ".gz", ".tar.gz", ".tar.bz2"}:
                    with tarfile.open(dest, "r:*") as tf:
                        for member in tf.getmembers():
                            if not member.isfile():
                                continue
                            mname = Path(member.name).name
                            if not mname or mname.startswith("."):
                                continue
                            target = extract_dir / mname
                            if target.exists():
                                stem2, sfx2 = Path(mname).stem, Path(mname).suffix
                                target = extract_dir / f"{stem2}_{uuid.uuid4().hex[:6]}{sfx2}"
                            src_f = tf.extractfile(member)
                            if src_f:
                                with src_f, open(target, "wb") as dst:
                                    shutil.copyfileobj(src_f, dst)
                            ext2 = target.suffix.lower()
                            ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                            if ftype2:
                                extracted_files.append({"name": target.name, "album": nested_album, "type": ftype2, "size": target.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                elif ext in {".rar", ".7z"}:
                    result = subprocess.run(
                        [_7Z_CMD, "e", str(dest), f"-o{extract_dir}", "-y"],
                        capture_output=True, text=True, timeout=300
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip()[:300] or f"7-Zip exited with code {result.returncode}")
                    for ef in sorted(extract_dir.iterdir()):
                        if not ef.is_file() or ef.name.startswith("."):
                            continue
                        ext2 = ef.suffix.lower()
                        ftype2 = "image" if ext2 in IMAGE_EXTS else ("video" if ext2 in VIDEO_EXTS else None)
                        if ftype2:
                            extracted_files.append({"name": ef.name, "album": nested_album, "type": ftype2, "size": ef.stat().st_size, "thumb": None, "album_url": jobs[job_id].get("url")})
                dest.unlink()
                job["log"].append({"type": "ok", "msg": f"[{idx}] ✓ Extracted: {filename} → {zip_stem}/ ({len(extracted_files)} file(s))"})
                with jobs_lock:
                    job["files"].extend(extracted_files)
                    job["success"] += 1
                    job["zip_folders"].append({"dir": str(extract_dir), "album": nested_album})
                with jobs_lock:
                    job["done"] += 1
                    job["file_progress"][str(idx)] = {"downloaded": 0, "total": 0, "speed": 0}
                    job["file_speeds"][str(idx)] = 0
                with bunkrinfo_lock:
                    _einfo = _bunkrinfo_read(extract_dir)
                    _einfo["url"] = page_url  # Set the album URL to the ZIP file's page
                    _efiles = _einfo.setdefault("files", {})
                    for ef in extracted_files:
                        _efiles[ef["name"]] = page_url
                    _bunkrinfo_write(extract_dir, _einfo)
                return True
            except Exception as ex:
                job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ Extraction failed, keeping archive: {ex}"})
                try:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                except Exception:
                    pass
        thumb_rel = None
        if file_type == "video" and FFMPEG_OK:
            thumb_rel = generate_video_thumbnail(dest, album_name)
            if thumb_rel:
                job["log"].append({"type": "info", "msg": f"[{idx}] 🖼 Video thumbnail: {thumb_rel}"})
            else:
                job["log"].append({"type": "warn", "msg": f"[{idx}] ⚠ Video thumbnail generation failed"})
        elif file_type == "image":
            thumb_rel = generate_image_thumbnail(dest, album_name)
            if thumb_rel:
                job["log"].append({"type": "info", "msg": f"[{idx}] 🖼 Image thumbnail: {thumb_rel}"})
        job["log"].append({"type": "ok", "msg": f"[{idx}] ✓ {filename}"})
        with bunkrinfo_lock:
            _info = _bunkrinfo_read(out_dir)
            _info.setdefault("files", {})[filename] = page_url
            _bunkrinfo_write(out_dir, _info)
        with jobs_lock:
            job["files"].append({"name": filename, "album": album_name, "type": file_type, "size": file_size, "thumb": thumb_rel})
            job["success"] += 1
    else:
        job["log"].append({"type": "error", "msg": f"[{idx}] ✗ Download failed: {filename}"})
        with jobs_lock:
            job["failed"] += 1
            job["failed_tasks"].append({
                "idx": idx, "page_url": page_url, "album_name": album_name,
                "out_dir": str(out_dir), "total": total, "reason": "Download failed after retries"
            })

    with jobs_lock:
        job["done"] += 1
        job["file_progress"][str(idx)] = {"downloaded": 0, "total": 0, "speed": 0}
        job["file_speeds"][str(idx)] = 0

    return ok

# ─── Job runner ────────────────────────────────────────────────────────────────

def run_job(job_id, album_url, concurrency_images, concurrency_videos):
    job = jobs[job_id]
    session = requests.Session()

    # Ensure global pools are initialized with current settings
    _ensure_global_pools(concurrency_images, concurrency_videos)

    job["log"].append({"type": "info", "msg": f"Fetching album: {album_url}"})
    if not PLAYWRIGHT_OK:
        job["log"].append({"type": "warn", "msg": "⚠ Playwright not installed — video pages may fail."})
    if not FFMPEG_OK:
        job["log"].append({"type": "warn", "msg": "⚠ ffmpeg not found — video thumbnails disabled."})
    if not SEVENZIP_OK:
        job["log"].append({"type": "warn", "msg": "⚠ 7-Zip not found — .rar and .7z archives will not be extracted."})

    album_name, file_pages = extract_file_links(album_url, session, job["log"])

    if not file_pages:
        job["log"].append({"type": "error", "msg": "No files found in album."})
        job["status"] = "failed"
        return

    only = job.get("only_file")
    if only:
        before_n = len(file_pages)
        file_pages = filter_pages_by_only(file_pages, only)
        if not file_pages:
            job["log"].append({"type": "error", "msg": f'No file matched "{only}" in this album.'})
            job["status"] = "failed"
            return
        job["log"].append({"type": "info", "msg": f"Downloading 1 file ({only}) — {before_n} total in album"})

    folder_album_name, out_dir = unique_album_dir(album_name, album_url)
    if folder_album_name != album_name:
        job["log"].append({"type": "warn", "msg": f'Album folder "{album_name}" already exists — saving this album as "{folder_album_name}"'})

    job["album_name"] = folder_album_name
    job["total"] = len(file_pages)
    job["log"].append({"type": "info", "msg": f'Album: "{folder_album_name}" — {len(file_pages)} file(s) in this job'})

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".bunkrinfo").write_text(json.dumps({"url": album_url}), encoding="utf-8")

    free_bytes = get_free_disk_bytes()
    if free_bytes is not None:
        rough_estimate = len(file_pages) * 50 * 1024 * 1024
        if free_bytes < rough_estimate * 0.5:
            job["log"].append({"type": "warn", "msg": f"⚠ Low disk space: {format_size(free_bytes)} free"})

    # Submit all tasks to global thread pools
    job["log"].append({"type": "info", "msg": f"Using adaptive pools: up to {concurrency_images} image and {concurrency_videos} video/zip streams; automatically reduces on throttling"})
    
    futures = []
    for idx, page_url in enumerate(file_pages, 1):
        task = (idx, page_url, out_dir, folder_album_name, len(file_pages))
        future = global_resolver_pool.submit(_process_task_with_pool_selection, job_id, task, session)
        futures.append(future)
    
    # Wait for all tasks to complete
    for future in as_completed(futures):
        if job.get("stop_requested"):
            # Cancel remaining futures
            for f in futures:
                f.cancel()
            break
        try:
            future.result()
        except Exception as e:
            job["log"].append({"type": "error", "msg": f"Task execution error: {e}"})

    # Clean up if stopped
    if job.get("stop_requested"):
        job["log"].append({"type": "warn", "msg": f"⏹ Stopped by user — ✓ {job.get('success',0)} downloaded, ✗ {job.get('failed',0)} failed, ↷ {job.get('skipped',0)} skipped"})
        job["status"] = "done"
        job["paused"] = False
        job["pause_requested"] = False
        return

    # If only 1 zip was downloaded in this album, flatten its subfolder to the album root
    zip_folders = job.get("zip_folders", [])
    if len(zip_folders) == 1:
        sole = zip_folders[0]
        sole_dir = Path(sole["dir"])
        nested_album = sole["album"]
        if sole_dir.is_dir():
            with bunkrinfo_lock:
                _nested_info = _bunkrinfo_read(sole_dir)
            _nested_srcs = _nested_info.get("files", {})
            _rename_map = {}
            for f in list(sole_dir.iterdir()):
                if f.name == ".bunkrinfo":
                    f.unlink(missing_ok=True)
                    continue
                target = out_dir / f.name
                if target.exists():
                    target = out_dir / f"{f.stem}_{uuid.uuid4().hex[:6]}{f.suffix}"
                shutil.move(str(f), str(target))
                _rename_map[f.name] = target.name
            if _nested_srcs:
                with bunkrinfo_lock:
                    _main_info = _bunkrinfo_read(out_dir)
                    _main_files = _main_info.setdefault("files", {})
                    for _old, _src_url in _nested_srcs.items():
                        _main_files[_rename_map.get(_old, _old)] = _src_url
                    _bunkrinfo_write(out_dir, _main_info)
            try:
                sole_dir.rmdir()
            except Exception:
                pass
            for item in job["files"]:
                if item.get("album") == nested_album:
                    item["album"] = folder_album_name
            job["log"].append({"type": "info", "msg": "Single zip — extracted files moved to album root"})

    job["log"].append({"type": "ok", "msg": f"Done — ✓ {job.get('success',0)} downloaded, ✗ {job.get('failed',0)} failed, ↷ {job.get('skipped',0)} skipped"})
    job["status"] = "done"
    job["paused"] = False
    job["pause_requested"] = False

    # Generate thumbnails for downloaded files (recurses into zip subfolders for multi-zip albums)
    def generate_thumbs_bg():
        try:
            job["log"].append({"type": "info", "msg": "🔍 Rechecking thumbnails for all files..."})
            newly_generated = 0
            still_missing = 0
            for f in out_dir.rglob("*"):
                if f.name.startswith(".") or f.is_dir():
                    continue
                ext = f.suffix.lower()
                if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
                    continue
                if f.stat().st_size == 0:
                    continue
                try:
                    album_for_thumb = str(f.parent.relative_to(DOWNLOADS_DIR)).replace("\\", "/")
                except ValueError:
                    album_for_thumb = folder_album_name
                thumb_rel = None
                if ext in IMAGE_EXTS:
                    thumb_rel = generate_image_thumbnail(f, album_for_thumb)
                elif ext in VIDEO_EXTS:
                    thumb_rel = generate_video_thumbnail(f, album_for_thumb)
                if thumb_rel:
                    newly_generated += 1
                    with jobs_lock:
                        for item in job["files"]:
                            if item.get("name") == f.name and not item.get("thumb"):
                                item["thumb"] = thumb_rel
                                break
                else:
                    still_missing += 1
                    job["log"].append({"type": "warn", "msg": f"⚠ Thumbnail missing after recheck: {f.name}"})
            job["log"].append({"type": "ok", "msg": f"🖼 Thumbnail recheck done — {newly_generated} generated, {still_missing} still missing"})
        except Exception as e:
            print(f"  [Thumbnail] Background generation error: {e}")
    
    threading.Thread(target=generate_thumbs_bg, daemon=True).start()
    
    # Add to history
    try:
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                history = []
        
        entry = {
            "album_name": folder_album_name,
            "album_url": album_url,
            "file_count": job.get('success', 0),
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if album_url:
            history = [h for h in history if h.get("album_url") != album_url]
        
        history.insert(0, entry)
        history = history[:100]
        
        HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [History] Failed to add entry: {e}")


def run_retry_job(job_id, failed_tasks, concurrency_images, concurrency_videos):
    job = jobs[job_id]
    session = requests.Session()
    
    # Ensure global pools are initialized with current settings
    _ensure_global_pools(concurrency_images, concurrency_videos)
    
    job["log"].append({"type": "info", "msg": f"Retrying {len(failed_tasks)} failed file(s)..."})
    job["log"].append({"type": "info", "msg": f"Using adaptive pools: up to {concurrency_images} image and {concurrency_videos} video/zip streams; automatically reduces on throttling"})
    job["total"] = len(failed_tasks)

    # Submit all retry tasks to global thread pools
    futures = []
    for i, ft in enumerate(failed_tasks, 1):
        out_dir = Path(ft["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        task = (i, ft["page_url"], out_dir, ft["album_name"], len(failed_tasks))
        future = global_resolver_pool.submit(_process_task_with_pool_selection, job_id, task, session)
        futures.append(future)

    # Wait for all tasks to complete
    for future in as_completed(futures):
        if job.get("stop_requested"):
            # Cancel remaining futures
            for f in futures:
                f.cancel()
            break
        try:
            future.result()
        except Exception as e:
            job["log"].append({"type": "error", "msg": f"Retry task execution error: {e}"})

    # Clean up if stopped
    if job.get("stop_requested"):
        # Drain the queue to prevent it from blocking
        while not task_q.empty():
            try:
                task_q.get(block=False)
                task_q.task_done()
            except Exception:
                break
        job["log"].append({"type": "warn", "msg": f"⏹ Stopped by user — ✓ {job.get('success',0)} downloaded, ✗ {job.get('failed',0)} still failed"})
        job["status"] = "done"
        job["paused"] = False
        job["pause_requested"] = False
        return

    job["log"].append({"type": "ok", "msg": f"Retry done — ✓ {job.get('success',0)} downloaded, ✗ {job.get('failed',0)} still failed, ↷ {job.get('skipped',0)} skipped"})
    job["status"] = "done"
    job["paused"] = False
    job["pause_requested"] = False

# ─── Preview incremental sizes (background + SSE) ────────────────────────────

def _preview_resolve_size(idx, page_url):
    """
    Resolve Bunkr file page → CDN URL, then probe size.
    Tries fast static HTML first; if no URL or no measurable size, uses Playwright (serialized).
    """
    sess = requests.Session()
    sz = None
    direct = None
    try:
        plog = []
        direct = resolve_direct_url(
            page_url, sess, plog, max_retries=2, allow_playwright=False
        )
        if direct:
            sz = preflight_size(direct, sess)

        need_js = direct is None or sz is None
        if need_js:
            plog2 = []
            direct_js = resolve_direct_url(
                page_url, sess, plog2, max_retries=2, allow_playwright=True
            )
            target = direct_js or direct
            if target:
                new_sz = preflight_size(target, sess)
                if new_sz is not None:
                    sz = new_sz
    except Exception:
        pass
    return idx, sz


def _preview_sizes_worker(preview_id, file_pages):
    q = None
    with preview_sessions_lock:
        s = preview_sessions.get(preview_id)
        if not s:
            return
        q = s["queue"]

    workers = min(PREVIEW_MAX_WORKERS, max(1, len(file_pages)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_preview_resolve_size, i, p): i
            for i, p in enumerate(file_pages)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                idx, sz = fut.result()
            except Exception:
                idx, sz = i, None
            with preview_sessions_lock:
                sp = preview_sessions.get(preview_id)
                if sp and not sp.get("cancelled"):
                    q.put({"idx": idx, "size": sz})


def _expire_preview_session_later(preview_id):
    def _later():
        time.sleep(PREVIEW_SESSION_MAX_AGE)
        with preview_sessions_lock:
            preview_sessions.pop(preview_id, None)

    threading.Thread(target=_later, daemon=True).start()


# ─── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/preview", methods=["POST"])
def preview_album():
    """
    Returns album listing immediately (sizes null). Sizes stream at GET /api/preview/stream/<id>.
    """
    data = request.json
    url = (data.get("url") or "").strip()
    if not url or "bunkr" not in url:
        return jsonify({"error": "Invalid Bunkr URL"}), 400
    try:
        session = requests.Session()
        album_name, file_pages = extract_file_links(url, session)
        if not file_pages:
            return jsonify({"error": "No files found in album."}), 404

        preview_id = uuid.uuid4().hex[:12]
        preview_files = []
        for p in file_pages:
            slug = p.rstrip("/").split("/")[-1]
            preview_files.append({
                "name": slug,
                "page_url": p,
                "url": p,
                "size": None,
            })

        with preview_sessions_lock:
            preview_sessions[preview_id] = {
                "queue": queue.Queue(),
                "total": len(file_pages),
                "cancelled": False,
            }

        threading.Thread(
            target=_preview_sizes_worker,
            args=(preview_id, file_pages),
            daemon=True,
        ).start()
        _expire_preview_session_later(preview_id)

        return jsonify({
            "preview_id": preview_id,
            "album_name": album_name,
            "file_count": len(file_pages),
            "files": preview_files,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/preview/stream/<preview_id>")
def preview_stream(preview_id):
    """SSE: emits {"idx": n, "size": bytes|null} per file, then {"done": true}."""
    with preview_sessions_lock:
        sess = preview_sessions.get(preview_id)
        if not sess:
            return jsonify({"error": "Preview session expired or invalid."}), 404

    total = sess["total"]

    def generate():
        got = 0
        q = sess["queue"]
        try:
            while got < total:
                try:
                    item = q.get(timeout=180)
                except queue.Empty:
                    yield f"data: {json.dumps({'error': 'timed out waiting for sizes'})}\n\n"
                    break
                yield f"data: {json.dumps(item)}\n\n"
                got += 1
            yield f"data: {json.dumps({'done': True})}\n\n"
        finally:
            with preview_sessions_lock:
                preview_sessions.pop(preview_id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = (data.get("url") or "").strip()
    only_file = (data.get("only_file") or "").strip()
    concurrency_images = max(1, min(MAX_CONCURRENCY_IMAGES, int(data.get("concurrency_images", DEFAULT_CONCURRENCY_IMAGES))))
    concurrency_videos = max(1, min(MAX_CONCURRENCY_VIDEOS, int(data.get("concurrency_videos", DEFAULT_CONCURRENCY_VIDEOS))))
    max_retries = max(1, min(10, int(data.get("max_retries", DEFAULT_MAX_RETRIES))))

    if not url or "bunkr" not in url:
        return jsonify({"error": "Invalid Bunkr URL"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running", "url": url, "album_name": "",
        "total": 0, "current": 0, "done": 0,
        "success": 0, "failed": 0, "skipped": 0,
        "log": [], "files": [],
        "file_progress": {}, "file_speeds": {}, "file_sizes": {},
        "failed_tasks": [],
        "pause_requested": False, "paused": False, "stop_requested": False,
        "concurrency_images": concurrency_images,
        "concurrency_videos": concurrency_videos,
        "max_retries": max_retries,
        "only_file": only_file or None,
        "zip_folders": [],
    }
    threading.Thread(target=run_job, args=(job_id, url, concurrency_images, concurrency_videos), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>/retry_failed", methods=["POST"])
def retry_failed(job_id):
    original_job = jobs.get(job_id)
    if not original_job:
        return jsonify({"error": "Job not found"}), 404
    failed_tasks = original_job.get("failed_tasks", [])
    if not failed_tasks:
        return jsonify({"error": "No failed files to retry"}), 400

    data = request.get_json() or {}
    concurrency_images = max(1, min(MAX_CONCURRENCY_IMAGES, int(data.get("concurrency_images") or original_job.get("concurrency_images", DEFAULT_CONCURRENCY_IMAGES))))
    concurrency_videos = max(1, min(MAX_CONCURRENCY_VIDEOS, int(data.get("concurrency_videos") or original_job.get("concurrency_videos", DEFAULT_CONCURRENCY_VIDEOS))))
    max_retries = max(1, min(10, int(data.get("max_retries") or original_job.get("max_retries", DEFAULT_MAX_RETRIES))))
    new_job_id = str(uuid.uuid4())[:8]
    jobs[new_job_id] = {
        "status": "running", "url": original_job["url"],
        "album_name": original_job.get("album_name", ""),
        "total": len(failed_tasks), "current": 0, "done": 0,
        "success": 0, "failed": 0, "skipped": 0,
        "log": [], "files": [],
        "file_progress": {}, "file_speeds": {}, "file_sizes": {},
        "failed_tasks": [],
        "pause_requested": False, "paused": False, "stop_requested": False,
        "concurrency_images": concurrency_images,
        "concurrency_videos": concurrency_videos,
        "max_retries": max_retries,
    }
    original_job["failed_tasks"] = []
    threading.Thread(target=run_retry_job, args=(new_job_id, failed_tasks, concurrency_images, concurrency_videos), daemon=True).start()
    return jsonify({"job_id": new_job_id})


@app.route("/api/job/<job_id>/retry_one", methods=["POST"])
def retry_one(job_id):
    original_job = jobs.get(job_id)
    if not original_job:
        return jsonify({"error": "Job not found"}), 404
    data = request.get_json() or {}
    target_idx = str(data.get("idx", ""))
    failed_tasks = original_job.get("failed_tasks", [])
    task = next((t for t in failed_tasks if str(t["idx"]) == target_idx), None)
    if not task:
        return jsonify({"error": "Task not found in failed list"}), 404
    concurrency_images = max(1, min(MAX_CONCURRENCY_IMAGES, int(data.get("concurrency_images") or original_job.get("concurrency_images", DEFAULT_CONCURRENCY_IMAGES))))
    concurrency_videos = max(1, min(MAX_CONCURRENCY_VIDEOS, int(data.get("concurrency_videos") or original_job.get("concurrency_videos", DEFAULT_CONCURRENCY_VIDEOS))))
    max_retries = max(1, min(10, int(data.get("max_retries") or original_job.get("max_retries", DEFAULT_MAX_RETRIES))))
    new_job_id = str(uuid.uuid4())[:8]
    jobs[new_job_id] = {
        "status": "running", "url": original_job.get("url", ""),
        "album_name": original_job.get("album_name", ""),
        "total": 1, "current": 0, "done": 0,
        "success": 0, "failed": 0, "skipped": 0,
        "log": [], "files": [],
        "file_progress": {}, "file_speeds": {}, "file_sizes": {},
        "failed_tasks": [],
        "pause_requested": False, "paused": False, "stop_requested": False,
        "concurrency_images": concurrency_images,
        "concurrency_videos": concurrency_videos,
        "max_retries": max_retries,
    }
    original_job["failed_tasks"] = [t for t in failed_tasks if str(t["idx"]) != target_idx]
    threading.Thread(target=run_retry_job, args=(new_job_id, [task], concurrency_images, concurrency_videos), daemon=True).start()
    return jsonify({"job_id": new_job_id})


@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    result = dict(job)
    full_log = result["log"]
    result["log_total"] = len(full_log)
    log_since = request.args.get("log_since", type=int)
    if log_since is not None and log_since >= 0:
        result["log"] = full_log[log_since:]
    result["total_speed"] = sum(job["file_speeds"].values())
    result["total_downloaded"] = sum(p.get("downloaded", 0) for p in job["file_progress"].values())
    result["ffmpeg_ok"] = FFMPEG_OK
    result["playwright_ok"] = PLAYWRIGHT_OK
    result["failed_count"] = len(job.get("failed_tasks", []))
    if request.args.get("diag"):
        result["diagnostic_meta"] = {
            "app": f"BunkrWrap Web UI v{VERSION}",
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "cwd": str(Path.cwd()),
            "downloads_dir": str(DOWNLOADS_DIR.resolve()),
            "thumbnails_dir": str(THUMBS_DIR.resolve()),
        }
    return jsonify(result)


@app.route("/api/job/<job_id>/pause", methods=["POST"])
def pause_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["pause_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/resume", methods=["POST"])
def resume_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["pause_requested"] = False
    job["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/stop", methods=["POST"])
def stop_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["stop_requested"] = True
    job["pause_requested"] = False
    job["paused"] = False
    job["status"] = "done"
    job["log"].append({"type": "warn", "msg": f"⏹ Stopped by user — ✓ {job.get('success',0)} downloaded so far"})
    return jsonify({"ok": True})


@app.route("/api/gallery")
def gallery():
    items = []
    for album_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not album_dir.is_dir():
            continue
        album_url = None
        file_sources = {}
        info_file = album_dir / ".bunkrinfo"
        if info_file.exists():
            try:
                _bi = json.loads(info_file.read_text(encoding="utf-8"))
                album_url = _bi.get("url")
                file_sources = _bi.get("files", {})
            except Exception:
                pass
        for f in sorted(album_dir.iterdir()):
            if f.name.startswith("."):
                continue
            if f.is_dir():
                # Nested album — scan one level deeper
                nested_name = f"{album_dir.name}/{f.name}"
                nested_url = None
                nested_file_sources = {}
                nested_info = f / ".bunkrinfo"
                if nested_info.exists():
                    try:
                        _nbi = json.loads(nested_info.read_text(encoding="utf-8"))
                        nested_url = _nbi.get("url")
                        nested_file_sources = _nbi.get("files", {})
                    except Exception:
                        pass
                for nf in sorted(f.iterdir()):
                    if nf.name.startswith(".") or nf.is_dir():
                        continue
                    ext = nf.suffix.lower()
                    nsrc = nested_file_sources.get(nf.name)
                    if ext in IMAGE_EXTS:
                        thumb_path = THUMBS_DIR / album_dir.name / f.name / (nf.stem + "_thumb.jpg")
                        thumb_url = f"/thumbs/{album_dir.name}/{f.name}/{thumb_path.name}" if thumb_path.exists() else None
                        items.append({"name": nf.name, "album": nested_name, "type": "image", "size": nf.stat().st_size, "mtime": nf.stat().st_mtime, "thumb": thumb_url, "album_url": nested_url, "source_url": nsrc})
                    elif ext in VIDEO_EXTS:
                        thumb_path = THUMBS_DIR / album_dir.name / f.name / (nf.stem + ".jpg")
                        thumb_url = f"/thumbs/{album_dir.name}/{f.name}/{thumb_path.name}" if thumb_path.exists() else None
                        items.append({"name": nf.name, "album": nested_name, "type": "video", "size": nf.stat().st_size, "mtime": nf.stat().st_mtime, "thumb": thumb_url, "album_url": nested_url, "source_url": nsrc})
                    elif ext in ZIP_EXTS:
                        items.append({"name": nf.name, "album": nested_name, "type": "zip", "size": nf.stat().st_size, "mtime": nf.stat().st_mtime, "thumb": None, "album_url": nested_url, "source_url": nsrc})
                continue
            ext = f.suffix.lower()
            src = file_sources.get(f.name)
            if ext in IMAGE_EXTS:
                thumb_path = THUMBS_DIR / album_dir.name / (f.stem + "_thumb.jpg")
                thumb_url = f"/thumbs/{album_dir.name}/{thumb_path.name}" if thumb_path.exists() else None
                items.append({"name": f.name, "album": album_dir.name, "type": "image", "size": f.stat().st_size, "mtime": f.stat().st_mtime, "thumb": thumb_url, "album_url": album_url, "source_url": src})
            elif ext in VIDEO_EXTS:
                thumb_path = THUMBS_DIR / album_dir.name / (f.stem + ".jpg")
                thumb_url = f"/thumbs/{album_dir.name}/{thumb_path.name}" if thumb_path.exists() else None
                items.append({"name": f.name, "album": album_dir.name, "type": "video", "size": f.stat().st_size, "mtime": f.stat().st_mtime, "thumb": thumb_url, "album_url": album_url, "source_url": src})
            elif ext in ZIP_EXTS:
                items.append({"name": f.name, "album": album_dir.name, "type": "zip", "size": f.stat().st_size, "mtime": f.stat().st_mtime, "thumb": None, "album_url": album_url, "source_url": src})
    return jsonify(items)


@app.route("/api/albums")
def list_albums():
    result = []
    if DOWNLOADS_DIR.exists():
        for d in sorted(DOWNLOADS_DIR.iterdir()):
            if not d.is_dir():
                continue
            album_url = None
            info_file = d / ".bunkrinfo"
            if info_file.exists():
                try:
                    album_url = json.loads(info_file.read_text(encoding="utf-8")).get("url")
                except Exception:
                    pass
            result.append({"name": d.name, "album_url": album_url})
            # Include nested albums (one level deep)
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    sub_url = None
                    sub_info = sub / ".bunkrinfo"
                    if sub_info.exists():
                        try:
                            sub_url = json.loads(sub_info.read_text(encoding="utf-8")).get("url")
                        except Exception:
                            pass
                    result.append({"name": f"{d.name}/{sub.name}", "album_url": sub_url})
    return jsonify(result)


@app.route("/api/albums/mtime")
def albums_mtime():
    """Return the max mtime across DOWNLOADS_DIR and its immediate subdirs.
    Used by the frontend for fast change detection without a full gallery fetch."""
    try:
        if not DOWNLOADS_DIR.exists():
            return jsonify({"mtime": 0})
        mt = DOWNLOADS_DIR.stat().st_mtime
        for d in DOWNLOADS_DIR.iterdir():
            if d.is_dir():
                try:
                    mt = max(mt, d.stat().st_mtime)
                except OSError:
                    pass
        return jsonify({"mtime": mt})
    except Exception:
        return jsonify({"mtime": 0})


@app.route("/api/album/create", methods=["POST"])
def album_create():
    data   = request.get_json(silent=True) or {}
    name   = sanitize((data.get("name") or "").strip())
    parent = (data.get("parent") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    
    # Validate album name
    try:
        validate_album_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    if parent:
        # Validate parent album name
        try:
            validate_album_path(parent)
        except ValueError as e:
            return jsonify({"error": f"Invalid parent: {e}"}), 400
            
        parent_path = DOWNLOADS_DIR / parent
        if not parent_path.is_dir():
            return jsonify({"error": "Parent album not found"}), 404
        full_name = f"{parent}/{name}"
        path = parent_path / name
    else:
        full_name = name
        path = DOWNLOADS_DIR / name
    if path.exists():
        return jsonify({"error": "Album already exists"}), 409
    path.mkdir(parents=True)
    return jsonify({"ok": True, "name": full_name})


@app.route("/api/album/nest", methods=["POST"])
def album_nest():
    data      = request.get_json(silent=True) or {}
    src_album = (data.get("source") or "").strip()
    dst_album = (data.get("target") or "").strip()
    if not src_album or not dst_album:
        return jsonify({"error": "source and target required"}), 400
    
    # Validate both album names
    try:
        validate_album_path(src_album)
        validate_album_path(dst_album)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    src_path   = DOWNLOADS_DIR / src_album
    dst_parent = DOWNLOADS_DIR / dst_album
    dst_path   = dst_parent / src_album
    if not src_path.is_dir():
        return jsonify({"error": "Source album not found"}), 404
    if not dst_parent.is_dir():
        return jsonify({"error": "Target album not found"}), 404
    if dst_path.exists():
        return jsonify({"error": "Destination already exists"}), 409
    try:
        shutil.move(str(src_path), str(dst_path))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Move thumbnails directory if present
    src_thumbs = THUMBS_DIR / src_album
    if src_thumbs.is_dir():
        dst_thumbs_parent = THUMBS_DIR / dst_album
        dst_thumbs_parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src_thumbs), str(dst_thumbs_parent / src_album))
        except Exception:
            pass
    return jsonify({"ok": True, "new_album": f"{dst_album}/{src_album}"})


@app.route("/api/album/rename", methods=["POST"])
def album_rename():
    data = request.get_json(silent=True) or {}
    old     = (data.get("old_name") or "").strip()
    new_raw = (data.get("new_name") or "").strip()
    if not old or not new_raw:
        return jsonify({"error": "old_name and new_name required"}), 400
    
    # Validate old album name
    try:
        validate_album_path(old)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    new = sanitize(new_raw)
    if not new:
        return jsonify({"error": "Invalid new name"}), 400
    
    # Validate new album name
    try:
        validate_album_path(new)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    src = DOWNLOADS_DIR / old
    dst = DOWNLOADS_DIR / new
    if not src.is_dir():
        return jsonify({"error": "Album not found"}), 404
    if dst.exists():
        return jsonify({"error": "Name already taken"}), 409
    
    # Rename album directory
    try:
        src.rename(dst)
    except Exception as e:
        return jsonify({"error": f"Failed to rename album: {e}"}), 500
    
    # Rename thumbnails directory if present
    tsrc = THUMBS_DIR / old
    if tsrc.exists():
        try:
            tsrc.rename(THUMBS_DIR / new)
        except Exception as e:
            # Rollback album rename if thumbnail rename fails
            try:
                dst.rename(src)
            except Exception:
                pass
            return jsonify({"error": f"Failed to rename thumbnails: {e}"}), 500
    
    return jsonify({"ok": True, "new_name": new})


@app.route("/api/album/delete", methods=["POST"])
def album_delete():
    data = request.get_json(silent=True) or {}
    album_name = (data.get("album_name") or "").strip()
    if not album_name:
        return jsonify({"error": "album_name required"}), 400
    
    # Validate album path to prevent directory traversal
    try:
        validate_album_path(album_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    album_dir = DOWNLOADS_DIR / album_name
    if not album_dir.exists():
        return jsonify({"error": "Album not found"}), 404
    
    # Count files before deletion
    file_count = sum(1 for f in album_dir.iterdir() if f.is_file())
    
    # Delete album directory and all contents
    shutil.rmtree(album_dir)
    
    # Delete thumbnails directory if exists
    thumb_dir = THUMBS_DIR / album_name
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)
    
    return jsonify({"ok": True, "deleted_files": file_count})


@app.route("/api/albums/merge", methods=["POST"])
def albums_merge():
    """
    Task 6.3: Merge multiple albums into a target album
    Requirements 2.4.2, 2.4.3, 2.4.4, 3.3.1
    """
    data = request.get_json(silent=True) or {}
    source_albums = data.get("sourceAlbums", [])
    target_album_name = (data.get("targetAlbumName") or "").strip()
    
    if not source_albums or not isinstance(source_albums, list):
        return jsonify({"success": False, "error": "sourceAlbums array required"}), 400
    
    if not target_album_name:
        return jsonify({"success": False, "error": "targetAlbumName required"}), 400
    
    # Validate all album names
    try:
        for album_name in source_albums:
            validate_album_path(album_name)
        validate_album_path(target_album_name)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    
    # Check that source albums exist
    for album_name in source_albums:
        album_dir = DOWNLOADS_DIR / album_name
        if not album_dir.exists():
            return jsonify({"success": False, "error": f"Source album not found: {album_name}"}), 404
    
    # Create target album directory if it doesn't exist
    target_dir = DOWNLOADS_DIR / target_album_name
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Create target thumbnails directory
    target_thumb_dir = THUMBS_DIR / target_album_name
    target_thumb_dir.mkdir(parents=True, exist_ok=True)
    
    files_moved = 0
    errors = []
    
    # Task 6.4: Handle duplicate filenames during merge
    def resolve_filename_conflict(target_dir, filename):
        """Generate unique filename if conflict exists"""
        if not (target_dir / filename).exists():
            return filename
        
        # Split filename and extension
        stem = Path(filename).stem
        ext = Path(filename).suffix
        
        # Try numbered suffixes until we find a unique name
        counter = 1
        while True:
            new_filename = f"{stem}_{counter}{ext}"
            if not (target_dir / new_filename).exists():
                return new_filename
            counter += 1
    
    # Merge each source album
    for source_album in source_albums:
        source_dir = DOWNLOADS_DIR / source_album
        source_thumb_dir = THUMBS_DIR / source_album
        
        try:
            # Move all files from source to target
            for file_path in source_dir.iterdir():
                if file_path.is_file() and file_path.name != ".bunkrinfo":
                    # Task 6.4: Resolve filename conflicts
                    target_filename = resolve_filename_conflict(target_dir, file_path.name)
                    target_path = target_dir / target_filename
                    
                    # Move file
                    shutil.move(str(file_path), str(target_path))
                    files_moved += 1
                    
                    # Move thumbnail if exists
                    thumb_name = file_path.stem + ".jpg"
                    source_thumb = source_thumb_dir / thumb_name
                    if source_thumb.exists():
                        target_thumb_name = Path(target_filename).stem + ".jpg"
                        target_thumb = target_thumb_dir / target_thumb_name
                        shutil.move(str(source_thumb), str(target_thumb))
            
            # Merge .bunkrinfo metadata
            source_info = _bunkrinfo_read(source_dir)
            target_info = _bunkrinfo_read(target_dir)
            
            # Merge file mappings
            if "files" in source_info:
                target_files = target_info.setdefault("files", {})
                for filename, url in source_info["files"].items():
                    # Use resolved filename if there was a conflict
                    resolved_name = resolve_filename_conflict(target_dir, filename)
                    target_files[resolved_name] = url
                target_info["files"] = target_files
            
            # Preserve album_url if target doesn't have one
            if "album_url" in source_info and "album_url" not in target_info:
                target_info["album_url"] = source_info["album_url"]
            
            _bunkrinfo_write(target_dir, target_info)
            
            # Delete source album directory
            shutil.rmtree(source_dir)
            
            # Delete source thumbnails directory
            if source_thumb_dir.exists():
                shutil.rmtree(source_thumb_dir)
                
        except Exception as e:
            errors.append({"album": source_album, "error": str(e)})
    
    if errors:
        return jsonify({
            "success": False,
            "targetAlbum": target_album_name,
            "filesMoved": files_moved,
            "errors": errors
        }), 500
    
    return jsonify({
        "success": True,
        "targetAlbum": target_album_name,
        "filesMoved": files_moved,
        "errors": []
    })


@app.route("/api/album/move-file", methods=["POST"])
@app.route("/api/move", methods=["POST"])  # Alias for convenience
def album_move_file():
    data      = request.get_json(silent=True) or {}
    src_album = (data.get("album") or "").strip()
    filename  = (data.get("filename") or "").strip()
    dst_album = (data.get("target_album") or "").strip()
    if not src_album or not filename or not dst_album:
        return jsonify({"error": "album, filename, target_album required"}), 400
    src_path = DOWNLOADS_DIR / src_album / filename
    dst_dir  = DOWNLOADS_DIR / dst_album
    if not src_path.is_file():
        return jsonify({"error": "File not found"}), 404
    if not dst_dir.is_dir():
        return jsonify({"error": "Target album not found"}), 404
    dst_path = dst_dir / filename
    if dst_path.exists():
        stem2, sfx2 = Path(filename).stem, Path(filename).suffix
        dst_path = dst_dir / f"{stem2}_{uuid.uuid4().hex[:6]}{sfx2}"
    shutil.move(str(src_path), str(dst_path))
    # Move thumbnail if it exists
    src_thumb = THUMBS_DIR / src_album / (Path(filename).stem + "_thumb.jpg")
    if src_thumb.exists():
        dst_thumb_dir = THUMBS_DIR / dst_album
        dst_thumb_dir.mkdir(parents=True, exist_ok=True)
        dst_thumb = dst_thumb_dir / (Path(dst_path.name).stem + "_thumb.jpg")
        shutil.move(str(src_thumb), str(dst_thumb))
    # Transfer per-file source URL between .bunkrinfo files
    with bunkrinfo_lock:
        src_url = update_bunkrinfo_remove(DOWNLOADS_DIR / src_album, filename)
        if src_url is not None:
            update_bunkrinfo_add(DOWNLOADS_DIR / dst_album, dst_path.name, src_url)
    return jsonify({"ok": True, "new_filename": dst_path.name})


@app.route("/api/album/move-files", methods=["POST"])
def move_files_batch():
    """
    Task 11.1: Move multiple files between albums in a batch operation.
    Task 21.2: Enhanced with comprehensive error handling, disk space checks, and logging.
    
    Request JSON:
    {
        "moves": [
            {"album": "source_album", "filename": "file.jpg", "target_album": "dest_album"},
            ...
        ]
    }
    
    Response JSON:
    {
        "success": [{"album": "...", "filename": "...", "target_album": "...", "new_filename": "..."}],
        "failed": [{"album": "...", "filename": "...", "error": "...", "error_code": "..."}],
        "conflicts": [{"album": "...", "filename": "...", "existing_name": "...", "suggested_name": "..."}]
    }
    """
    data = request.get_json(silent=True) or {}
    moves = data.get("moves", [])
    
    # Task 21.2: Validate request structure
    if not isinstance(moves, list):
        return jsonify({"error": "moves must be an array", "error_code": "InvalidRequest"}), 400
    
    if not moves:
        return jsonify({"error": "No files specified", "error_code": "EmptyRequest"}), 400
    
    success = []
    failed = []
    conflicts = []
    
    # Task 22.3: Collect metadata changes per album for batched updates
    # Maps: album_name -> list of filenames to remove
    metadata_removes = {}
    # Maps: album_name -> {filename: url} to add
    metadata_adds = {}
    
    for move in moves:
        if not isinstance(move, dict):
            failed.append({
                "album": "",
                "filename": "",
                "error": "Invalid move entry",
                "error_code": "InvalidMoveEntry"
            })
            continue
        
        src_album = (move.get("album") or "").strip()
        filename = (move.get("filename") or "").strip()
        dst_album = (move.get("target_album") or "").strip()
        
        # Task 21.2: Validate required fields
        if not src_album or not filename or not dst_album:
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": "album, filename, target_album required",
                "error_code": "MissingRequiredFields"
            })
            continue
        
        # Task 21.4: Validate destination album path (security check)
        if not validate_album_path(dst_album):
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": "Target album name is invalid or contains path traversal attempt",
                "error_code": "InvalidAlbumPath"
            })
            continue
        
        # Task 21.2: Validate destination album exists
        dst_dir = DOWNLOADS_DIR / dst_album
        if not dst_dir.is_dir():
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": "Target album not found",
                "error_code": "AlbumNotFound"
            })
            continue
        
        # Task 21.2: Check source file exists
        src_path = DOWNLOADS_DIR / src_album / filename
        if not src_path.is_file():
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": "File not found",
                "error_code": "FileNotFound"
            })
            continue
        
        # Prevent moving to same album
        if src_album == dst_album:
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": "Cannot move file to the same album",
                "error_code": "SameAlbumMove"
            })
            continue
        
        # Generate unique filename if conflict exists
        dst_path = dst_dir / filename
        new_filename = filename
        if dst_path.exists():
            new_filename = get_unique_filename(dst_dir, filename)
            dst_path = dst_dir / new_filename
        
        # Perform the move operation
        try:
            # Task 21.2: Check disk space before moving
            try:
                file_size = src_path.stat().st_size
                free_space = get_free_disk_bytes()
                if free_space is not None and free_space < file_size:
                    raise DiskSpaceError(f"Insufficient disk space: {format_size(free_space)} available, {format_size(file_size)} required")
            except DiskSpaceError:
                raise
            except Exception as e:
                # Task 21.2: Log error but continue (disk space check is best-effort)
                print(f"  [Warning] Could not check disk space: {e}")
            
            # Task 21.2: Perform move with permission error handling
            # Task 22.3: Use shutil.move() for same-filesystem moves (already implemented)
            try:
                shutil.move(str(src_path), str(dst_path))
            except PermissionError as e:
                raise PermissionError(f"Permission denied: {e}")
            except OSError as e:
                if "permission" in str(e).lower():
                    raise PermissionError(f"Permission denied: {e}")
                raise
            
            # Move thumbnail if it exists
            src_thumb = THUMBS_DIR / src_album / (Path(filename).stem + "_thumb.jpg")
            if src_thumb.exists():
                dst_thumb_dir = THUMBS_DIR / dst_album
                dst_thumb_dir.mkdir(parents=True, exist_ok=True)
                dst_thumb = dst_thumb_dir / (Path(new_filename).stem + "_thumb.jpg")
                try:
                    shutil.move(str(src_thumb), str(dst_thumb))
                except Exception as e:
                    # Task 21.2: Log thumbnail move failure but don't fail the whole operation
                    print(f"  [Warning] Failed to move thumbnail for {filename}: {e}")
            
            # Also check for video thumbnail (without _thumb suffix)
            src_video_thumb = THUMBS_DIR / src_album / (Path(filename).stem + ".jpg")
            if src_video_thumb.exists():
                dst_thumb_dir = THUMBS_DIR / dst_album
                dst_thumb_dir.mkdir(parents=True, exist_ok=True)
                dst_video_thumb = dst_thumb_dir / (Path(new_filename).stem + ".jpg")
                try:
                    shutil.move(str(src_video_thumb), str(dst_video_thumb))
                except Exception as e:
                    # Task 21.2: Log thumbnail move failure but don't fail the whole operation
                    print(f"  [Warning] Failed to move video thumbnail for {filename}: {e}")
            
            # Task 22.3: Collect metadata changes for batched updates
            # Read the URL from source album metadata
            src_url = update_bunkrinfo_remove(DOWNLOADS_DIR / src_album, filename)
            
            # Track successful move for batched metadata update
            if src_url is not None:
                # Collect for batched add to destination
                if dst_album not in metadata_adds:
                    metadata_adds[dst_album] = {}
                metadata_adds[dst_album][new_filename] = src_url
            
            success.append({
                "album": src_album,
                "filename": filename,
                "target_album": dst_album,
                "new_filename": new_filename
            })
            
        except FileOperationError as e:
            # Task 21.2: Consistent error response format for file operation errors
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": str(e),
                "error_code": type(e).__name__
            })
            # Task 21.2: Log error for monitoring
            print(f"  [Error] Move failed for {src_album}/{filename}: {e}")
        except Exception as e:
            # Task 21.2: Catch-all for unexpected errors
            failed.append({
                "album": src_album,
                "filename": filename,
                "error": str(e),
                "error_code": "UnexpectedError"
            })
            # Task 21.2: Log error for monitoring
            print(f"  [Error] Unexpected error moving {src_album}/{filename}: {e}")
    
    # Task 22.3: Apply batched metadata updates (single write per album)
    for album, file_url_map in metadata_adds.items():
        update_bunkrinfo_batch_add(DOWNLOADS_DIR / album, file_url_map)
    
    return jsonify({
        "success": success,
        "failed": failed,
        "conflicts": conflicts
    })


def get_unique_filename(target_dir, filename):
    """
    Generate a unique filename if a conflict exists in the target directory.
    Appends suffixes like " (1)", " (2)", etc. while preserving the extension.
    
    Args:
        target_dir: Path object for the target directory
        filename: Original filename string
    
    Returns:
        Unique filename string
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    
    counter = 1
    new_filename = filename
    
    while (target_dir / new_filename).exists():
        new_filename = f"{stem} ({counter}){suffix}"
        counter += 1
    
    return new_filename


def validate_album_path(album_name):
    """
    Task 21.4: Validate that the album name is safe and doesn't contain path traversal attempts.
    
    This function prevents security vulnerabilities by:
    - Rejecting path traversal sequences ("..", "/", "\")
    - Blocking null bytes and control characters
    - Ensuring resolved paths stay within DOWNLOADS_DIR
    
    Args:
        album_name: Album name string to validate
    
    Returns:
        True if valid, False otherwise
    """
    if not album_name:
        return False
    
    # Task 21.4: Check for path traversal attempts
    if ".." in album_name or "/" in album_name or "\\" in album_name:
        print(f"  [Security] Rejected album name with path traversal attempt: {album_name}")
        return False
    
    # Task 21.4: Check for null bytes or control characters
    if any(ord(c) < 32 for c in album_name):
        print(f"  [Security] Rejected album name with control characters: {repr(album_name)}")
        return False
    
    # Task 21.4: Ensure resolved path stays within DOWNLOADS_DIR
    try:
        resolved = (DOWNLOADS_DIR / album_name).resolve()
        is_valid = resolved.parent == DOWNLOADS_DIR.resolve()
        if not is_valid:
            print(f"  [Security] Rejected album name that resolves outside DOWNLOADS_DIR: {album_name}")
        return is_valid
    except Exception as e:
        print(f"  [Security] Error validating album path '{album_name}': {e}")
        return False


@app.route("/api/album/delete-file", methods=["POST"])
def delete_single_file():
    """Delete a single file from an album."""
    data = request.get_json(silent=True) or {}
    album = sanitize(data.get("album", ""))
    filename = sanitize(data.get("filename", ""))
    
    if not album or not filename:
        return jsonify({"error": "Missing album or filename"}), 400
    
    # Validate album path to prevent directory traversal
    try:
        validate_album_path(album)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    # Additional validation for filename to prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    
    file_path = DOWNLOADS_DIR / album / filename
    thumb_path = THUMBS_DIR / album / (Path(filename).stem + "_thumb.jpg")
    video_thumb_path = THUMBS_DIR / album / (Path(filename).stem + ".jpg")
    
    try:
        # Delete main file first
        if file_path.exists():
            file_path.unlink()
        else:
            return jsonify({"error": "File not found"}), 404
        
        # Only delete thumbnails if main file deletion succeeded
        if thumb_path.exists():
            thumb_path.unlink()
        if video_thumb_path.exists():
            video_thumb_path.unlink()
        
        # Update metadata
        update_bunkrinfo_remove(DOWNLOADS_DIR / album, filename)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/album/delete-files", methods=["POST"])
def delete_files_batch():
    """
    Task 17.3: Delete multiple files from albums in a batch operation.
    Task 21.2: Enhanced with comprehensive error handling and logging.
    
    Request JSON:
    {
        "files": [
            {"album": "album_name", "filename": "file.jpg"},
            ...
        ]
    }
    
    Response JSON:
    {
        "success": [{"album": "...", "filename": "..."}],
        "failed": [{"album": "...", "filename": "...", "error": "...", "error_code": "..."}]
    }
    """
    data = request.get_json(silent=True) or {}
    files = data.get("files", [])
    
    # Task 21.2: Validate request structure
    if not isinstance(files, list):
        return jsonify({"error": "files must be an array", "error_code": "InvalidRequest"}), 400
    
    if not files:
        return jsonify({"error": "No files specified", "error_code": "EmptyRequest"}), 400
    
    success = []
    failed = []
    
    # Task 22.3: Collect metadata changes per album for batched updates
    # Maps: album_name -> list of filenames to remove
    metadata_removes = {}
    
    for file_entry in files:
        if not isinstance(file_entry, dict):
            failed.append({
                "album": "",
                "filename": "",
                "error": "Invalid file entry",
                "error_code": "InvalidFileEntry"
            })
            continue
        
        album = (file_entry.get("album") or "").strip()
        filename = (file_entry.get("filename") or "").strip()
        
        # Task 21.2: Validate required fields
        if not album or not filename:
            failed.append({
                "album": album,
                "filename": filename,
                "error": "album and filename required",
                "error_code": "MissingRequiredFields"
            })
            continue
        
        # Task 21.2: Check if file exists
        file_path = DOWNLOADS_DIR / album / filename
        if not file_path.is_file():
            failed.append({
                "album": album,
                "filename": filename,
                "error": "File not found",
                "error_code": "FileNotFound"
            })
            continue
        
        # Perform the delete operation
        try:
            # Task 21.2: Permission error handling
            try:
                # Delete the main file
                if file_path.exists():
                    file_path.unlink()
            except PermissionError as e:
                raise PermissionError(f"Permission denied: {e}")
            except OSError as e:
                if "permission" in str(e).lower():
                    raise PermissionError(f"Permission denied: {e}")
                raise
            
            # Delete image thumbnail (with _thumb suffix)
            thumb_path = THUMBS_DIR / album / (Path(filename).stem + "_thumb.jpg")
            if thumb_path.exists():
                try:
                    thumb_path.unlink()
                except Exception as e:
                    # Task 21.2: Log thumbnail deletion failure but don't fail the whole operation
                    print(f"  [Warning] Failed to delete thumbnail for {filename}: {e}")
            
            # Delete video thumbnail (without _thumb suffix)
            video_thumb_path = THUMBS_DIR / album / (Path(filename).stem + ".jpg")
            if video_thumb_path.exists():
                try:
                    video_thumb_path.unlink()
                except Exception as e:
                    # Task 21.2: Log thumbnail deletion failure but don't fail the whole operation
                    print(f"  [Warning] Failed to delete video thumbnail for {filename}: {e}")
            
            # Task 22.3: Collect metadata changes for batched updates
            if album not in metadata_removes:
                metadata_removes[album] = []
            metadata_removes[album].append(filename)
            
            success.append({
                "album": album,
                "filename": filename
            })
            
        except FileOperationError as e:
            # Task 21.2: Consistent error response format for file operation errors
            failed.append({
                "album": album,
                "filename": filename,
                "error": str(e),
                "error_code": type(e).__name__
            })
            # Task 21.2: Log error for monitoring
            print(f"  [Error] Delete failed for {album}/{filename}: {e}")
        except Exception as e:
            # Task 21.2: Catch-all for unexpected errors
            failed.append({
                "album": album,
                "filename": filename,
                "error": str(e),
                "error_code": "UnexpectedError"
            })
            # Task 21.2: Log error for monitoring
            print(f"  [Error] Unexpected error deleting {album}/{filename}: {e}")
    
    # Task 22.3: Apply batched metadata updates (single write per album)
    with bunkrinfo_lock:
        for album, filenames in metadata_removes.items():
            update_bunkrinfo_batch_remove(DOWNLOADS_DIR / album, filenames)
    
    return jsonify({
        "success": success,
        "failed": failed
    })


@app.route("/api/copy", methods=["POST"])
def copy_file():
    """
    Copy a file from one album to another.
    
    Request JSON:
    {
        "album": "source_album",
        "filename": "file.jpg",
        "target_album": "destination_album"
    }
    
    Response JSON:
    {
        "ok": true,
        "new_filename": "file.jpg"  // May be renamed if conflict
    }
    """
    data = request.get_json(silent=True) or {}
    src_album = (data.get("album") or "").strip()
    filename = (data.get("filename") or "").strip()
    dst_album = (data.get("target_album") or "").strip()
    
    if not src_album or not filename or not dst_album:
        return jsonify({"error": "album, filename, target_album required"}), 400
    
    src_path = DOWNLOADS_DIR / src_album / filename
    dst_dir = DOWNLOADS_DIR / dst_album
    
    if not src_path.is_file():
        return jsonify({"error": "File not found"}), 404
    
    if not dst_dir.is_dir():
        return jsonify({"error": "Target album not found"}), 404
    
    # Handle filename conflicts
    dst_path = dst_dir / filename
    if dst_path.exists():
        stem, sfx = Path(filename).stem, Path(filename).suffix
        dst_path = dst_dir / f"{stem}_{uuid.uuid4().hex[:6]}{sfx}"
    
    # Copy the main file
    try:
        shutil.copy2(str(src_path), str(dst_path))
    except Exception as e:
        return jsonify({"error": f"Failed to copy file: {e}"}), 500
    
    # Copy thumbnail if it exists
    src_thumb = THUMBS_DIR / src_album / (Path(filename).stem + "_thumb.jpg")
    if src_thumb.exists():
        dst_thumb_dir = THUMBS_DIR / dst_album
        dst_thumb_dir.mkdir(parents=True, exist_ok=True)
        dst_thumb = dst_thumb_dir / (Path(dst_path.name).stem + "_thumb.jpg")
        try:
            shutil.copy2(str(src_thumb), str(dst_thumb))
        except Exception as e:
            print(f"  [Warning] Failed to copy thumbnail: {e}")
    
    # Copy video thumbnail if it exists
    src_video_thumb = THUMBS_DIR / src_album / (Path(filename).stem + ".jpg")
    if src_video_thumb.exists():
        dst_thumb_dir = THUMBS_DIR / dst_album
        dst_thumb_dir.mkdir(parents=True, exist_ok=True)
        dst_video_thumb = dst_thumb_dir / (Path(dst_path.name).stem + ".jpg")
        try:
            shutil.copy2(str(src_video_thumb), str(dst_video_thumb))
        except Exception as e:
            print(f"  [Warning] Failed to copy video thumbnail: {e}")
    
    # Copy source URL metadata
    with bunkrinfo_lock:
        src_info = _bunkrinfo_read(DOWNLOADS_DIR / src_album)
        src_url = src_info.get("files", {}).get(filename)
        if src_url:
            update_bunkrinfo_add(DOWNLOADS_DIR / dst_album, dst_path.name, src_url)
    
    return jsonify({"ok": True, "new_filename": dst_path.name})


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """
    Open an album folder in the system file explorer.
    
    Request JSON:
    {
        "album": "album_name"
    }
    
    Response JSON:
    {
        "ok": true
    }
    """
    data = request.get_json(silent=True) or {}
    album = (data.get("album") or "").strip()
    
    if not album:
        return jsonify({"error": "album required"}), 400
    
    album_path = DOWNLOADS_DIR / album
    
    if not album_path.is_dir():
        return jsonify({"error": "Album not found"}), 404
    
    # Open folder in system file explorer
    try:
        import platform
        system = platform.system()
        
        if system == "Windows":
            # Windows: use explorer
            subprocess.run(["explorer", str(album_path.resolve())], check=False)
        elif system == "Darwin":
            # macOS: use open
            subprocess.run(["open", str(album_path.resolve())], check=False)
        else:
            # Linux: try xdg-open
            subprocess.run(["xdg-open", str(album_path.resolve())], check=False)
        
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Failed to open folder: {e}"}), 500


@app.route("/api/thumbnails/generate", methods=["POST"])
def generate_thumbnails():
    """Generate thumbnails for all media files in the gallery."""
    data = request.get_json(silent=True) or {}
    album_filter = data.get("album")  # Optional: generate for specific album only
    
    generated = 0
    skipped = 0
    errors = 0
    
    for album_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not album_dir.is_dir():
            continue
        if album_filter and album_dir.name != album_filter:
            continue
            
        for f in sorted(album_dir.iterdir()):
            if f.name.startswith("."):
                continue
            ext = f.suffix.lower()
            
            if ext in IMAGE_EXTS:
                result = generate_image_thumbnail(f, album_dir.name)
                if result:
                    generated += 1
                elif result is None:
                    skipped += 1
                else:
                    errors += 1
            elif ext in VIDEO_EXTS:
                result = generate_video_thumbnail(f, album_dir.name)
                if result:
                    generated += 1
                elif result is None:
                    skipped += 1
                else:
                    errors += 1
    
    return jsonify({"ok": True, "generated": generated, "skipped": skipped, "errors": errors})


@app.route("/api/history")
def get_history():
    """Get download history."""
    if not HISTORY_FILE.exists():
        return jsonify([])
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return jsonify(history)
    except Exception:
        return jsonify([])


@app.route("/api/history/add", methods=["POST"])
def add_history():
    """Add a completed download to history."""
    data = request.get_json(silent=True) or {}
    album_name = data.get("album_name")
    album_url = data.get("album_url")
    file_count = data.get("file_count", 0)
    
    if not album_name:
        return jsonify({"error": "album_name required"}), 400
    
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []
    
    # Add new entry
    entry = {
        "album_name": album_name,
        "album_url": album_url,
        "file_count": file_count,
        "timestamp": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # Avoid duplicates (same album_url)
    if album_url:
        history = [h for h in history if h.get("album_url") != album_url]
    
    history.insert(0, entry)
    
    # Keep only last 100 entries
    history = history[:100]
    
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    return jsonify({"ok": True})


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    """Clear download history."""
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/disk")
def disk_info():
    try:
        usage = shutil.disk_usage(DOWNLOADS_DIR)
        return jsonify({"free": usage.free, "total": usage.total, "used": usage.used})
    except Exception:
        return jsonify({"error": "unavailable"})


@app.route("/api/config")
def get_config():
    return jsonify({
        "version": VERSION,
        "ffmpeg_ok": FFMPEG_OK, "playwright_ok": PLAYWRIGHT_OK, "sevenzip_ok": SEVENZIP_OK,
        "default_concurrency_images": DEFAULT_CONCURRENCY_IMAGES,
        "default_concurrency_videos": DEFAULT_CONCURRENCY_VIDEOS,
        "default_max_retries": DEFAULT_MAX_RETRIES,
        "pool_size": browser_pool.status["target"], "pool_max": POOL_MAX,
    })

@app.route("/api/pool/size", methods=["GET", "POST"])
def api_pool_size():
    if request.method == "GET":
        return jsonify(browser_pool.status)
    data     = request.get_json() or {}
    new_size = max(1, min(POOL_MAX, int(data.get("size", POOL_SIZE))))
    browser_pool.resize(new_size)
    return jsonify({"size": new_size})


@app.route("/files/<path:full_path>")
def serve_file(full_path):
    album, _, filename = full_path.rpartition("/")
    if not album or not filename:
        return jsonify({"error": "Invalid path"}), 400
    return send_from_directory(DOWNLOADS_DIR / album, filename)


@app.route("/thumbs/<path:full_path>")
def serve_thumb(full_path):
    album, _, filename = full_path.rpartition("/")
    if not album or not filename:
        return jsonify({"error": "Invalid path"}), 400
    return send_from_directory(THUMBS_DIR / album, filename)


@app.route("/help")
def serve_help():
    """Serve the help documentation as HTML."""
    try:
        help_path = Path(__file__).parent / "HELP.md"
        if not help_path.exists():
            return "Help file not found", 404
        
        help_text = help_path.read_text(encoding="utf-8")
        
        # Simple markdown-to-HTML conversion (basic)
        html_content = help_text
        html_content = html_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # Headers
        import re
        html_content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html_content, flags=re.MULTILINE)
        html_content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html_content, flags=re.MULTILINE)
        html_content = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html_content, flags=re.MULTILINE)
        
        # Bold
        html_content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_content)
        
        # Code blocks
        html_content = re.sub(r'```(\w+)?\n(.*?)\n```', r'<pre><code>\2</code></pre>', html_content, flags=re.DOTALL)
        html_content = re.sub(r'`([^`]+)`', r'<code>\1</code>', html_content)
        
        # Lists
        html_content = re.sub(r'^\- (.+)$', r'<li>\1</li>', html_content, flags=re.MULTILINE)
        html_content = re.sub(r'(<li>.*</li>)', r'<ul>\1</ul>', html_content, flags=re.DOTALL)
        
        # Paragraphs
        html_content = re.sub(r'\n\n', '</p><p>', html_content)
        html_content = '<p>' + html_content + '</p>'
        
        return f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>BunkrWrap Help</title>
            <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap" rel="stylesheet">
            <style>
                :root {{
                    --bg: #0d0d0f;
                    --surface: #141418;
                    --panel: #1a1a20;
                    --border: #2a2a35;
                    --accent: #00e5ff;
                    --text: #e2e8f0;
                    --muted: #64748b;
                    --mono: 'Share Tech Mono', monospace;
                    --sans: 'Barlow', sans-serif;
                }}
                body {{ 
                    font-family: var(--sans); 
                    background: var(--bg); 
                    color: var(--text); 
                    padding: 20px 40px;
                    line-height: 1.7;
                    max-width: 1000px;
                    margin: 0 auto;
                }}
                h1, h2, h3 {{ 
                    color: var(--accent); 
                    font-family: var(--mono);
                    letter-spacing: 1px;
                    margin-top: 2em;
                    margin-bottom: 0.5em;
                }}
                h1 {{ font-size: 1.8rem; border-bottom: 2px solid var(--border); padding-bottom: 0.5em; }}
                h2 {{ font-size: 1.4rem; }}
                h3 {{ font-size: 1.1rem; color: var(--muted); }}
                code {{ 
                    background: var(--panel); 
                    padding: 2px 6px; 
                    border-radius: 3px;
                    font-family: var(--mono);
                    font-size: 0.9em;
                    color: var(--accent);
                }}
                pre {{ 
                    background: var(--panel); 
                    padding: 16px; 
                    border-radius: 4px;
                    overflow-x: auto;
                    border: 1px solid var(--border);
                }}
                pre code {{
                    background: none;
                    padding: 0;
                    color: var(--text);
                }}
                ul {{ 
                    margin-left: 20px;
                    line-height: 1.8;
                }}
                li {{ margin-bottom: 0.5em; }}
                strong {{ color: var(--accent); font-weight: 600; }}
                a {{ color: var(--accent); text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                hr {{ border: none; border-top: 1px solid var(--border); margin: 2em 0; }}
            </style>
        </head>
        <body>{html_content}</body>
        </html>
        """
    except Exception as e:
        return f"Error loading help: {e}", 500


@app.route("/")
def index():
    with open(Path(__file__).parent / "index.html", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    print("\n  ╔══════════════════════════════════════════╗")
    print(f"  ║   BunkrWrap  Web UI  v{VERSION:<18}║")
    print("  ║   http://localhost:5000                  ║")
    print(f"  ║   Playwright : {'✓ ready' if PLAYWRIGHT_OK else '✗ not installed'}                   ║")
    print(f"  ║   ffmpeg     : {'✓ ready' if FFMPEG_OK else '✗ not found (no video thumbs)'}         ║")
    print(f"  ║   7-Zip      : {'✓ ready' if SEVENZIP_OK else '✗ not found (no rar/7z extract)'}        ║")
    print("  ╚══════════════════════════════════════════╝\n")
    if PLAYWRIGHT_OK:
        threading.Thread(
            target=browser_pool.start, args=(POOL_SIZE,),
            daemon=True, name="BrowserPool"
        ).start()
    app.run(debug=False, port=5000, threaded=True)
