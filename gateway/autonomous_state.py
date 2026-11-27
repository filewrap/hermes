"""Autonomous mode run store + state machine (file-backed, per launch home).

The split the spec requires:

- ``platforms.telegram.autonomous_mode`` in config.yaml is the persisted
  master switch ("autonomous was set up / last enabled"). It survives
  restarts on purpose: it records the owner's intent, not the live mode.
- The *live* flag lives here, in ``<launch-home>/autonomous/state.json``:
  ``{active, current_run_id, pinned_message_id, chat_id}``. It survives
  restarts too — autonomous mode never disables itself (see
  :func:`fail_interrupted_run_on_boot`); a crash never resurrects a run
  as live, and the pinned message stays because it still claims a mode
  that is on.
- One record per run in ``<launch-home>/autonomous/runs/<run_id>.json``,
  checkpointed atomically after every completed tool round (see
  :func:`checkpoint`), so a crash mid-turn does not lose the run.

Layout mirrors the ``suggest_task`` pending store
(``tools/suggest_task_tool.py``): per-home JSON files, 0700 dirs, 0600
files, ``atomic_json_write``. No SessionDB schema work.

State machine (LangGraph ``should_continue`` edge, owned by code, not by
prompt) — ``idle | running | needs_input | done | stopped | failed``::

    idle → running            on enable (new run record created)
    running → needs_input     the agent waits for something only the owner can supply
    needs_input → running     the owner's next message
    running → done            the agent declares the goal complete
    running → stopped         owner /stop or /autonomous disable
    running → failed          unrecoverable error

Every other transition raises :class:`ValueError` — invalid transitions
fail closed, never silently. ``done`` is terminal; ``stopped`` / ``failed``
are terminal for the ordinary transition table, and only the owner's explicit
Resume choice re-opens one (:func:`reopen_run`) — a previous run is never
silently resumed. Restart marks an interrupted ``running`` run ``failed``
(:func:`fail_interrupted_run_on_boot`) and leaves the live flag, the current
run id and the pinned message exactly as they were, so the resume ask can
find it and the pinned enable card still claims a mode that is on — unless
the agent's last completed tool call was ``autonomous_done``: the agent
already signaled the goal complete, so the run is marked ``done``, and a
record an earlier boot already stamped ``failed`` on that evidence is
healed to ``done`` the same way.

This module never imports Telethon: :func:`fail_interrupted_run_on_boot`
touches only the store; it unpins nothing.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from utils import atomic_json_write

logger = logging.getLogger(__name__)

#: Serialises concurrent token reports into one run record (parallel
#: workers report after every response; a lost delta under-counts the run).
_TOKEN_LOCK = threading.Lock()

#: States a run record can be in. ``idle`` is the pre-start state of a
#: record created by :func:`create_run`; a record is born idle and only
#: becomes ``running`` via :func:`start_run`.
RUN_STATES = ("idle", "running", "needs_input", "done", "stopped", "failed")

#: Allowed transitions: current state -> states it may move to.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "idle": frozenset({"running"}),
    "running": frozenset({"needs_input", "done", "stopped", "failed"}),
    "needs_input": frozenset({"running", "stopped", "failed"}),
    "done": frozenset(),
    "stopped": frozenset(),
    "failed": frozenset(),
}

#: Fields a run record carries. ``checkpoint`` / ``transition`` refuse
#: update keys outside this set, so a typo fails loudly instead of
#: silently dropping a ledger field.
_RUN_RECORD_FIELDS = frozenset({
    "run_id", "session_key", "goal", "state", "started_at", "last_step_at",
    "steps", "input_tokens", "output_tokens", "stall_count", "last_tool",
    "last_error", "stopped_by", "model", "killed", "recent_tools",
    "recent_agents",
})

_STATE_FILENAME = "state.json"
_RUNS_DIRNAME = "runs"
_DIRNAME = "autonomous"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def autonomous_dir(home: str | Path) -> Path:
    """``<home>/autonomous`` — the per-launch-home autonomous store."""
    return Path(home) / _DIRNAME


def _state_path(home: str | Path) -> Path:
    return autonomous_dir(home) / _STATE_FILENAME


def _runs_dir(home: str | Path) -> Path:
    return autonomous_dir(home) / _RUNS_DIRNAME


def _run_path(home: str | Path, run_id: str) -> Path:
    """Run-record path; a run id carrying a separator is refused, never resolved."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"Invalid autonomous run id: {run_id!r}")
    return _runs_dir(home) / f"{run_id}.json"


