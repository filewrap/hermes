"""Human checkpoint gates for the web-designer add-on (audit 5.35/6.25).

Gates are exactly brief, wireframe, direction, preview (types.GATE_STAGES).
A gate pauses the run durably (``awaiting_checkpoint``) and resumes only
on an explicit human decision. A 30-minute timeout is a DENY: the run
pauses durably, never auto-approves, and never re-asks the same question.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from . import store
from .types import DEFAULT_BUDGETS, GATE_STAGES, STAGE_KEYS, STAGE_ORDER, RunRecord

__all__ = ["check_timeout", "record_decision", "request_checkpoint"]

_DECISIONS = ("approve", "approve_with_changes", "reject")
_PENDING_KEY = "pending_checkpoint"


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pending(record: RunRecord) -> Optional[dict[str, Any]]:
    pending = record.stage_data.get(_PENDING_KEY)
    return pending if isinstance(pending, dict) else None


def _next_incomplete(record: RunRecord) -> Optional[str]:
    for key in STAGE_KEYS:
        if key not in record.completed_stages:
            return key
    return None


def request_checkpoint(
    record: RunRecord, gate: str, *, payload: Optional[dict[str, Any]] = None,
) -> RunRecord:
    """Pause durably at *gate* and record the gate + payload + request
    time. Raises ValueError for an unknown gate or a run that is not
    running."""
    if gate not in GATE_STAGES:
        raise ValueError(f"unknown checkpoint gate {gate!r}; gates are "
                         f"{sorted(GATE_STAGES)}")
    if record.status != "running":
        raise ValueError(
            f"run {record.run_id} is {record.status!r}; only a running run "
            "can pause at a checkpoint")
    record.stage_data[_PENDING_KEY] = {
        "gate": gate, "payload": dict(payload or {}),
        "requested_at": _now_iso(), "stage": GATE_STAGES[gate],
    }
    store.save_run(record)
    store.append_audit(record, "checkpoint_requested", {"gate": gate})
    return store.transition(record, "awaiting_checkpoint",
                            reason=f"checkpoint gate {gate}")


def _reopen_stage(record: RunRecord, return_stage: str) -> None:
    """Drop the run back to *return_stage* so it can be re-run: stages
    from *return_stage* onward leave the completed list and lose their
    stage data."""
    if return_stage not in STAGE_ORDER:
        raise ValueError(f"unknown stage {return_stage!r}")
    order = STAGE_ORDER[return_stage]
    record.completed_stages = [
        s for s in record.completed_stages if STAGE_ORDER[s] < order]
    record.stage_data = {
        k: v for k, v in record.stage_data.items()
        if k not in STAGE_ORDER or STAGE_ORDER[k] < order}
    record.stage = return_stage


def record_decision(
    record: RunRecord,
    gate: str,
    decision: str,
    *,
    changes: Any = None,
    return_stage: Optional[str] = None,
) -> RunRecord:
    """Record the human decision for a pending gate.

    approve -> running, resume at the next incomplete stage.
    approve_with_changes -> the listed changes are required; resume at
    *return_stage* (default: the gate's own stage).
    reject -> return to *return_stage* (default: the gate's own stage)
    with the reason recorded, status running at that stage.
    Unknown gate/decision, or a decision with no pending request for the
    gate, raises ValueError."""
    if gate not in GATE_STAGES:
        raise ValueError(f"unknown checkpoint gate {gate!r}")
    if decision not in _DECISIONS:
        raise ValueError(
            f"unknown checkpoint decision {decision!r}; use {list(_DECISIONS)}")
    pending = _pending(record)
    if pending is None or pending.get("gate") != gate:
        raise ValueError(f"no checkpoint requested for gate {gate!r}")
    if record.status != "awaiting_checkpoint":
        raise ValueError(
            f"run {record.run_id} is {record.status!r}; no decision is pending")
    if decision == "approve_with_changes":
        if not changes:
            raise ValueError(
                "approve_with_changes needs the listed changes")
    target = return_stage or GATE_STAGES[gate]
    if target not in STAGE_ORDER:
        raise ValueError(f"unknown return stage {target!r}")
    entry: dict[str, Any] = {
        "gate": gate, "decision": decision, "decided_at": _now_iso(),
        "return_stage": target,
    }
    if changes is not None:
        entry["changes"] = changes
    if decision == "reject" and isinstance(changes, str):
        entry["reason"] = changes
    record.checkpoint_decisions.append(entry)
    del record.stage_data[_PENDING_KEY]
    if decision == "approve":
        record.stage = _next_incomplete(record) or record.stage
    else:
        _reopen_stage(record, target)
    store.save_run(record)
    store.append_audit(record, "checkpoint_decision",
                       {"gate": gate, "decision": decision,
                        "return_stage": target})
    return store.transition(record, "running",
                            reason=f"checkpoint {gate}: {decision}")


def check_timeout(
    record: RunRecord, gate: str, *, now: Any = None,
) -> bool:
    """True when a pending gate timed out (30 min, no decision): the
    timeout is recorded as a DENY, the run pauses durably, and the gate
    is never re-asked. False when nothing is pending or time remains."""
    pending = _pending(record)
    if pending is None or pending.get("gate") != gate:
        return False
    if record.status != "awaiting_checkpoint":
        return False
    requested = _parse_iso(pending.get("requested_at"))
    if requested is None:
        return False
    moment = _parse_iso(now) if now is not None else _now()
    if moment is None:
        return False
    elapsed = (moment - requested).total_seconds() / 60
    if elapsed < DEFAULT_BUDGETS.approval_timeout_minutes:
        return False
    record.checkpoint_decisions.append({
        "gate": gate, "decision": "timeout_deny", "decided_at": _now_iso(),
        "reason": "no decision within 30 minutes; timeout is a deny",
    })
    del record.stage_data[_PENDING_KEY]
    store.save_run(record)
    store.append_audit(record, "checkpoint_timeout",
                       {"gate": gate, "elapsed_minutes": round(elapsed, 1)})
    store.transition(record, "paused", reason=f"checkpoint {gate} timed out")
    return True
