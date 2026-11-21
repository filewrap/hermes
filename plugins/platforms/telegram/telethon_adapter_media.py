"""Local video geometry probing and thumbnail extraction for Telegram uploads.

Split out of ``plugins.platforms.telegram.telethon_adapter`` (file-size ratchet); the names stay
importable from the facade so existing imports keep working.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("plugins.platforms.telegram.telethon_adapter")


def _coerce_duration_seconds(value: Any) -> Optional[int]:
    """Round a raw length to whole positive seconds, or None if unusable.

    Ported from upstream v2026.9.24 (plugins/platforms/telegram/adapter.py).
    """
    try:
        secs = round(float(value))
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _probe_video_geometry(path: str) -> dict[str, int]:
    """``{"width", "height", "duration"}`` for a local video; ``{}`` when ffprobe can't read it.

    Ported from upstream v2026.9.24 (plugins/platforms/telegram/adapter.py).
    Telegram runs its own video processing only for uploads under roughly 10 MB; above that it
    stores the file as an unprocessed ``320x320`` video with ``duration=0``, so the message must
    carry the real geometry or clients draw a square tile for any aspect ratio.
    """
    try:
        import shutil
        import subprocess
        if not shutil.which("ffprobe"):
            return {}
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        blob = json.loads(proc.stdout or "{}")
        streams = blob.get("streams") or []
        if not streams:
            return {}
        geometry = {"width": int(streams[0]["width"]), "height": int(streams[0]["height"])}
        duration = _coerce_duration_seconds((blob.get("format") or {}).get("duration"))
        if duration:
            geometry["duration"] = duration
        return geometry
    except Exception:
        logger.debug("[Telegram] video geometry probe failed for %s", path, exc_info=True)
        return {}


def _video_thumbnail_jpeg(path: str, duration: Optional[int]) -> Optional[str]:
    """Write a 320px-wide JPEG frame for Telegram's ``thumbnail`` field; None on failure.

    Ported from upstream v2026.9.24 (plugins/platforms/telegram/adapter.py).
    Telegram keeps a supplied thumbnail for the uploads it did not process itself — without one the
    chat shows a square placeholder tile until the video is opened.
    """
    out = None
    try:
        import contextlib
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("ffmpeg"):
            return None
        seek = max(1, int((duration or 3) * 0.25))
        fd, out = tempfile.mkstemp(suffix=".jpg", prefix="hermes-tg-thumb-")
        os.close(fd)
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(seek),
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-q:v",
                "6",
                out,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0 or not os.path.getsize(out):
            with contextlib.suppress(OSError):
                os.remove(out)
            return None
        return out
    except Exception:
        logger.debug("[Telegram] video thumbnail extraction failed for %s", path, exc_info=True)
        if out:
            with contextlib.suppress(OSError):
                os.remove(out)
        return None