def _ensure_dirs(home: str | Path) -> None:
    """Create the store dirs (0700, same shape as the suggest_task store)."""
    for directory in (autonomous_dir(home), _runs_dir(home)):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write, 0600 from creation (never umask-readable)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json_write(path, data, mode=0o600)


def _read_json_or_none(path: Path) -> Optional[dict[str, Any]]:
    """The JSON object at *path*, or None when missing/unreadable/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- Active flag (state.json) ------------------------------------------------

_DEFAULT_STATE = {"active": False, "current_run_id": None,
                  "pinned_message_id": None, "chat_id": None,
                  "closed_note": None}


def load_state(home: str | Path) -> dict[str, Any]:
    """The live-mode state; a missing or corrupt file reads as inactive."""
    stored = _read_json_or_none(_state_path(home))
    if stored is None:
        return dict(_DEFAULT_STATE)
    return {**_DEFAULT_STATE, **{k: v for k, v in stored.items() if k in _DEFAULT_STATE}}


def _save_state(home: str | Path, state: dict[str, Any]) -> None:
    _ensure_dirs(home)
    _write_json(_state_path(home), {k: state.get(k) for k in _DEFAULT_STATE})


def is_active(home: str | Path) -> bool:
    """True when autonomous mode is currently live for this launch home."""
    return bool(load_state(home).get("active", False))


def set_active(home: str | Path, active: bool, *, run_id: Optional[str] = None,
               pinned_message_id: Optional[Any] = None,
               chat_id: Optional[Any] = None) -> dict[str, Any]:
    """Set the live flag; returns the stored state.

    Activating stores the run id (and the pinned message, when given).
    Deactivating clears the current run id; the pinned message id is left
    for :func:`clear_pinned_message` — the disable flow unpins + deletes
    it first, then clears the record.
    """
    state = load_state(home)
    state["active"] = bool(active)
    if active:
        if run_id is not None:
            state["current_run_id"] = run_id
        if pinned_message_id is not None:
            state["pinned_message_id"] = pinned_message_id
        if chat_id is not None:
            state["chat_id"] = chat_id
    else:
        state["current_run_id"] = None
    _save_state(home, state)
    return state


def clear_pinned_message(home: str | Path) -> Optional[dict[str, Any]]:
    """Drop the stored pinned message and return it for unpin + delete.

    Returns ``{"pinned_message_id": ..., "chat_id": ...}`` when one was
    stored, else None. The caller (Telegram layer) does the actual unpin
    and delete; this module never imports Telethon.
    """
    state = load_state(home)
    pinned = state.get("pinned_message_id")
    if pinned is None:
        return None
    info = {"pinned_message_id": pinned, "chat_id": state.get("chat_id")}
    state["pinned_message_id"] = None
    state["chat_id"] = None
    _save_state(home, state)
    return info


def _heal_done_runs_stamped_failed(home: str | Path) -> None:
    """Repair records an earlier boot stamped ``failed`` by mistake.

    The boot marker used to fail every interrupted run, including one
    whose agent had already called ``autonomous_done`` — the run's own
    ledger says the goal was declared complete, so ``failed`` contradicts
    what actually happened. A record in exactly that shape (failed, last
    tool autonomous_done, restart error) is repaired to ``done`` and the
    stale error cleared, in place: ``failed → done`` is not a legal
    transition, and the record is wrong, so this mutates it directly
    (the :func:`mark_killed` precedent) instead of forcing it through
    the table. Any other failed run is left exactly as it is.
    """
    for record in _all_runs(home):
        if (str(record.get("state")) != "failed"
                or str(record.get("last_tool") or "") != "autonomous_done"
                or str(record.get("last_error") or "")
                != "gateway restarted mid-run"):
            continue
        record["state"] = "done"
        record["last_error"] = None
        _save_run(home, record)


def fail_interrupted_run_on_boot(home: str | Path) -> Optional[dict[str, Any]]:
    """Boot: mark an interrupted ``running`` run ``failed``; nothing else moves.

    Autonomous mode never disables itself: the live flag, the current run id
    and the pinned message stay exactly as they were, so the pinned enable
    card still claims a mode that is on. A run that was ``running`` when the
    gateway died is marked ``failed`` — the turn is gone, and the record is
    the resume source — so the resume ask on re-enable can find it. Returns
    the failed record, or None when no running run was interrupted.

    One exception: when the agent's last completed tool call was
    ``autonomous_done``, the agent already signaled the goal complete and
    the done transition raced the restart — the run is marked ``done``,
    not failed. Records an earlier boot already stamped ``failed`` on
    that evidence are healed the same way (see
    :func:`_heal_done_runs_stamped_failed`).
    """
    _heal_done_runs_stamped_failed(home)
    state = load_state(home)
    run_id = state.get("current_run_id")
    if isinstance(run_id, str) and run_id:
        record = load_run(home, run_id)
        if record is not None and str(record.get("state")) == "running":
            if str(record.get("last_tool") or "") == "autonomous_done":
                try:
                    return mark_done(home, run_id)
                except ValueError:
                    logger.debug("Autonomous boot marking: run %s already moved on", run_id)
                    return None
            try:
                return mark_failed(home, run_id, last_error="gateway restarted mid-run")
            except ValueError:
                logger.debug("Autonomous boot marking: run %s already moved on", run_id)
    return None


# --- Run records --------------------------------------------------------------

def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def create_run(home: str | Path, session_key: str, goal: str,
               model: Optional[str] = None) -> dict[str, Any]:
    """Create a run record in state ``idle`` and checkpoint it to disk.

    Creation is the idle side of the ``idle → running`` edge; the record
    only becomes ``running`` via :func:`start_run`.
    """
    now = _utcnow()
    record = {
        "run_id": _new_run_id(), "session_key": session_key, "goal": goal,
        "state": "idle", "started_at": now, "last_step_at": now, "steps": 0,
        "input_tokens": 0, "output_tokens": 0, "stall_count": 0,
        "last_tool": None, "last_error": None, "stopped_by": None,
        "model": model, "killed": False,
    }
    _ensure_dirs(home)
    _write_json(_run_path(home, record["run_id"]), record)
    return record


def load_run(home: str | Path, run_id: str) -> Optional[dict[str, Any]]:
    """The run record, or None when missing or unreadable."""
    try:
        path = _run_path(home, run_id)
    except ValueError:
        return None
    record = _read_json_or_none(path)
    if record is None:
        logger.warning("Skipping unreadable autonomous run record: %s", path)
    return record


def _save_run(home: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    _write_json(_run_path(home, str(record["run_id"])), record)
    return record


def transition(home: str | Path, run_id: str, new_state: str,
               **updates: Any) -> dict[str, Any]:
    """Move a run to *new_state*, applying *updates* to the record.

    Raises :class:`ValueError` on an unknown run, an unknown state, an
    update key outside the record fields, or a transition the table does
    not allow — invalid transitions fail closed, never silently.
    """
    if new_state not in RUN_STATES:
        raise ValueError(f"Unknown autonomous run state: {new_state!r}")
    unknown = set(updates) - _RUN_RECORD_FIELDS
    if unknown:
        raise ValueError(f"Unknown autonomous run fields: {sorted(unknown)}")
    if "state" in updates or "run_id" in updates:
        raise ValueError("transition() owns 'state' and 'run_id'; pass them as arguments, not updates")
    record = load_run(home, run_id)
    if record is None:
        raise ValueError(f"Unknown autonomous run: {run_id!r}")
    current = str(record.get("state"))
    if new_state not in _TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"Invalid autonomous transition: {current!r} -> {new_state!r} (run {run_id!r})")
    record["state"] = new_state
    record.update(updates)
    record["last_step_at"] = str(updates.get("last_step_at") or _utcnow())
    return _save_run(home, record)


def start_run(home: str | Path, run_id: str) -> dict[str, Any]:
    """``idle → running`` on enable; marks the mode live for this run."""
    record = transition(home, run_id, "running")
    set_active(home, True, run_id=run_id)
    return record


def mark_needs_input(home: str | Path, run_id: str, **updates: Any) -> dict[str, Any]:
    """``running → needs_input``: the run waits for the owner's next message."""
    return transition(home, run_id, "needs_input", **updates)


