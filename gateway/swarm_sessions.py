"""Private storage and lifecycle helpers for Telegram swarm/agent sessions.

Mirrors ``gateway.deep_research_sessions``: one private directory per Telegram
user under the profile home, one ``session.json`` record per swarm, atomic
writes (0600 files, 0700 directories), owner-bound loads, and an allowlisted
update path. A record tracks one live agent, one async delegation, or one
durable Kanban swarm; ephemeral swarms have no resume path by design (the
lifecycle ``reconnect()`` reports ``RECONNECT_UNAVAILABLE`` and terminal
snapshots are retained one hour), so only Kanban swarms survive a restart.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_USER_ID_RE = re.compile(r"^[0-9]{1,24}$")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ALLOWED_KINDS = {"delegation", "kanban", "lifecycle", "moa"}
_ALLOWED_STATUS = {"starting", "running", "paused", "completed", "failed", "interrupted", "cancelled"}
# Live statuses reported by the delegation/subagent registries. Terminal
# statuses are normalised to the durable set when a snapshot is merged.
_LIVE_STATUS = _ALLOWED_STATUS | {"stalling", "stalled", "finalizing", "pending", "succeeded",
                                 "cancel_requested", "unknown", "ready", "todo", "done", "blocked"}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _identity(user_id: str | int) -> str:
    value = str(user_id or "").strip()
    if not _USER_ID_RE.fullmatch(value):
        raise ValueError("Swarm sessions require a numeric Telegram user ID")
    return value


def _base_dir(profile_home: str | Path, user_id: str | int) -> Path:
    home = Path(profile_home).expanduser().resolve(strict=False)
    base = home / "swarm_sessions" / _identity(user_id)
    if base.resolve(strict=False) != base and not base.resolve(strict=False).is_relative_to(home):
        raise ValueError("Invalid swarm storage path")
    return base


def _session_dir(profile_home: str | Path, user_id: str | int, session_id: str) -> Path:
    sid = str(session_id or "").strip().lower()
    if not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("Invalid swarm session ID")
    base = _base_dir(profile_home, user_id)
    target = base / sid
    resolved_base = base.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_base):
        raise ValueError("Swarm session path escaped its private directory")
    return target


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def create_session(
    profile_home: str | Path,
    user_id: str | int,
    *,
    kind: str,
    source_id: str,
    goal: str,
    chat_id: str | int,
    chat_type: str,
    thread_id: str | int | None = None,
    trigger_message_id: str | int | None = None,
    board: str | None = None,
    workers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a private session directory and a durable metadata record."""
    kind_value = str(kind or "").strip().lower()
    if kind_value not in _ALLOWED_KINDS:
        raise ValueError("Swarm kind must be delegation, kanban, lifecycle, or moa")
    source = str(source_id or "").strip()
    if not source or len(source) > 200:
        raise ValueError("Swarm source ID must contain 1–200 characters")
    goal_value = str(goal or "").strip()
    if not goal_value or len(goal_value) > 4000:
        raise ValueError("Swarm goal must contain 1–4,000 characters")
    worker_rows = []
    for item in workers or []:
        if not isinstance(item, dict) or not str(item.get("label") or "").strip():
            raise ValueError("Swarm workers need a label")
        worker_rows.append({
            "label": str(item["label"])[:200],
            "status": str(item.get("status") or "pending")[:80],
            "detail": str(item.get("detail") or "")[:500],
            "task_id": str(item.get("task_id") or "")[:100] or None,
        })
    base = _base_dir(profile_home, user_id)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    for _ in range(5):
        session_id = uuid.uuid4().hex[:12]
        directory = _session_dir(profile_home, user_id, session_id)
        try:
            directory.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("Could not allocate a unique swarm session directory")
    record: dict[str, Any] = {
        "session_id": session_id,
        "user_id": _identity(user_id),
        "chat_id": str(chat_id),
        "chat_type": str(chat_type or "unknown"),
        "thread_id": str(thread_id) if thread_id is not None else None,
        "trigger_message_id": str(trigger_message_id) if trigger_message_id is not None else None,
        "progress_message_id": None,
        "kind": kind_value,
        "source_id": source,
        "board": str(board) if board else None,
        "goal": goal_value,
        "status": "starting",
        "phase": "starting",
        "workers": worker_rows,
        "failure_class": None,
        "last_error": None,
        "result_summary": None,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "session_dir": str(directory.resolve()),
    }
    try:
        with _lock_for(directory / "session.json"):
            _write_json(directory / "session.json", record)
    except BaseException:
        try:
            (directory / "session.json").unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass
        raise
    return record


