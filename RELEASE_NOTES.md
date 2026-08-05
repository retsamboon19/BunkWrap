# BunkrWrap v5.1.0

## Download

Download `BunkrWrap-v5.1.0-Windows.zip` from the Assets section below, extract it, and double-click `Install BunkrWrap.bat`.

## What changed

- Removed the redundant size request before every fresh file transfer.
- Reused pooled HTTP connections and carried album cookies into worker sessions.
- Added an album-wide circuit breaker for an unavailable resolver API.
- Increased streaming chunks to reduce overhead on large video files.
- Kept broken-stream retries local to the affected file.
- Moved image and video thumbnail generation off download workers.
- Preserved partial files and unfinished queue items across browser and app restarts.
- Fixed the activity log jumping back to the top while the user is scrolling.

Existing downloads, thumbnails, history, and persistent queue data are retained when updating.