def resume_run(home: str | Path, run_id: str) -> dict[str, Any]:
    """``needs_input → running`` on the owner's next message."""
    record = transition(home, run_id, "running")
    set_active(home, True, run_id=run_id)
    return record


def reopen_run(home: str | Path, run_id: str) -> dict[str, Any]:
    """Explicit owner resume: ``stopped`` / ``failed`` / ``needs_input`` / ``done`` → ``running``.

    The ONLY way a terminal run comes back — the Resume tap on the resume
    ask, the status card's Resume Previous, or a per-session Resume on the
    sessions page. The ordinary transition table still refuses these edges
    (see ``test_running_to_stopped_and_failed_are_terminal``), so a resume
    can never happen silently or by accident. A ``done`` run re-opens as a
    continuation of its goal; ``idle`` runs refuse; so does a run that is
    already ``running``.
    """
    record = load_run(home, run_id)
    if record is None:
        raise ValueError(f"Unknown autonomous run: {run_id!r}")
    if record.get("killed"):
        raise ValueError(f"A killed run can never be resumed: {run_id!r}")
    current = str(record.get("state"))
    if current not in {"stopped", "failed", "needs_input", "done"}:
        raise ValueError(f"Only a stopped, failed, waiting or done run can be resumed: {current!r}")
    record["state"] = "running"
    record["last_step_at"] = _utcnow()
    _save_run(home, record)
    set_active(home, True, run_id=run_id)
    clear_closed_note(home, run_id)
    return record


