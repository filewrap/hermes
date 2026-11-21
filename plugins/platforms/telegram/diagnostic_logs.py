"""Read only Hermes-managed logs; never export a home directory or raw secrets."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import time
import zipfile

_LOG_NAME = re.compile(r"(?:agent|errors|gateway|gui)\.log(?:\.[0-9]+)?\Z")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_LOG_FILES = 256


def _open_directory(home: Path) -> int:
    # Directory-relative O_NOFOLLOW opens also close the symlink-swap race.
    return os.open(Path(home) / "logs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _read_at(directory: int, name: str, limit: int, *, tail: bool = False) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Log is not a regular file")
        if tail and info.st_size > limit:
            # Read the whole bounded file before redaction, even for a tail: cutting
            # through a multiline private key before scrubbing could expose its body.
            raise ValueError("Log exceeds safe preview size; rotate it before export")
        if info.st_size > limit:
            raise ValueError("Log exceeds safe export size; rotate it before export")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Log grew beyond safe export size")
    return _scrub(raw.decode("utf-8", errors="replace"))


def _scrub(text: str) -> str:
    from agent.redact import redact_for_egress, redact_terminal_output
    try:
        return redact_for_egress(redact_terminal_output(text, command="env", force=True))
    except Exception:
        # Never fall back to the unredacted file if a scrubber fails.
        raise ValueError("Log redaction unavailable; no log content exported") from None


def _names(directory: int) -> list[str]:
    names = []
    deadline = time.monotonic() + 2.0
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DIRECTORY_ENTRIES or time.monotonic() > deadline:
                raise ValueError("Log directory exceeds safe scan limits; archive old rotations locally")
            if not _LOG_NAME.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            names.append(entry.name)
            if len(names) > MAX_LOG_FILES:
                raise ValueError("Too many retained log files for a safe export; archive old rotations locally")
    return sorted(names)


def recent_logs(home: Path) -> str:
    """Latest lines of the three operational logs, scrubbed before truncation."""
    try:
        directory = _open_directory(home)
    except FileNotFoundError:
        return "No Hermes log directory exists in this profile yet."
    parts = []
    try:
        names = set(_names(directory))
        for name in ("gateway.log", "errors.log", "agent.log"):
            if name not in names:
                continue
            text = _read_at(directory, name, MAX_FILE_BYTES, tail=True)
            excerpt = "\n".join(text.splitlines()[-25:])[-5000:]
            # Four-backtick fences keep ordinary log triple-backticks inside code.
            excerpt = excerpt.replace("````", "` ` ` `")
            parts.append(f"## {name}\n\n````text\n{excerpt or '(empty)'}\n````")
    finally:
        os.close(directory)
    try:
        from .forum_audit import status_for_profile
        status = status_for_profile(home)
        status_text = (
            f"Enabled: {status.get('enabled', False)}\n"
            f"Pending: {status.get('pending', 0)}\n"
            f"Per-user topics: {status.get('mapped_users', 0)}\n"
            f"Status: {status.get('message', 'verified configuration active' if status.get('enabled') else 'not configured')}"
        )
    except Exception:
        status_text = "Status unavailable; check the local gateway log."
    parts.append(f"## Telegram forum audit status\n\n````text\n{status_text}\n````")
    return "\n\n".join(parts)


def export_logs(home: Path, destination: Path) -> int:
    """ZIP all retained managed logs, including rotations. Fail rather than truncate."""
    directory = _open_directory(home)
    total = 0
    count = 0
    try:
        names = _names(directory)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                text = _read_at(directory, name, MAX_FILE_BYTES)
                encoded = text.encode("utf-8")
                total += len(encoded)
                if total > MAX_EXPORT_BYTES:
                    raise ValueError("Retained logs exceed the 128 MiB safe export limit; rotate/archive locally")
                archive.writestr(name, encoded)
                count += 1
            archive.writestr("README.txt", (
                "All retained Hermes-managed agent/errors/gateway/gui logs and numeric rotations "
                "available in the requesting profile at export time. Secrets are redacted. "
                "No .env, configuration, databases, authentication sessions, or arbitrary files are included. "
                "Deleted rotations and supervisor stdout not written to these logs are unavailable. "
                "Exports are limited to 256 files, 32 MiB per file and 128 MiB combined; oversized exports fail rather than silently omit data. "
                "Files are read sequentially, not as an atomic cross-file snapshot.\n"
            ))
    finally:
        os.close(directory)
    return count
