#!/usr/bin/env python3
"""Run registry keyed by runId (F3 turn-control yield/wait).

One in-memory, process-local registry for delegated subagent runs, mirroring
The external agent's ``src/agents/subagents/registry/`` at the seam Hermes needs:

- ``register_run`` / ``settle_run`` — a run is registered when its child is
  built/dispatched and settled the moment ``_run_single_child`` returns its
  result entry, so ``agents_wait`` can join a single child without waiting
  for the whole unit to join.
- ``await_runs`` — event-driven subscribe/await over a ``threading.Condition``
  (``subscribeSubagentRunChanges`` in the external agent): waiters wake the moment a
  run settles, never by polling. Slices bound only the interrupt latency.
- Yield claims — the ``sessions_yield`` claim, with the same reserve/lookup
  shape as Hermes' own ``RunIdempotencyStore`` (``(scope, key)`` + fingerprint
  → created/reused): a second yield for the same runs reuses the claim
  instead of scheduling a second continuation. A yield for a *different*
  set extends the claim (a session may yield for a second batch while the
  first is still pending), where the run store would report a conflict.

Process-local by design: background delegation itself is process-local
(``tools/AGENTS.md``), and the live child registry (``_active_subagents``)
it extends is too. After a restart the registry is empty — ``agents_wait``
reports those runIds as ``not_found`` — while the durable async-delegation
row still delivers the completion as a new turn.

No new store: settlement entries are the ``_run_single_child`` result
entries, ownership reuses ``_owns_subagent_record`` (weakref chain, then
durable session lineage), and continuation delivery reuses the existing
async-delegation completion event.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
import weakref
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The external agent's agents-wait cap (src/agents/tools/agents-wait-tool.ts).
MAX_WAIT_IDS = 1_000
# The external agent's frozen-collector-result bound: a wait answer carries the
# complete recorded answer within this cap, never an unbounded summary.
_RESULT_TEXT_MAX_CHARS = 100_000
# Settled runs retained for late agents_wait reads; mirrors the
# _RECENT_SUBAGENTS_CAP bound in delegate_tool_registry. Running runs are
# never pruned.
_MAX_RETAINED_SETTLED = 200
# Bound on interrupt latency while parked in agents_wait: the condition
# wait wakes immediately on settle; the slice only re-checks the turn's
# interrupt flag.
_WAIT_INTERRUPT_SLICE_S = 0.5

RUNNING = "running"
# Hermes ``_run_single_child`` entry status -> external-agent agents_wait status.
_WAIT_STATUS = {
    "completed": "done", "failed": "failed", "error": "failed",
    "interrupted": "killed", "stalled": "timeout", "timeout": "timeout",
}

_records_lock = threading.Lock()
_records_cond = threading.Condition(_records_lock)
# runId -> record dict. ``_agent_ref`` is a weakref (or None): the record
# must never pin a finished child agent alive.
_records: dict[str, dict[str, Any]] = {}
# Owner session lineage tip -> yield claim {run_ids, fingerprint, message,
# acknowledgment, claimed_at}.
_yield_claims: dict[str, dict[str, Any]] = {}


def _agent_of(record: dict[str, Any]) -> Any:
    ref = record.get("_agent_ref")
    if ref is None:
        return None
    try:
        return ref()
    except Exception:  # health: allow BLE001 -- a dead weakref is "no agent", never an error
        return None


def _owns_run(record: dict[str, Any], parent_agent: Any) -> bool:
    """Ownership via the same spine delivery routes by (``_owns_subagent_record``):
    weakref chain while the parent agent survives, durable session lineage after
    a parent rebuild or compression rotation."""
    from tools.delegate_tool_registry import _owns_subagent_record

    probe = {"agent": _agent_of(record), "owner_agent_session_id": record.get("owner_agent_session_id")}
    return bool(_owns_subagent_record(probe, parent_agent))


def _session_key(parent_agent: Any) -> str:
    """Claim key: the lineage tip of the agent's session id, so a yield
    claimed before a compression rotation is still the same claim after it."""
    sid = str(getattr(parent_agent, "session_id", "") or "")
    if not sid:
        return sid
    try:
        from tools.delegate_tool_registry import _resolve_session_lineage
        return _resolve_session_lineage(sid, parent_agent)
    except Exception:  # health: allow BLE001 -- lineage is best-effort; the raw id still keys the claim
        logger.debug("subagent run registry: lineage resolve failed for claim key", exc_info=True)
        return sid


def _prune_settled_locked() -> None:
    """Drop oldest settled records past the retention bound (running kept)."""
    settled = [rid for rid, r in _records.items() if r.get("status") != RUNNING]
    excess = len(settled) - _MAX_RETAINED_SETTLED
    for rid in settled[:excess] if excess > 0 else ():
        _records.pop(rid, None)


def register_run(
    run_id: str, *, goal: Optional[str] = None, model: Optional[str] = None,
    owner_agent_session_id: Optional[str] = None, owner_session_id: Optional[str] = None,
    delegation_id: Optional[str] = None, unit_id: Optional[str] = None,
    group: Optional[str] = None, child_session_id: Optional[str] = None, agent: Any = None,
) -> Optional[dict[str, Any]]:
    """Upsert a run as running (dispatch-time early registration, then the
    live-child registration fills the agent ref). Never overwrites a settled
    record: a late register for a finished run is a no-op."""
    if not run_id or not isinstance(run_id, str):
        return None
    with _records_cond:
        record = _records.get(run_id)
        if record is not None and record.get("status") != RUNNING:
            return dict(record)
        if record is None:
            record = {
                "run_id": run_id, "status": RUNNING, "goal": None, "model": None,
                "owner_agent_session_id": None, "owner_session_id": None,
                "delegation_id": None, "unit_id": None, "group": None,
                "child_session_id": None, "started_at": time.time(), "settled_at": None,
                "entry": None, "error": None, "_agent_ref": None,
            }
            _records[run_id] = record
        for key, value in (
            ("goal", goal), ("model", model), ("owner_agent_session_id", owner_agent_session_id),
            ("owner_session_id", owner_session_id), ("delegation_id", delegation_id),
            ("unit_id", unit_id), ("group", group), ("child_session_id", child_session_id),
        ):
            if value is not None and record.get(key) is None:
                record[key] = value
        if agent is not None:
            try:
                record["_agent_ref"] = weakref.ref(agent)
            except TypeError:
                record["_agent_ref"] = None  # non-weakref-able test doubles
        return dict(record)


def settle_run(run_id: str, entry: Optional[dict[str, Any]], *, error: Optional[str] = None) -> bool:
    """Settle a run with its ``_run_single_child`` result entry. First wins:
    a stall settlement is not overwritten by the wedged worker's late return
    (the async machinery already dropped that late result the same way).
    Upserts a minimal record when registration never ran, so the settle is
    still visible; ownership of such a record fails closed in the wait."""
    if not run_id or not isinstance(run_id, str):
        return False
    entry = entry if isinstance(entry, dict) else {}
    status = str(entry.get("status") or "failed")
    with _records_cond:
        record = _records.get(run_id)
        if record is None:
            record = {
                "run_id": run_id, "status": RUNNING, "goal": None, "model": None,
                "owner_agent_session_id": None, "owner_session_id": None,
                "delegation_id": None, "unit_id": None, "group": None,
                "child_session_id": None, "started_at": time.time(), "settled_at": None,
                "entry": None, "error": None, "_agent_ref": None,
            }
            _records[run_id] = record
        if record.get("status") != RUNNING:
            return False
        record["status"] = status
        record["settled_at"] = time.time()
        record["entry"] = dict(entry)
        record["error"] = error if error is not None else entry.get("error")
        _prune_settled_locked()
        _records_cond.notify_all()
        return True


def settle_unit_runs(unit_id: str, *, status: str = "stalled", error: Optional[str] = None) -> list[str]:
    """Settle every still-running run of one async unit (the stale monitor's
    force-finalize path: the wedged worker never returns a per-child entry).
    Runs already settled keep their real entry. Returns the settled runIds."""
    settled: list[str] = []
    if not unit_id:
        return settled
    with _records_cond:
        for run_id, record in list(_records.items()):
            if record.get("unit_id") != unit_id or record.get("status") != RUNNING:
                continue
            record["status"] = status
            record["settled_at"] = time.time()
            record["entry"] = {"status": status, "summary": "", "error": error or ""}
            record["error"] = error
            settled.append(run_id)
        if settled:
            _prune_settled_locked()
            _records_cond.notify_all()
    return settled


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """Copy of one run record (entry included), or None."""
    if not run_id:
        return None
    with _records_cond:
        record = _records.get(run_id)
        return dict(record) if record is not None else None


def _live_subagent_pending(parent_agent: Any) -> dict[str, dict[str, Any]]:
    """Owned live children (``_active_subagents``) with no registry record
    yet — the dispatch-to-registration window. Keyed by subagent_id."""
    try:
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, _owns_subagent_record
    except Exception:  # health: allow BLE001 -- without the live registry there is nothing extra to claim
        logger.debug("subagent run registry: live subagent registry unavailable", exc_info=True)
        return {}
    with _active_subagents_lock:
        live = list(_active_subagents.values())
    pending: dict[str, dict[str, Any]] = {}
    for record in live:
        sid = record.get("subagent_id")
        if not sid or sid in _records:
            continue
        try:
            owned = _owns_subagent_record(record, parent_agent)
        except Exception:  # health: allow BLE001 -- an uncheckable record is not claimable
            logger.debug("subagent run registry: ownership check failed for %s", sid, exc_info=True)
            continue
        if owned:
            pending[sid] = {
                "run_id": sid, "goal": record.get("goal"), "model": record.get("model"),
                "delegation_id": record.get("delegation_id"),
                "owner_agent_session_id": record.get("owner_agent_session_id"),
            }
    return pending


def pending_runs_for_session(parent_agent: Any) -> list[dict[str, Any]]:
    """Owned still-running runs: registry runs plus live children whose
    registration has not landed yet. Each entry carries ``run_id`` plus the
    unit/delegation ids the yield claim attaches its message to."""
    with _records_cond:
        owned = [
            {"run_id": r["run_id"], "goal": r.get("goal"), "model": r.get("model"),
             "delegation_id": r.get("delegation_id"), "unit_id": r.get("unit_id"),
             "group": r.get("group"), "state": "running",
             "owner_agent_session_id": r.get("owner_agent_session_id")}
            for r in _records.values() if r.get("status") == RUNNING
        ]
        pending = {entry["run_id"]: entry for entry in owned if _owns_run(
            _records.get(entry["run_id"]) or {}, parent_agent)}
    for run_id, extra in _live_subagent_pending(parent_agent).items():
        pending.setdefault(run_id, {**extra, "unit_id": None, "group": None, "state": "running"})
    return sorted(pending.values(), key=lambda e: e["run_id"])


def _completion_for(record: dict[str, Any]) -> dict[str, Any]:
    """One settled run as an external-agent ``agents_wait`` completion entry."""
    entry = record.get("entry") or {}
    status = _WAIT_STATUS.get(str(record.get("status") or ""), "failed")
    summary = entry.get("summary") if isinstance(entry.get("summary"), str) else ""
    truncated = False
    if len(summary) > _RESULT_TEXT_MAX_CHARS:
        summary = summary[:_RESULT_TEXT_MAX_CHARS]
        truncated = True
    started, settled = record.get("started_at"), record.get("settled_at")
    runtime_ms: Optional[float] = None
    # The entry's measured duration is authoritative (it excludes dispatch
    # queueing); registry wall time is the fallback for fabricated entries.
    if isinstance(entry.get("duration_seconds"), (int, float)):
        runtime_ms = round(float(entry["duration_seconds"]) * 1000, 1)
    elif isinstance(started, (int, float)) and isinstance(settled, (int, float)) and settled >= started:
        runtime_ms = round((settled - started) * 1000, 1)
    out: dict[str, Any] = {
        "runId": record["run_id"], "status": status, "result": summary,
        "sessionKey": str(record.get("child_session_id") or ""),
    }
    if truncated:
        out["resultTruncated"] = True
    if record.get("goal"):
        out["label"] = str(record["goal"])[:200]
    if record.get("group"):
        out["groupId"] = str(record["group"])
    if runtime_ms is not None:
        out["runtimeMs"] = runtime_ms
    if record.get("error"):
        out["error"] = str(record["error"])
    schema_errors = entry.get("schema_errors")
    if entry.get("schema_valid") is False and isinstance(schema_errors, list) and schema_errors:
        out["schemaError"] = str(schema_errors[0])
    return out


def _read_wait_state_locked(parent_agent: Any, run_ids: list[str]) -> dict[str, Any]:
    """``{completed, pending, errors}`` for *run_ids* from the current
    records (caller holds the condition lock). Unknown ids that are live in
    ``_active_subagents`` and owned are pending — their settlement upserts
    the record; unknown and not live is ``not_found``."""
    completed: list[dict[str, Any]] = []
    pending: list[str] = []
    errors: list[dict[str, str]] = []
    live_pending = _live_subagent_pending(parent_agent)
    for input_index, run_id in enumerate(run_ids):
        record = _records.get(run_id)
        if record is None:
            if run_id in live_pending:
                pending.append(run_id)
            else:
                errors.append({"runId": run_id, "error": "not_found"})
            continue
        if not _owns_run(record, parent_agent):
            errors.append({"runId": run_id, "error": "not_owner"})
            continue
        if record.get("status") == RUNNING:
            pending.append(run_id)
        else:
            # The external agent order: completion time, then input order.
            completed.append((
                (record.get("settled_at") or 0, input_index),
                _completion_for(record),
            ))
    completed.sort(key=lambda pair: pair[0])
    return {"completed": [entry for _, entry in completed], "pending": pending, "errors": errors}


def await_runs(
    parent_agent: Any, run_ids: list[str], *, timeout_seconds: Optional[float] = None,
    await_results: bool = False,
) -> dict[str, Any]:
    """Subscribe/await settlement for *run_ids* without polling: the
    condition wakes the moment any run settles. Returns when any run
    completes (unless ``await_results``, which joins all), when nothing is
    pending, at the deadline, or when the turn is interrupted. The caller
    supplies the default timeout (the external agent: 30s); ``await_results`` has no
    deadline — the runs settle through their own child timeouts, and the
    stale monitor force-finalizes a wedged unit as ``stalled``."""
    deadline = None if await_results or timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
    with _records_cond:
        while True:
            state = _read_wait_state_locked(parent_agent, run_ids)
            if (
                (not await_results and state["completed"])
                or not state["pending"]
                or (deadline is not None and time.monotonic() >= deadline)
            ):
                return state
            remaining = None if deadline is None else deadline - time.monotonic()
            _records_cond.wait(timeout=min(remaining, _WAIT_INTERRUPT_SLICE_S) if remaining is not None
                               else _WAIT_INTERRUPT_SLICE_S)
            if getattr(parent_agent, "_interrupt_requested", False):
                return _read_wait_state_locked(parent_agent, run_ids)


# ── Yield claims (RunIdempotencyStore shape: scope + key + fingerprint) ──

def _fingerprint(run_ids: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(run_ids)).encode("utf-8")).hexdigest()


def _claim_pending_locked(claim: dict[str, Any]) -> list[str]:
    """RunIds of a claim still unsettled (registry running, or live)."""
    pending: list[str] = []
    for run_id in claim.get("run_ids") or ():
        record = _records.get(run_id)
        if record is not None and record.get("status") == RUNNING:
            pending.append(run_id)
    return pending


def claim_yield(
    session_id: str, run_ids: list[str], *, message: Optional[str] = None,
    acknowledgment: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Reserve the yield claim for this session: ``(outcome, claim)`` with
    outcome ``created`` (new claim), ``reused`` (same runs already claimed),
    or ``extended`` (new runs joined a live claim). An inert claim (all its
    runs settled) is treated as absent. The fingerprint compare uses
    ``hmac.compare_digest``, the ``RunIdempotencyStore`` shape."""
    key = str(session_id or "")
    fingerprint = _fingerprint(run_ids)
    with _records_cond:
        claim = _yield_claims.get(key)
        if claim is not None and not _claim_pending_locked(claim):
            _yield_claims.pop(key, None)
            claim = None
        if claim is not None and hmac.compare_digest(str(claim.get("fingerprint") or ""), fingerprint):
            return "reused", dict(claim)
        if claim is not None:
            merged = sorted(set(claim.get("run_ids") or ()) | set(run_ids))
            claim = {
                "run_ids": merged, "fingerprint": _fingerprint(merged),
                "message": message, "acknowledgment": acknowledgment, "claimed_at": time.time(),
            }
            _yield_claims[key] = claim
            return "extended", dict(claim)
        claim = {
            "run_ids": sorted(set(run_ids)), "fingerprint": fingerprint,
            "message": message, "acknowledgment": acknowledgment, "claimed_at": time.time(),
        }
        _yield_claims[key] = claim
        return "created", dict(claim)