def mark_killed(home: str | Path, run_id: str) -> dict[str, Any]:
    """Mark a run killed: never offered as Resume, never re-opened.

    The kill-switch's record half. A ``running`` / ``needs_input`` run
    moves to ``stopped`` with ``stopped_by="kill"``; an already-terminal
    run (``failed`` — the transition table has no ``failed → stopped``
    edge) keeps its state and is marked killed in place. Either way the
    ``killed`` flag is what :func:`find_resumable_run` and
    :func:`reopen_run` refuse on, so a kill is permanent. Follows the
    :func:`reopen_run` precedent of mutating the record directly and
    saving it, instead of forcing the flag through the transition table.
    """
    record = load_run(home, run_id)
    if record is None:
        raise ValueError(f"Unknown autonomous run: {run_id!r}")
    if str(record.get("state")) in {"running", "needs_input"}:
        record["state"] = "stopped"
    record["stopped_by"] = "kill"
    record["killed"] = True
    record["last_step_at"] = _utcnow()
    return _save_run(home, record)


def mark_done(home: str | Path, run_id: str, **updates: Any) -> dict[str, Any]:
    """``running → done``: the agent declared the goal complete."""
    return transition(home, run_id, "done", **updates)


def mark_stopped(home: str | Path, run_id: str,
                 stopped_by: Optional[str] = None) -> dict[str, Any]:
    """``running → stopped``: owner /stop or /autonomous disable.

    Only the record moves; the caller also clears the live flag with
    :func:`set_active` (and unpins + deletes via :func:`clear_pinned_message`).
    """
    updates: dict[str, Any] = {}
    if stopped_by is not None:
        updates["stopped_by"] = stopped_by
    return transition(home, run_id, "stopped", **updates)


def mark_failed(home: str | Path, run_id: str,
                last_error: Optional[str] = None) -> dict[str, Any]:
    """``running → failed``: unrecoverable error. The record is preserved."""
    updates: dict[str, Any] = {}
    if last_error is not None:
        updates["last_error"] = last_error
    return transition(home, run_id, "failed", **updates)


