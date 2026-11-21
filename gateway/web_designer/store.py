"""Durable web-designer run store (Phase 1 spine).

Mirrors the autonomous-mode run store (``gateway/autonomous_state.py``):
per-home JSON files, 0700 dirs, 0600 files, ``atomic_json_write``. A run
lives under ``<home>/web-designer/<run_id>/`` with ``state.json`` (the
``RunRecord``), an append-only ``audit.log`` (JSONL, one line per stage /
decision / tool result) and the project workspace under ``project/``.

One active run per user/thread: :func:`active_run_for` is the load gate's
check (audit coordinator 6.1). A crash never resurrects a run as live:
:func:`reset_on_boot` marks an interrupted ``running`` /
``awaiting_checkpoint`` run ``failed`` so a resume ask can find it.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from utils import atomic_json_write

from .types import RunRecord, transition_allowed

logger = logging.getLogger(__name__)

_STATE_FILENAME = "state.json"
_AUDIT_FILENAME = "audit.log"
_PROJECT_DIRNAME = "project"


def web_designer_home(home: str | Path) -> Path:
    """``<home>/web-designer/`` — the add-on's root under a profile home."""
    return Path(home) / "web-designer"


def run_dir(home: str | Path, run_id: str) -> Path:
    return web_designer_home(home) / run_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def create_run(
    *, home: str | Path, thread_id: str, user_id: str, chat_id: str,
    run_id: Optional[str] = None,
) -> RunRecord:
    """Create a run record (born ``idle``) and its directory. The caller
    must transition it to ``running`` via :func:`start_run`."""
    rid = run_id or uuid.uuid4().hex[:16]
    record = RunRecord(
        run_id=rid, thread_id=thread_id, user_id=user_id,
        chat_id=chat_id, home=str(home), started_at=_now_iso(),
        last_step_at=_now_iso(),
    )
    base = run_dir(home, rid)
    _ensure_dir(base)
    _ensure_dir(base / _PROJECT_DIRNAME)
    save_run(record)
    append_audit(record, "run_created", {"thread_id": thread_id})
    return record


def state_path(home: str | Path, run_id: str) -> Path:
    return run_dir(home, run_id) / _STATE_FILENAME


def save_run(record: RunRecord) -> None:
    """Checkpoint the record atomically (0600). A failed write raises —
    the machine must halt before starting the next stage (audit 6.27)."""
    path = state_path(record.home, record.run_id)
    _ensure_dir(path.parent)
    record.last_step_at = _now_iso()
    atomic_json_write(path, record.to_dict(), mode=0o600)


def load_run(home: str | Path, run_id: str) -> Optional[RunRecord]:
    """Load a run record, or None when it does not exist / is unreadable."""
    path = state_path(home, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return RunRecord.from_dict(data)
    except (KeyError, TypeError, ValueError):
        logger.warning("web-designer run %s has an unreadable state file", run_id)
        return None


def list_runs(home: str | Path) -> list[RunRecord]:
    """All run records under the home, newest first."""
    base = web_designer_home(home)
    if not base.is_dir():
        return []
    records = []
    for child in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
        if not child.is_dir():
            continue
        record = load_run(home, child.name)
        if record is not None:
            records.append(record)
    records.sort(key=lambda r: r.started_at or "", reverse=True)
    return records


def active_run_for(home: str | Path, thread_id: str) -> Optional[RunRecord]:
    """The one active run for a thread, if any. Active means it can still
    advance: ``running``, ``awaiting_checkpoint``, ``needs_input`` or
    ``paused``. A second run on the same thread is refused while one is
    active (audit 6.1: one active web-design run per user/thread)."""
    for record in list_runs(home):
        if record.thread_id == thread_id and record.status in {
            "running", "awaiting_checkpoint", "needs_input", "paused",
        }:
            return record
    return None


def transition(record: RunRecord, target: str, *, reason: str = "") -> RunRecord:
    """Move the run to a new status. Illegal transitions raise ValueError
    and change nothing — invalid transitions fail closed, never silently."""
    if not transition_allowed(record.status, target):
        raise ValueError(
            f"web-designer run {record.run_id}: illegal transition "
            f"{record.status!r} -> {target!r}"
        )
    record.status = target
    record.last_step_at = _now_iso()
    save_run(record)
    append_audit(record, "transition", {"to": target, "reason": reason})
    return record


def start_run(record: RunRecord) -> RunRecord:
    return transition(record, "running", reason="load gate passed")


def complete_stage(
    record: RunRecord, stage: str, *, data: dict[str, Any] | None = None,
) -> RunRecord:
    """Checkpoint a completed stage: record its data, mark it complete,
    advance the current-stage pointer, and persist atomically. The
    checkpoint write happens before the machine starts the next stage
    (audit 6.27: never advance on uncheckpointed state)."""
    if data is not None:
        record.stage_data[stage] = data
    if stage not in record.completed_stages:
        record.completed_stages.append(stage)
    record.last_step_at = _now_iso()
    save_run(record)
    append_audit(record, "stage_completed", {"stage": stage})
    return record


def append_audit(record: RunRecord, event: str, payload: dict[str, Any]) -> None:
    """Append one line to the run's audit log (audit 6.30). Append-only:
    the log is the evidence for every claim in the delivery summary."""
    path = run_dir(record.home, record.run_id) / _AUDIT_FILENAME
    _ensure_dir(path.parent)
    line = json.dumps(
        {"ts": _now_iso(), "run_id": record.run_id, "event": event, **payload},
        ensure_ascii=False,
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_audit(record: RunRecord) -> list[dict[str, Any]]:
    """Audit log entries in order (empty when the log is missing)."""
    path = run_dir(record.home, record.run_id) / _AUDIT_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def reset_on_boot(home: str | Path) -> list[RunRecord]:
    """Boot recovery: an interrupted ``running`` / ``awaiting_checkpoint``
    run is marked ``failed`` so it is never silently resumed; the resume
    ask can find it as a failed run. Returns the runs that were failed."""
    failed = []
    for record in list_runs(home):
        if record.status in {"running", "awaiting_checkpoint"}:
            previous = record.status
            record.status = "failed"
            record.last_step_at = _now_iso()
            save_run(record)
            append_audit(record, "boot_reset", {"from": previous})
            failed.append(record)
    return failed
