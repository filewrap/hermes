#!/usr/bin/env python3
"""Model-facing ``transcripts`` tool over the existing transcription engine.

Thin wrapper only — no engine work. Two transcript sources already exist in
Hermes and neither has a core model-facing tool:

- the STT engine (``tools/transcription_tools.py::transcribe_audio``) turns an
  audio file into text; the gateway calls it for inbound voice messages, but
  the model itself cannot transcribe a file;
- meeting captures (``plugins/google_meet/process_manager.py``) persist as
  ``<HERMES_HOME>/workspace/meetings/<meeting-id>/transcript.txt`` after the
  bot leaves; the plugin's ``meet_transcript`` reads only the *active* meeting,
  so past captures are unreachable from the model.

Actions: ``list`` (stored captures, newest first), ``show`` (read one back),
``transcribe`` (audio file -> text via the configured STT provider),
``status`` (STT provider resolution + active capture), ``import`` (store
transcript text as a capture so ``list``/``show`` can read it back). No new
store: a capture is a directory under ``workspace/meetings/`` containing a
``transcript.txt``, the exact shape the Meet bot writes.
"""

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Mirror the external agent's TRANSCRIPTS_SHOW_MAX_CHARS: a ``show`` result carries at
# most this much transcript text, with a truncation marker naming the full read.
SHOW_MAX_CHARS = 12_000
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50


def _get_hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _meetings_root() -> Path:
    """Capture directory. The Meet plugin owns the layout; read it through its
    own ``_root()`` when importable so a layout change lands in one place."""
    try:
        from plugins.google_meet import process_manager as _pm

        return Path(_pm._root())
    except Exception:  # health: allow BLE001 -- plugin import/layout failure falls back to the default meetings dir
        return _get_hermes_home() / "workspace" / "meetings"


def _read_active_meeting() -> Optional[dict[str, Any]]:
    """The ``.active.json`` record, or None when no meeting is active / the
    plugin cannot be read. Never raises."""
    try:
        from plugins.google_meet import process_manager as _pm

        return _pm._read_active()
    except Exception:  # health: allow BLE001 -- unreadable active-meeting record means "none active", never fails the call
        return None


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra}, ensure_ascii=False)


def _ok(**fields: Any) -> str:
    return json.dumps({"success": True, **fields}, ensure_ascii=False)


def _slug(value: str) -> str:
    """``meeting_id`` suffix for an imported capture: lowercase slug, or ""."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _read_transcript_file(path: Path) -> list[str]:
    """Transcript lines (empty lines dropped, like the Meet reader)."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def _capture_entry(
    capture_dir: Path, active_id: Optional[str]
) -> Optional[dict[str, Any]]:
    """One list row for a capture dir, or None when it holds no transcript."""
    transcript_path = capture_dir / "transcript.txt"
    if not transcript_path.is_file():
        return None
    try:
        lines = _read_transcript_file(transcript_path)
        chars = sum(len(line) + 1 for line in lines)
        return {
            "meeting_id": capture_dir.name,
            "lines": len(lines),
            "chars": chars,
            "modified": transcript_path.stat().st_mtime,
            "active": capture_dir.name == active_id,
            "path": str(transcript_path),
        }
    except OSError as exc:
        logger.warning("transcripts list: unreadable capture %s: %s", capture_dir, exc)
        return None


def _list_captures(limit: int) -> str:
    """Stored captures, newest first. Empty list (not an error) when none exist."""
    root = _meetings_root()
    active = _read_active_meeting() or {}
    active_id = active.get("meeting_id")
    entries: list[dict[str, Any]] = []
    if root.is_dir():
        for capture_dir in root.iterdir():
            if not capture_dir.is_dir() or capture_dir.name.startswith("."):
                continue
            entry = _capture_entry(capture_dir, active_id)
            if entry is not None:
                entries.append(entry)
    entries.sort(key=lambda e: e["modified"], reverse=True)
    entries = entries[: max(1, min(LIST_MAX_LIMIT, limit))]
    for entry in entries:
        entry["modified"] = _format_mtime(entry["modified"])
    return _ok(action="list", captures=entries, count=len(entries))