def delete_run(home: str | Path, run_id: str) -> bool:
    """Delete a run record; the sessions page's Delete button.

    The live current run is refused — deleting the run a live mode is
    attached to would strand the flag and the pin — so a live run raises
    :class:`ValueError` and nothing is removed. An unknown run id also
    raises (the path helper refuses traversal); a known run that is
    already gone reads False. A pending run-closed note for the run is
    dropped with it, so the next turn in its session is never told about
    a deleted run.
    """
    state = load_state(home)
    if (bool(state.get("active"))
            and str(state.get("current_run_id") or "") == str(run_id)):
        raise ValueError(f"Cannot delete the live autonomous run: {run_id!r}")
    path = _run_path(home, run_id)
    if not path.is_file():
        return False
    path.unlink()
    clear_closed_note(home, run_id)
    return True


def _closed_note_for(record: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The one-shot run-closed note for a stopped run, or None.

    Armed by :func:`disable` for any run that is not already ``done``:
    the task was cut off, so the next turn in the run's session must be
    told it is closed. A ``done`` run needs no note — the agent already
    declared the goal complete in the transcript. A record without a
    session key names no DM to tell, so it arms nothing.
    """
    if record is None:
        return None
    if str(record.get("state") or "") == "done":
        return None
    session_key = record.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        return None
    goal = record.get("goal")
    return {
        "run_id": str(record.get("run_id") or ""),
        "session_key": session_key,
        "goal": goal if isinstance(goal, str) else "",
    }


def _arm_closed_note(home: str | Path, note: dict[str, Any]) -> None:
    """Store a run-closed note in state; the next turn in its session takes it."""
    try:
        state = load_state(home)
        state["closed_note"] = note
        _save_state(home, state)
    except Exception:
        logger.debug("Autonomous closed-note arm failed", exc_info=True)


def take_closed_note(home: str | Path, session_key: str) -> Optional[dict[str, Any]]:
    """Consume the pending run-closed note for *session_key*, if any.

    One-shot: the note is cleared from state as it is taken, so only the
    next turn in that session ever sees it. A note for another session is
    left pending for its own DM.
    """
    try:
        state = load_state(home)
        note = state.get("closed_note")
        if not isinstance(note, dict) or str(note.get("session_key") or "") != str(session_key):
            return None
        state["closed_note"] = None
        _save_state(home, state)
        return note
    except Exception:
        logger.debug("Autonomous closed-note take failed", exc_info=True)
        return None


def clear_closed_note(home: str | Path, run_id: str) -> None:
    """Drop a pending run-closed note for *run_id* without showing it.

    The explicit-resume path (:func:`reopen_run`): the owner chose to
    continue this run, so the "task is closed" line must never reach the
    turn that resumes it.
    """
    try:
        state = load_state(home)
        note = state.get("closed_note")
        if isinstance(note, dict) and str(note.get("run_id") or "") == str(run_id):
            state["closed_note"] = None
            _save_state(home, state)
    except Exception:
        logger.debug("Autonomous closed-note clear failed", exc_info=True)


def disable(home: str | Path, *, stopped_by: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Disable the mode: stop the live run (if any), clear the live flag.

    The run record is kept as ``stopped`` (never deleted — it is the resume
    source). A run that is already terminal, or never started, is left as it
    is; the flag still clears. A stopped run that is not already ``done``
    arms a one-shot run-closed note for its session (see
    :func:`take_closed_note`): the next turn in that session is told the
    task is closed, so the agent does not continue it from history.
    Returns the pinned message info for the caller to unpin + delete, or
    None when nothing was pinned.
    """
    state = load_state(home)
    run_id = state.get("current_run_id")
    record = load_run(home, run_id) if isinstance(run_id, str) and run_id else None
    note = _closed_note_for(record)
    if isinstance(run_id, str) and run_id:
        try:
            mark_stopped(home, run_id, stopped_by=stopped_by)
        except ValueError:
            # Already terminal (done/stopped/failed) or never started (idle):
            # the flag still clears; the record is left as it is.
            logger.debug("Autonomous disable: run %s already terminal", run_id)
    set_active(home, False)
    pinned = clear_pinned_message(home)
    if note is not None:
        _arm_closed_note(home, note)
    return pinned


def compact_tokens(value: int) -> str:
    """Compact token total for cards: ``1.2M``, ``3.4B``, ``850K``.

    The compact form reads bare — no brackets, no long number. Callers
    that show a used/budget pair write it as ``"used / budget"``.
    """
    n = int(value or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _all_runs(home: str | Path) -> list[dict[str, Any]]:
    """All run records in the store, newest first (``last_step_at`` desc).

    Best-effort: an unreadable file is skipped, a missing runs dir reads as
    empty. The lookups below share this one scan instead of each globbing
    the dir.
    """
    try:
        runs_dir = _runs_dir(home)
        if not runs_dir.is_dir():
            return []
        records = []
        for path in runs_dir.glob("run_*.json"):
            record = _read_json_or_none(path)
            if isinstance(record, dict):
                records.append(record)
        records.sort(key=lambda r: str(r.get("last_step_at") or ""), reverse=True)
        return records
    except Exception:
        logger.debug("Autonomous run listing failed", exc_info=True)
        return []


def all_runs(home: str | Path) -> list[dict[str, Any]]:
    """All run records in the store, newest first (``last_step_at`` desc).

    The sessions page's listing; a missing store reads as empty.
    """
    return _all_runs(home)


def recent_runs(home: str | Path, limit: int = 3) -> list[dict[str, Any]]:
    """The latest *limit* run records (any state), newest first.

    The status card's "last sessions" list; a missing store reads as empty.
    """
    return _all_runs(home)[:max(0, int(limit))]


def find_resumable_run(home: str | Path) -> Optional[dict[str, Any]]:
    """The latest run that could be resumed (``stopped`` / ``failed`` / ``needs_input`` / ``done``), if any.

    The resume ask on re-enable branches on this; the owner chooses Resume
    (re-open the record) or Start fresh (a new run). A ``done`` run re-opens
    as a continuation of its goal. A killed run is never a candidate,
    whatever its state.
    """
    for record in _all_runs(home):
        if record.get("killed"):
            continue
        if str(record.get("state")) in {"stopped", "failed", "needs_input", "done"}:
            return record
    return None


def find_run_for_session(home: str | Path, session_key: str) -> Optional[dict[str, Any]]:
    """The latest run record for a session (any state), or None.

    The auto-resume drain asks this before restarting an interrupted turn:
    a record in a non-terminal state means the turn belonged to an
    autonomous run and must not be restarted as an ordinary turn.
    """
    for record in _all_runs(home):
        if str(record.get("session_key")) == str(session_key):
            return record
    return None


def checkpoint(home: str | Path, run_id: str, **updates: Any) -> dict[str, Any]:
    """Persist the run ledger after a completed tool round.

    The runner calls this after every tool round — a crash mid-turn must
    not lose the run. No state change; only ledger fields move (steps,
    tokens, last tool / error, stall count, model). Raises
    :class:`ValueError` on an unknown run or an unknown field.
    """
    unknown = set(updates) - _RUN_RECORD_FIELDS
    if unknown:
        raise ValueError(f"Unknown autonomous run fields: {sorted(unknown)}")
    if "state" in updates or "run_id" in updates:
        raise ValueError("checkpoint() never changes state or run_id; use transition()")
    record = load_run(home, run_id)
    if record is None:
        raise ValueError(f"Unknown autonomous run: {run_id!r}")
    record.update(updates)
    record["last_step_at"] = _utcnow()
    return _save_run(home, record)


def add_tokens(home: str | Path, run_id: str, input_delta: int, output_delta: int) -> dict[str, Any]:
    """Add one response's tokens to the run ledger (workers report their own).

    Read-modify-write under a process lock so parallel workers cannot lose
    each other's deltas. The coordinator never calls this: its tokens arrive
    through :func:`checkpoint` (via ``checkpoint_tool``), so the two writers
    never double-count the same response.
    """
    with _TOKEN_LOCK:
        record = load_run(home, run_id)
        if record is None:
            raise ValueError(f"Unknown autonomous run: {run_id!r}")
        return checkpoint(
            home, run_id,
            input_tokens=int(record.get("input_tokens") or 0) + max(0, int(input_delta)),
            output_tokens=int(record.get("output_tokens") or 0) + max(0, int(output_delta)),
        )


def run_token_total(record: dict[str, Any]) -> int:
    """Total tokens (input + output) the run has burned so far."""
    return int(record.get("input_tokens") or 0) + int(record.get("output_tokens") or 0)


def make_run_budget_check(home: str | Path, run_id: str):
    """Callable answering whether the run's token total reached its cap.

    A cap of 0 or less disables the check; a missing record or unreadable
    config answers False (fail open — the refusal and the run-failed paths are
    the enforcement, never this reader). The turn loop calls it at the top of
    each iteration, so a run that crosses its cap mid-response stops before
    the next request.
    """
    def _check() -> bool:
        try:
            cap = _config_max_tokens_per_run(home)
            if cap <= 0:
                return False
            record = load_run(home, run_id)
            if record is None:
                return False
            return run_token_total(record) >= cap
        except Exception:
            logger.debug("Autonomous run budget check failed for run %s", run_id, exc_info=True)
            return False

    return _check


# --- Setting read -------------------------------------------------------------

def _config_max_tokens_per_run(home: str | Path) -> int:
    """``autonomous.max_tokens_per_run`` for *home* (default 1,000,000).

    Same raw-config.yaml read as :func:`_config_mode_enabled` (no merged-config
    dependency); the gateway reads it from the LAUNCH home, never the session
    home. A non-integer or missing value falls back to the default; 0 or
    negative disables the cap.
    """
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(home) / "config.yaml")
        section = config.get("autonomous") if isinstance(config, dict) else None
        if not isinstance(section, dict):
            return 1_000_000
        raw = section.get("max_tokens_per_run", 1_000_000)
        return int(raw)
    except (TypeError, ValueError):
        logger.debug("autonomous.max_tokens_per_run unreadable; using default", exc_info=True)
        return 1_000_000
    except Exception:
        logger.debug("autonomous.max_tokens_per_run read failed; using default", exc_info=True)
        return 1_000_000


def run_budget_cap(home: str | Path) -> int:
    """The run token budget for *home* (``autonomous.max_tokens_per_run``).

    Public reader for card rendering — the same source
    :func:`make_run_budget_check` enforces. Default 1,000,000; 0 or
    negative means the cap is disabled.
    """
    return _config_max_tokens_per_run(home)


def _config_mode_enabled(home: str | Path) -> bool:
    """Profile-wide ``platforms.telegram.autonomous_mode``; False when unset or unreadable.

    Same raw-config.yaml read as the settings UI readers (no merged-config
    dependency); the gateway reads it from the LAUNCH home, never the
    session home.
    """
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(home) / "config.yaml")
        platforms = config.get("platforms") if isinstance(config, dict) else None
        telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
        if not isinstance(telegram, dict):
            return False
        return bool(telegram.get("autonomous_mode", False))
    except Exception:
        logger.debug("autonomous_mode read failed; defaulting to off", exc_info=True)
        return False