def pending_yielded_runs(session_id: str) -> list[str]:
    """RunIds this session yielded for that are still unsettled (live claim
    only; settled claims are inert and pruned)."""
    with _records_cond:
        claim = _yield_claims.get(str(session_id or ""))
        if claim is None:
            return []
        pending = _claim_pending_locked(claim)
        if not pending:
            _yield_claims.pop(str(session_id or ""), None)
        return pending


# ── Yield turn flag (consumed by the agent loop) ──────────────────────────

def request_yield(agent: Any, acknowledgment: str, run_ids: list[str], message: Optional[str] = None) -> None:
    """Arm the deterministic turn end: the tool round ends this turn with
    the acknowledgment as the final response once the current tool batch
    finishes. The flag is per-turn state; the loop clears a stale one at
    turn start, so a host that never consumes it cannot end a later turn."""
    agent._yield_requested = {
        "acknowledgment": acknowledgment, "run_ids": list(run_ids), "message": message,
    }


def consume_yield_request(agent: Any) -> Optional[str]:
    """Pop the armed yield, returning its acknowledgment (None when no yield
    was requested this turn)."""
    pending = getattr(agent, "_yield_requested", None)
    agent._yield_requested = None
    if not isinstance(pending, dict):
        return None
    ack = pending.get("acknowledgment")
    return str(ack) if isinstance(ack, str) and ack.strip() else None