def _format_mtime(epoch: float) -> str:
    from datetime import datetime, UTC

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _resolve_capture(meeting_id: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    """``(transcript_path, error)`` for *meeting_id*, or the active capture
    when no id is given."""
    capture_id = (meeting_id or "").strip()
    if not capture_id:
        active = _read_active_meeting() or {}
        capture_id = str(active.get("meeting_id") or "").strip()
        if not capture_id:
            return None, (
                "no meeting_id given and no meeting is active — "
                "call transcripts with action='list' to see stored captures"
            )
    path = _meetings_root() / capture_id / "transcript.txt"
    if not path.is_file():
        return None, f"no stored transcript for meeting_id {capture_id!r}"
    return path, None


def _show_capture(meeting_id: Optional[str], last: Optional[int]) -> str:
    """Read one capture back. Text is capped at SHOW_MAX_CHARS with a marker;
    ``last`` returns only the final N lines."""
    path, error = _resolve_capture(meeting_id)
    if error is not None or path is None:
        return _err(
            error or "capture could not be resolved", meeting_id=meeting_id or ""
        )
    lines = _read_transcript_file(path)
    total_lines = len(lines)
    if last is not None and last >= 1:
        lines = lines[-last:]
    text = "\n".join(lines)
    total_chars = len(text)
    truncated = False
    if total_chars > SHOW_MAX_CHARS:
        marker = (
            f"\n[truncated to {SHOW_MAX_CHARS} chars of {total_chars}; "
            f"call show with last=N to read the tail]"
        )
        text = text[: SHOW_MAX_CHARS - len(marker)] + marker
        truncated = True
    return _ok(
        action="show",
        meeting_id=path.parent.name,
        path=str(path),
        lines=lines,
        text=text,
        total_lines=total_lines,
        total_chars=total_chars,
        truncated=truncated,
    )


def _resolve_audio_path(raw: str) -> Path:
    """Audio path as given when absolute; a relative path is resolved against
    the profile workspace first, then left as given for the engine to reject."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    workspace_path = _get_hermes_home() / "workspace" / path
    return workspace_path if workspace_path.exists() else path


def _transcribe_file(file: Optional[str], model: Optional[str]) -> str:
    """Transcribe an audio file with the existing STT engine. The engine owns
    validation, provider resolution and its own error envelope."""
    if not (file or "").strip():
        return _err("file is required for action='transcribe'")
    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio(
        str(_resolve_audio_path(file.strip())),
        model=model or None,
        source="transcripts_tool",
    )
    if not isinstance(result, dict):
        return _err("transcription engine returned an unexpected result")
    return json.dumps(
        {
            "success": bool(result.get("success")),
            "action": "transcribe",
            "file": file.strip(),
            "transcript": result.get("transcript", ""),
            "provider": result.get("provider"),
            "error": result.get("error"),
        },
        ensure_ascii=False,
    )


def _status() -> str:
    """STT provider resolution plus the active capture, if any. Every probe is
    guarded: a broken config reports its reason instead of failing the call."""
    from tools import transcription_tools as _tt

    try:
        stt_config = _tt._load_stt_config()
    except Exception as exc:  # health: allow BLE001 -- a broken STT config reports its reason instead of failing the call
        return _ok(
            action="status",
            enabled=False,
            provider="none",
            provider_error=str(exc),
            active_capture=None,
        )
    try:
        enabled = _tt.is_stt_enabled(stt_config)
    except Exception:  # health: allow BLE001 -- an unreadable enabled flag means disabled, never fails the call
        enabled = False
    provider = "none"
    provider_error = None
    if enabled:
        try:
            provider = _tt._get_provider(stt_config) or "none"
        except Exception as exc:  # health: allow BLE001 -- provider resolution failure reports its reason instead of failing the call
            provider_error = str(exc)
    try:
        local_backend = _tt._detect_local_backend()
    except Exception:  # health: allow BLE001 -- backend detection failure means unknown backend, never fails the call
        local_backend = None
    active = _read_active_meeting()
    active_capture = None
    if active:
        active_capture = {
            "meeting_id": active.get("meeting_id"),
            "url": active.get("url"),
            "started_at": active.get("started_at"),
            "mode": active.get("mode"),
        }
    return _ok(
        action="status",
        enabled=enabled,
        provider=provider,
        local_backend=local_backend,
        provider_error=provider_error,
        active_capture=active_capture,
    )


def _import_transcript(transcript: Optional[str], meeting_id: Optional[str]) -> str:
    """Store transcript text as a capture under ``workspace/meetings/`` so
    ``list``/``show`` can read it back. Written verbatim; refuses to overwrite
    an existing capture with the same id."""
    text = transcript if isinstance(transcript, str) else ""
    if not text.strip():
        return _err("transcript is required for action='import'")
    suffix = _slug(meeting_id or "") or uuid.uuid4().hex[:8]
    capture_id = f"imported-{suffix}"
    capture_dir = _meetings_root() / capture_id
    transcript_path = capture_dir / "transcript.txt"
    if transcript_path.is_file():
        return _err(
            f"capture {capture_id!r} already exists — pick a different meeting_id",
            meeting_id=capture_id,
        )
    capture_dir.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(text, encoding="utf-8")
    lines = _read_transcript_file(transcript_path)
    return _ok(
        action="import",
        meeting_id=capture_id,
        path=str(transcript_path),
        lines=len(lines),
        chars=len(text),
    )


def transcripts_tool(args: dict[str, Any]) -> str:
    """Dispatch one ``transcripts`` call. Unknown actions error naming the
    supported verbs; per-action argument errors name the missing argument."""
    action = str((args or {}).get("action") or "").strip().lower()
    if action == "list":
        limit = args.get("limit", LIST_DEFAULT_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = LIST_DEFAULT_LIMIT
        return _list_captures(limit)
    if action == "show":
        last = args.get("last")
        try:
            last = int(last) if last is not None else None
        except (TypeError, ValueError):
            last = None
        return _show_capture(args.get("meeting_id"), last)
    if action == "transcribe":
        return _transcribe_file(args.get("file"), args.get("model"))
    if action == "status":
        return _status()
    if action == "import":
        return _import_transcript(args.get("transcript"), args.get("meeting_id"))
    return _err(
        "unknown action — use list, show, transcribe, status, or import", action=action
    )


TRANSCRIPTS_SCHEMA = {
    "name": "transcripts",
    "description": (
        "List, read, transcribe, or store meeting and voice transcripts. "
        "`list` shows stored transcript captures (newest first); `show` reads one "
        "capture back (pass `meeting_id`, or omit it to read the active capture; "
        "`last` returns only the final N lines). `transcribe` turns an audio file "
        "into text with the configured speech-to-text provider. `status` reports "
        "which speech-to-text provider is configured and whether a capture is "
        "active. `import` stores transcript text as a capture so `list` and `show` "
        "can read it back later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "show", "transcribe", "status", "import"],
                "description": "What to do: list captures, show one, transcribe an audio file, report status, or import transcript text.",
            },
            "meeting_id": {
                "type": "string",
                "description": (
                    "Capture id from `list` (for `show`); for `import`, an optional "
                    "label for the stored capture. Omit `meeting_id` on `show` to read "
                    "the active capture."
                ),
            },
            "file": {
                "type": "string",
                "description": (
                    "Audio file to transcribe (for `transcribe`). Absolute path, or "
                    "relative to the workspace."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional speech-to-text model override for `transcribe` "
                    "(provider default otherwise)."
                ),
            },
            "transcript": {
                "type": "string",
                "description": "Transcript text to store (for `import`).",
            },
            "last": {
                "type": "integer",
                "minimum": 1,
                "description": "For `show`: return only the last N lines of the capture.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "For `list`: maximum captures to return (default 20).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

from tools.registry import registry

registry.register(
    name="transcripts",
    toolset="transcripts",
    schema=TRANSCRIPTS_SCHEMA,
    handler=lambda args, **kw: transcripts_tool(args),
    emoji="📝",
)