def autonomous_mode_enabled(adapter: Any) -> bool:
    """Master switch for autonomous mode, adapter runtime override first.

    Mirrors ``streaming_response_enabled`` (``plugins/platforms/telegram/
    streaming_response.py``): a live adapter runtime override wins, then
    the adapter config's ``extra`` value, else the launch-home config.
    This answers "is autonomous set up / last enabled" — not "is a run
    live right now"; that is :func:`is_active`.
    """
    if adapter is not None:
        runtime = getattr(adapter, "_autonomous_mode_runtime", None)
        if runtime is not None:
            return bool(runtime)
        extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
        if "autonomous_mode" in extra:
            truthy = getattr(adapter, "_truthy_setting", None)
            if callable(truthy):
                return bool(truthy(extra.get("autonomous_mode"), default=False))
            return bool(extra.get("autonomous_mode", False))
    from hermes_constants import get_routing_process_hermes_home

    return _config_mode_enabled(get_routing_process_hermes_home())


def _config_badges_enabled(home: str | Path) -> bool:
    """Profile-wide ``platforms.telegram.autonomous_badges``; True when unset or unreadable.

    Same raw-config.yaml read as :func:`_config_mode_enabled`; the green
    ``autonomous`` tag rides every message while the mode is active unless
    the owner turns badges off.
    """
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(home) / "config.yaml")
        platforms = config.get("platforms") if isinstance(config, dict) else None
        telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
        if not isinstance(telegram, dict):
            return True
        return bool(telegram.get("autonomous_badges", True))
    except Exception:
        logger.debug("autonomous_badges read failed; defaulting to on", exc_info=True)
        return True