# ── Message waits (F4: sessions_yield waitFor "message" + sessions_send) ──

# A delegate child has no turn-resume machinery (run_conversation is
# one-shot), so its waitFor-"message" yield parks inside the tool call on
# one of these waiters, and sessions_send delivers into it. A main agent's
# yield ends the turn instead, so its wait is only a claim (below) that
# sessions_send consumes when it resumes the session as a new turn.
_MESSAGE_WAIT_SLICE_S = 0.5
_SESSION_WAIT_TTL_S = 24 * 3600

_message_waits_lock = threading.Lock()
# child session id -> waiter {event, message, parked_at}
_message_waits: dict[str, dict[str, Any]] = {}
# session lineage tip -> claim {message, acknowledgment, claimed_at}
_session_waits: dict[str, dict[str, Any]] = {}


def find_run_by_child_session(child_session_id: str) -> Optional[dict[str, Any]]:
    """Registry record for a child session id, or None."""
    if not child_session_id:
        return None
    with _records_cond:
        for record in _records.values():
            if record.get("child_session_id") == child_session_id:
                return dict(record)
    return None


def park_message_wait(session_id: str) -> dict[str, Any]:
    """Park (or reuse) the message waiter for one child session id."""
    key = str(session_id or "")
    with _message_waits_lock:
        waiter = _message_waits.get(key)
        if waiter is None:
            waiter = {"session_id": key, "event": threading.Event(),
                      "message": None, "parked_at": time.time()}
            _message_waits[key] = waiter
        return waiter


