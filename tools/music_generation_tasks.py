#!/usr/bin/env python3
"""Background music-generation task records for ``music_generate``.

One JSON record per generation under
``$HERMES_HOME/cache/generated/tool-music-generation/tasks/<task_id>.json``
(profile-scoped: the home is captured by the tool handler, never derived from
process state inside the worker thread). Statuses: ``pending`` → ``running`` →
``succeeded`` / ``failed``; a ``pending``/``running`` record with no live worker
thread (process restart) reads back as ``interrupted``.

Duplicate guard (mirrors the external agent's ``findDuplicateGuard…TaskForSession``):
a live task with the same request key is returned instead of starting a second
generation, and a succeeded task with the same key is reused so a repeated
request returns the same track instead of spending twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import provider_media

logger = logging.getLogger(__name__)

TASKS_KIND = f"{provider_media.GENERATED_SUBDIR}/tool-music-generation/tasks"
ACTIVE_STATUSES = ("pending", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "interrupted")

_lock = threading.Lock()
_live: set[tuple[str, str]] = set()


def build_request_key(
    *, prompt: str, provider: Optional[str], model: Optional[str], lyrics: Optional[str],
    instrumental: Optional[bool], duration: Optional[int], format: Optional[str],
    filename: Optional[str], reference_image_urls: Optional[list[str]],
) -> str:
    """Stable hash of the normalised request; identical requests share a key."""
    canonical = json.dumps(
        {
            "prompt": prompt, "provider": provider, "model": model, "lyrics": lyrics,
            "instrumental": instrumental, "duration": duration, "format": format,
            "filename": filename,
            "reference_image_urls": sorted(reference_image_urls or []),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tasks_dir(home: Path) -> Path:
    path = Path(home) / "cache" / TASKS_KIND
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(home: Path, task_id: str) -> Path:
    return _tasks_dir(home) / f"{task_id}.json"


def _read_record(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.debug("Unreadable music task record %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _write_record(home: Path, record: dict[str, Any]) -> dict[str, Any]:
    record["updated_at"] = _now()
    path = _record_path(home, record["id"])
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    return record


def create_task(
    home: Path, *, request_key: str, prompt: str, provider: Optional[str], model: Optional[str],
    lyrics: Optional[str] = None, instrumental: Optional[bool] = None,
    duration: Optional[int] = None, format: Optional[str] = None,
    filename: Optional[str] = None, reference_image_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Create a ``pending`` record and return it."""
    record = {
        "id": uuid.uuid4().hex[:12], "request_key": request_key, "status": "pending",
        "prompt": prompt, "provider": provider, "model": model, "lyrics": lyrics,
        "instrumental": instrumental, "duration": duration, "format": format,
        "filename": filename, "reference_image_urls": list(reference_image_urls or []),
        "progress": "Queued", "audio": None, "duration_seconds": None,
        "lyrics_returned": None, "error": None, "error_type": None,
        "attempts": [], "ignored_overrides": [], "normalization": {},
        "created_at": _now(), "updated_at": _now(),
    }
    return _write_record(home, record)


def mark_live(home: Path, task_id: str) -> None:
    with _lock:
        _live.add((str(home), task_id))


def mark_done(home: Path, task_id: str) -> None:
    with _lock:
        _live.discard((str(home), task_id))


def is_live(home: Path, task_id: str) -> bool:
    with _lock:
        return (str(home), task_id) in _live


def update_task(home: Path, task_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Apply *fields* to the record (status transitions, progress, results)."""
    path = _record_path(home, task_id)
    record = _read_record(path)
    if record is None:
        return None
    record.update(fields)
    return _write_record(home, record)


def _reconcile(home: Path, record: dict[str, Any]) -> dict[str, Any]:
    """A ``pending``/``running`` record with no live worker is ``interrupted``
    (the process died mid-generation)."""
    if record.get("status") in ACTIVE_STATUSES and not is_live(home, record["id"]):
        return update_task(
            home, record["id"], status="interrupted", progress="Interrupted",
            error="Generation was interrupted (the process restarted before it finished).",
            error_type="interrupted",
        ) or record
    return record


def get_task(home: Path, task_id: str) -> Optional[dict[str, Any]]:
    """Load a record; a ``pending``/``running`` record with no live worker is
    reconciled to ``interrupted`` (the process died mid-generation)."""
    record = _read_record(_record_path(home, task_id))
    if record is None:
        return None
    return _reconcile(home, record)


def list_tasks(home: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first task records (reconciled like :func:`get_task`)."""
    records = []
    for path in sorted(_tasks_dir(home).glob("*.json"), reverse=True):
        record = _read_record(path)
        if record is not None:
            records.append(_reconcile(home, record))
    records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    out = []
    for record in records[:limit]:
        fresh = get_task(home, str(record["id"]))
        out.append(fresh if fresh is not None else record)
    return out


def find_duplicate(home: Path, request_key: str) -> Optional[dict[str, Any]]:
    """Live task with this key first, else the most recent succeeded one."""
    tasks = list_tasks(home, limit=100)
    for record in tasks:
        if (record.get("request_key") == request_key
                and record.get("status") in ACTIVE_STATUSES
                and is_live(home, record["id"])):
            return record
    for record in tasks:
        if record.get("request_key") == request_key and record.get("status") == "succeeded":
            return record
    return None


def most_recent_active(home: Path) -> Optional[dict[str, Any]]:
    """Most recent live (pending/running) task, or None."""
    for record in list_tasks(home, limit=20):
        if record.get("status") in ACTIVE_STATUSES and is_live(home, record["id"]):
            return record
    return None


def spawn_task_thread(home: Path, task_id: str, target: Any, *args: Any, **kwargs: Any) -> threading.Thread:
    """Start *target* on a daemon thread under the spawner's contextvars (so the
    profile secret scope survives the turn) and track it as live until it ends."""
    from agent.memory_provider import spawn_context_thread

    def _wrapped() -> None:
        mark_live(home, task_id)
        try:
            target(*args, **kwargs)
        finally:
            mark_done(home, task_id)

    thread = spawn_context_thread(_wrapped, name=f"music-generate-{task_id}", daemon=True)
    # Mark live BEFORE start: a status poll between create and thread start must
    # not reconcile the just-created record to "interrupted".
    mark_live(home, task_id)
    thread.start()
    return thread