def autonomous_badges_enabled(adapter: Any) -> bool:
    """Badges switch for the green tag, adapter runtime override first.

    Mirrors :func:`autonomous_mode_enabled`: a live adapter runtime
    override wins, then the adapter config's ``extra`` value, else the
    launch-home config.
    """
    if adapter is not None:
        runtime = getattr(adapter, "_autonomous_badges_runtime", None)
        if runtime is not None:
            return bool(runtime)
        extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
        if "autonomous_badges" in extra:
            truthy = getattr(adapter, "_truthy_setting", None)
            if callable(truthy):
                return bool(truthy(extra.get("autonomous_badges"), default=True))
            return bool(extra.get("autonomous_badges", True))
    from hermes_constants import get_routing_process_hermes_home

    return _config_badges_enabled(get_routing_process_hermes_home())


def autonomous_tag_enabled(adapter: Any = None) -> bool:
    """True when a message of the mode should carry the green tag.

    Live flag on (launch home) AND badges on. Fails open to untagged on
    any read failure: the tag is an ornament, never a delivery gate.
    """
    try:
        from hermes_constants import get_routing_process_hermes_home

        return bool(is_active(get_routing_process_hermes_home())
                    and autonomous_badges_enabled(adapter))
    except Exception:  # health: allow BLE001 -- the tag gate fails open to untagged, never breaks delivery
        return False