def deliver_message(session_id: str, message: str) -> bool:
    """Deliver a message to a parked waiter; False when nobody is waiting."""
    with _message_waits_lock:
        waiter = _message_waits.get(str(session_id or ""))
        if waiter is None or waiter["event"].is_set():
            return False
        waiter["message"] = str(message)
        waiter["event"].set()
        return True


def cancel_message_wait(session_id: str) -> None:
    """Drop the parked waiter for one child session id, if any."""
    with _message_waits_lock:
        _message_waits.pop(str(session_id or ""), None)


def await_message_wait(
    parent_agent: Any, session_id: str, *, on_slice: Any = None,
) -> tuple[str, Optional[str]]:
    """Park on the session's message waiter until delivery or interrupt.

    Returns ``("delivered", message)`` or ``("interrupted", None)``. The
    wait parks in slices so the caller can touch activity (a parked child
    must not look stale to the delegation heartbeat) and notice an
    interrupt; it is bounded by the delegation child timeout, never by a
    deadline of its own. The waiter is reaped here, whichever way out.
    """
    waiter = park_message_wait(session_id)
    try:
        while True:
            if waiter["event"].is_set():
                return "delivered", waiter.get("message")
            if bool(getattr(parent_agent, "_interrupt_requested", False)):
                return "interrupted", None
            waiter["event"].wait(timeout=_MESSAGE_WAIT_SLICE_S)
            if on_slice is not None:
                try:
                    on_slice()
                except Exception:  # health: allow BLE001 -- a failed activity touch never ends the wait
                    logger.debug("message wait: slice callback failed", exc_info=True)
    finally:
        cancel_message_wait(session_id)


