"""Narrow, bounded read-only access to the active profile's Hermes gateway logs."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

LOG_FILES = {"gateway": "gateway.log", "errors": "errors.log"}
DEFAULT_LOG_LINES = 50
MAX_LOG_LINES = 100
MAX_TAIL_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 16_000
MAX_LINE_CHARS = 1_200


def read_gateway_logs(profile_home: str | Path, *, log: str = "gateway", limit: int = DEFAULT_LOG_LINES) -> dict[str, Any]:
    """Return only the newest bounded lines from one allowlisted log in *profile_home*."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LOG_LINES:
        raise ValueError(f"limit must be between 1 and {MAX_LOG_LINES}")
    filename = LOG_FILES.get(str(log or "").strip().lower())
    if filename is None:
        raise ValueError("log must be 'gateway' or 'errors'")

    home = Path(profile_home).expanduser().resolve()
    logs_dir = home / "logs"
    if logs_dir.is_symlink() or not logs_dir.is_dir():
        raise PermissionError("Gateway logs are unavailable in the active profile")
    path = logs_dir / filename
    if path.is_symlink():
        raise PermissionError("Gateway log access is restricted to regular files in the active profile")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {"file": filename, "line_count": 0, "truncated": False, "text": ""}

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("Gateway log access is restricted to regular files in the active profile")
        start = max(0, info.st_size - MAX_TAIL_BYTES)
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            stream.seek(start)
            raw = stream.read(MAX_TAIL_BYTES)
    finally:
        if fd >= 0:
            os.close(fd)

    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    truncated = start > 0
    if start:
        _, separator, text = text.partition("\n")
        text = text if separator else ""
    lines = text.splitlines()
    if len(lines) > limit:
        lines = lines[-limit:]
        truncated = True

    # This is a safety boundary: do not honor the user's redact_secrets opt-out.
    from agent.redact import redact_sensitive_text

    redacted_lines = redact_sensitive_text(
        "\n".join(lines), force=True, file_read=True, redact_url_credentials=True,
    ).splitlines()
    selected: list[str] = []
    output_chars = 0
    for line in reversed(redacted_lines):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "… [line truncated]"
            truncated = True
        addition = len(line) + (1 if selected else 0)
        if output_chars + addition > MAX_OUTPUT_CHARS:
            truncated = True
            break
        selected.append(line)
        output_chars += addition
    selected.reverse()
    return {
        "file": filename,
        "line_count": len(selected),
        "truncated": truncated,
        "text": "\n".join(selected),
    }