#: Guest-thread prefix, same as ``guestbot.is_guest_thread`` (duplicated
#: here so this module keeps its no-plugin-imports invariant).
_GUEST_THREAD_PREFIX = "guest-"


def tag_enabled_for_delivery(adapter: Any, source: Any) -> bool:
    """Full tag gate for one delivery: Telegram DM, not a guest thread, tag on.

    Mirrors ``streaming_response_sink``: private 1-1 chats only (strict
    ``dm`` — the broader enable-handler set does not apply to delivery),
    guest threads get nothing. Any failure fails open to untagged.
    """
    try:
        if adapter is None or source is None:
            return False
        raw_platform = getattr(source, "platform", None)
        platform = str(getattr(raw_platform, "value", raw_platform) or "").lower()
        if platform != "telegram":
            return False
        if str(getattr(source, "chat_type", "")) != "dm":
            return False
        if str(getattr(source, "thread_id", "") or "").startswith(_GUEST_THREAD_PREFIX):
            return False
        return autonomous_tag_enabled(adapter)
    except Exception:  # health: allow BLE001 -- the tag gate fails open to untagged, never breaks delivery
        return False


def tag_metadata(adapter: Any, source: Any,
                 metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return *metadata* carrying the autonomous-tag flag when the gate passes.

    The final-delivery bracket calls this once per final; the Telegram
    adapter tags only the first part of the send. Untagged metadata is
    returned as-is; tagged metadata is a copy, never mutated in place.
    """
    if not tag_enabled_for_delivery(adapter, source):
        return metadata if metadata is not None else {}
    tagged = dict(metadata or {})
    tagged["telegram_autonomous_tag"] = True
    return tagged