def _prune_session_waits_locked(now: float) -> None:
    for key, claim in list(_session_waits.items()):
        if now - float(claim.get("claimed_at") or 0) > _SESSION_WAIT_TTL_S:
            _session_waits.pop(key, None)


def claim_session_wait(
    session_id: str, *, message: Optional[str] = None, acknowledgment: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Claim that this session waits for a message: ``(outcome, claim)``
    with outcome ``created`` or ``reused`` (a live claim for the same
    session). A second yield for a waiting session reuses the claim
    instead of re-arming the wait."""
    key = str(session_id or "")
    with _message_waits_lock:
        _prune_session_waits_locked(time.time())
        claim = _session_waits.get(key)
        if claim is not None:
            return "reused", dict(claim)
        claim = {"message": message, "acknowledgment": acknowledgment, "claimed_at": time.time()}
        _session_waits[key] = claim
        return "created", dict(claim)


def consume_session_wait(session_id: str) -> Optional[dict[str, Any]]:
    """Pop the session-wait claim (sessions_send delivery), or None."""
    with _message_waits_lock:
        claim = _session_waits.pop(str(session_id or ""), None)
        return dict(claim) if claim is not None else None


def is_session_waiting(session_id: str) -> bool:
    """True when a live session-wait claim exists for this session."""
    with _message_waits_lock:
        _prune_session_waits_locked(time.time())
        return str(session_id or "") in _session_waits