def resolve_session_dir(profile_home: str | Path, user_id: str | int, session_id: str) -> Path:
    """Return an existing session directory only when its durable record matches the owner."""
    directory = _session_dir(profile_home, user_id, session_id)
    record_path = directory / "session.json"
    if not record_path.is_file():
        raise FileNotFoundError("Swarm session does not exist")
    resolved = directory.resolve(strict=True)
    base = _base_dir(profile_home, user_id).resolve(strict=True)
    if not resolved.is_relative_to(base):
        raise ValueError("Swarm session escaped its private directory")
    record = load_session(profile_home, user_id, session_id)
    if record.get("session_id") != session_id or record.get("user_id") != _identity(user_id):
        raise PermissionError("Swarm session owner mismatch")
    return resolved


def load_session(profile_home: str | Path, user_id: str | int, session_id: str) -> dict[str, Any]:
    directory = _session_dir(profile_home, user_id, session_id)
    path = directory / "session.json"
    with _lock_for(path):
        record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Swarm session record is invalid")
    if record.get("user_id") != _identity(user_id):
        raise PermissionError("Swarm session owner mismatch")
    if record.get("session_id") != str(session_id):
        raise ValueError("Swarm session ID mismatch")
    return record


def update_session(
    profile_home: str | Path,
    user_id: str | int,
    session_id: str,
    **changes: Any,
) -> dict[str, Any]:
    """Update only known lifecycle fields while preserving session ownership."""
    allowed = {
        "status", "phase", "thread_id", "trigger_message_id", "progress_message_id",
        "workers", "failure_class", "last_error", "result_summary", "goal",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("Unsupported swarm session fields: " + ", ".join(sorted(unknown)))
    if "status" in changes and changes["status"] not in _ALLOWED_STATUS:
        raise ValueError("Invalid swarm session status")
    if "workers" in changes:
        rows = changes["workers"]
        if not isinstance(rows, list) or any(
            not isinstance(item, dict) or not str(item.get("label") or "").strip() for item in rows
        ):
            raise ValueError("Swarm workers must be a list of labelled rows")
        changes["workers"] = [{
            "label": str(item["label"])[:200],
            "status": str(item.get("status") or "pending")[:80],
            "detail": str(item.get("detail") or "")[:500],
            "task_id": str(item.get("task_id") or "")[:100] or None,
        } for item in rows]
    for field in ("thread_id", "trigger_message_id", "progress_message_id"):
        if field in changes and changes[field] is not None:
            changes[field] = str(changes[field])[:100]
    for field in ("phase", "failure_class"):
        if field in changes and changes[field] is not None:
            changes[field] = str(changes[field])[:160]
    if "last_error" in changes and changes["last_error"] is not None:
        changes["last_error"] = str(changes["last_error"])[:500]
    if "result_summary" in changes and changes["result_summary"] is not None:
        changes["result_summary"] = str(changes["result_summary"])[:8000]
    if "goal" in changes:
        changes["goal"] = str(changes["goal"] or "").strip()[:4000]
    path = _session_dir(profile_home, user_id, session_id) / "session.json"
    with _lock_for(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("user_id") != _identity(user_id):
            raise PermissionError("Swarm session owner mismatch")
        record.update(changes)
        record["updated_at"] = _utc_now()
        _write_json(path, record)
    return record


def find_session_for_topic(profile_home: str | Path, chat_id: str | int,
                           thread_id: str | int | None) -> dict[str, Any] | None:
    """Find the swarm owner for an exact dedicated topic, across user scopes.

    Read metadata only; never reconcile or mutate saved sessions here.
    """
    if thread_id in (None, "", 0, "0"):
        return None
    base = Path(profile_home).expanduser().resolve(strict=False) / "swarm_sessions"
    if not base.exists():
        return None
    matches = []
    for owner in base.iterdir():
        if not _USER_ID_RE.fullmatch(owner.name):
            continue
        if owner.is_symlink() or not owner.is_dir():
            raise ValueError("Cannot verify saved swarm topic ownership: invalid owner directory")
        for directory in owner.iterdir():
            if not _SESSION_ID_RE.fullmatch(directory.name):
                continue
            path = directory / "session.json"
            if directory.is_symlink() or path.is_symlink() or not directory.is_dir():
                raise ValueError("Cannot verify saved swarm topic ownership: invalid session path")
            try:
                record = load_session(profile_home, owner.name, directory.name)
            except Exception as exc:
                raise ValueError("Cannot verify saved swarm topic ownership: unreadable session metadata") from exc
            if str(record.get("chat_id")) == str(chat_id) and str(record.get("thread_id")) == str(thread_id):
                matches.append(record)
    if len(matches) > 1:
        raise ValueError("Multiple swarm sessions claim the same Telegram topic; owner review is required")
    return matches[0] if matches else None


def list_sessions(profile_home: str | Path, user_id: str | int, *, limit: int = 10) -> list[dict[str, Any]]:
    """List only this Telegram user's sessions, newest first."""
    base = _base_dir(profile_home, user_id)
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for directory in base.iterdir():
        if not _SESSION_ID_RE.fullmatch(directory.name) or not directory.is_dir():
            continue
        try:
            rows.append(load_session(profile_home, user_id, directory.name))
        except Exception:
            continue
    rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
    return rows[: max(1, min(int(limit), 50))]


# ---------------------------------------------------------------- live snapshot

def _normalise_status(raw: Any) -> str:
    """Map a live registry status onto the durable status vocabulary.

    Unknown values surface as-is (never silently rewritten to a terminal
    state), so the card cannot claim a swarm finished when it did not.
    """
    value = str(raw or "").strip().lower()
    if value in _ALLOWED_STATUS:
        return value
    mapping = {
        "succeeded": "completed", "done": "completed",
        "cancel_requested": "running", "stalling": "running", "stalled": "running",
        "finalizing": "running", "ready": "starting", "todo": "starting",
    }
    return mapping.get(value, value or "unknown")


def _delegation_goal(record: dict[str, Any]) -> str:
    goal = str(record.get("goal") or "").strip()
    if goal:
        return goal
    goals = record.get("goals")
    if isinstance(goals, list):
        parts = [str(item).strip() for item in goals if str(item).strip()]
        if parts:
            return "; ".join(parts)
    return ""


def _delegation_workers(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, child in enumerate(record.get("children_activity") or []):
        if not isinstance(child, dict):
            continue
        tool = str(child.get("current_tool") or "")
        calls = child.get("api_calls", "?")
        idle = child.get("seconds_since_activity")
        detail = f"{tool} · {calls} calls" if tool else f"{calls} calls"
        if idle is not None:
            detail += f" · idle {idle:.0f}s"
        rows.append({"label": f"worker {index + 1}", "status": "running", "detail": detail, "task_id": None})
    return rows


def _live_entries() -> list[dict[str, Any]]:
    """Snapshot rows for live async delegations and live subagent children.

    Fail-open per source: a broken sampler must not hide the other registry.
    """
    entries: list[dict[str, Any]] = []
    try:
        from tools.async_delegation import list_async_delegations
        for record in list_async_delegations():
            status = str(record.get("status") or "")
            if status not in _LIVE_STATUS:
                continue
            entries.append({
                "entry_id": f"live:delegation:{record.get('delegation_id')}",
                "kind": "delegation",
                "source_id": str(record.get("delegation_id") or ""),
                "status": _normalise_status(status),
                "goal": _delegation_goal(record),
                "workers": _delegation_workers(record),
                "session_key": str(record.get("session_key") or ""),
                "board": None,
                "durable_session_id": None,
                "result_summary": _delegation_result(record),
                "created_at": "",
                "updated_at": "",
            })
    except Exception:
        pass
    try:
        from tools.delegate_tool_registry import list_active_subagents
        for record in list_active_subagents():
            subagent_id = str(record.get("subagent_id") or "")
            if not subagent_id:
                continue
            entries.append({
                "entry_id": f"live:lifecycle:{subagent_id}",
                "kind": "lifecycle",
                "source_id": subagent_id,
                "status": _normalise_status(record.get("status")),
                "goal": str(record.get("goal") or ""),
                "workers": [{
                    "label": "worker",
                    "status": _normalise_status(record.get("status")),
                    "detail": str(record.get("model") or ""),
                    "task_id": None,
                }],
                "session_key": "",
                "board": None,
                "durable_session_id": None,
                "result_summary": None,
                "created_at": "",
                "updated_at": "",
            })
    except Exception:
        pass
    return entries


def _delegation_result(record: dict[str, Any]) -> str | None:
    """One-line result for a finished delegation, from its stored outcome."""
    results = record.get("results")
    if isinstance(results, list) and results:
        parts = []
        for item in results:
            if isinstance(item, dict):
                summary = str(item.get("summary") or item.get("error") or "").strip()
                if summary:
                    parts.append(summary)
        if parts:
            return " | ".join(parts)[:4000]
    summary = str(record.get("summary") or record.get("error") or "").strip()
    return summary[:4000] if summary else None


def collect_swarm_entries(profile_home: str | Path, user_id: str | int, chat_id: str | int,
                          *, limit: int = 10) -> list[dict[str, Any]]:
    """Merge durable swarm records with the live registries for one chat.

    A durable record whose ``source_id`` matches a live delegation or child
    merges into that live row (live status wins; the durable goal fills a
    blank live goal). Durable records with no live counterpart are listed on
    their own, newest first. MoA has no registry to poll, so it never appears
    here: its only observable state is the per-reference completion count on
    the parent turn's progress callback.
    """
    live = _live_entries()
    by_source = {entry["source_id"]: entry for entry in live if entry["source_id"]}
    merged: list[dict[str, Any]] = list(live)
    try:
        durable = [row for row in list_sessions(profile_home, user_id, limit=limit)
                   if str(row.get("chat_id")) == str(chat_id)]
    except Exception:
        durable = []
    for row in durable:
        source_id = str(row.get("source_id") or "")
        live_row = by_source.get(source_id) if source_id else None
        if live_row is not None:
            live_row["durable_session_id"] = str(row.get("session_id"))
            if not live_row["goal"]:
                live_row["goal"] = str(row.get("goal") or "")
            if not live_row["workers"] and isinstance(row.get("workers"), list):
                live_row["workers"] = row["workers"]
            if not live_row["result_summary"]:
                live_row["result_summary"] = str(row.get("result_summary") or "") or None
            continue
        merged.append({
            "entry_id": f"swarm:{row.get('session_id')}",
            "kind": str(row.get("kind") or "unknown"),
            "source_id": source_id,
            "status": _normalise_status(row.get("status")),
            "goal": str(row.get("goal") or ""),
            "workers": list(row.get("workers") or []),
            "session_key": "",
            "board": row.get("board"),
            "durable_session_id": str(row.get("session_id")),
            "result_summary": str(row.get("result_summary") or "") or None,
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        })
    return merged
