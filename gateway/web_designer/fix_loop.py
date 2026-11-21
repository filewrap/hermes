"""Capped fix -> re-judge loop (audit 6.24, boundaries 5.2-5.4).

Blockers first, then defects, then changes; observations never block.
Per issue at most 3 fix -> re-judge cycles; after the third failure the
issue is parked as unresolved and escalated with the best passing
version. A fix that introduces a new blocker rolls back to the last
passing checkpoint first. The loop never runs indefinitely and never
silently drops a blocker.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import boundaries, machine
from . import store
from .types import DEFAULT_BUDGETS, Finding, RunRecord, Verdict

logger = logging.getLogger(__name__)

__all__ = ["run_fix_loop"]

_FIXABLE = ("blocker", "defect", "change")
_SEVERITY_ORDER = {"blocker": 0, "defect": 1, "change": 2}


def _as_verdicts(value: Any) -> list[Verdict]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if isinstance(item, Verdict):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(Verdict.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _issue_key(finding: Finding) -> str:
    return f"{finding.rule_id}@{finding.location}"


def _open_findings(verdicts: list[Verdict]) -> list[Finding]:
    found = []
    for verdict in verdicts:
        found.extend(f for f in verdict.findings if f.status == "open")
    found.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3),
                              f.rule_id, f.location))
    return found


def _new_blockers(before: list[Finding], after: list[Verdict]) -> list[Finding]:
    before_keys = {_issue_key(f) for f in before}
    fresh = []
    for verdict in after:
        for finding in verdict.findings:
            if (finding.severity == "blocker" and finding.status == "open"
                    and _issue_key(finding) not in before_keys):
                fresh.append(finding)
    return fresh


def _still_open(after: list[Verdict], finding: Finding) -> bool:
    key = _issue_key(finding)
    return any(_issue_key(f) == key and f.status == "open"
               for v in after for f in v.findings)


def _best_version(record: RunRecord, open_count: int, open_blockers: int) -> dict[str, Any]:
    return {"run_id": record.run_id, "stage": record.stage,
            "open_findings": open_count, "open_blockers": open_blockers,
            "completed_stages": list(record.completed_stages)}


def _is_better(candidate: dict[str, Any], best: Optional[dict[str, Any]]) -> bool:
    if best is None:
        return True
    return (candidate["open_blockers"], candidate["open_findings"]) < (
        best["open_blockers"], best["open_findings"])


def _fix_changed(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("changed", result.get("files_changed")))
    return False


def run_fix_loop(
    record: RunRecord,
    verdicts: list[Verdict | dict[str, Any]],
    fix_fn: Callable[[RunRecord, dict[str, Any]], Any],
    *,
    rejudge_fn: Callable[[RunRecord, dict[str, Any]], Any],
) -> dict[str, Any]:
    """Run the capped fix loop over *verdicts*.

    ``fix_fn(record, finding_dict)`` applies one fix and reports whether
    anything changed (bool, or a dict with ``changed`` / ``files_changed``).
    ``rejudge_fn(record, finding_dict)`` re-runs the affected judgers plus
    visual and returns verdict(s). Returns ``{"escalated": [...],
    "best_version": {...}, "fixed": [...], "parked": [...]}`` — and
    ``"halted": "no_progress"`` when the no-progress breaker fires.
    """
    parsed = _as_verdicts(verdicts)
    issues = [f for f in _open_findings(parsed) if f.severity in _FIXABLE]
    fixed: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    best: Optional[dict[str, Any]] = None
    no_progress_streak = 0
    halted: Optional[str] = None

    for finding in issues:
        if halted is not None:
            parked.append({**finding.to_dict(), "status": "parked"})
            continue
        key = _issue_key(finding)
        open_before = _open_findings(parsed)
        while True:
            try:
                boundaries.check_fix_loop_cap(record, key)
            except boundaries.BoundaryViolation:
                break
            try:
                changed = _fix_changed(fix_fn(record, finding.to_dict()))
            except Exception:
                # Deliberate catch-all: fix_fn is caller-supplied, any
                # failure counts as a spent attempt, never a crash.
                logger.exception("web-designer fix failed for %s", key)
                changed = False
                store.append_audit(record, "fix_failed",
                                   {"issue": key, "error": "fix_fn raised"})
            record.fix_attempts[key] = record.fix_attempts.get(key, 0) + 1
            store.save_run(record)
            rejudge_ok = True
            try:
                after = _as_verdicts(rejudge_fn(record, finding.to_dict()))
            except Exception:
                # Deliberate catch-all: rejudge_fn is caller-supplied; a
                # failed re-judge proves nothing and spends the attempt.
                logger.exception("web-designer re-judge failed for %s", key)
                rejudge_ok = False
                after = []
                store.append_audit(record, "rejudge_failed",
                                   {"issue": key, "error": "rejudge_fn raised"})
            if not rejudge_ok:
                # A failed re-judge proves nothing: the attempt is spent
                # but the issue is neither fixed nor newly passing.
                continue
            fresh_blockers = _new_blockers(open_before, after)
            if fresh_blockers:
                last_good = (record.completed_stages[-1]
                             if record.completed_stages else None)
                if last_good is not None and last_good != "fix_loop":
                    machine.rollback(record, last_good)
                store.append_audit(record, "fix_rollback",
                                   {"issue": key, "new_blockers": len(fresh_blockers)})
            newly_passing = len(open_before) - sum(
                1 for v in after for f in v.findings if f.status == "open")
            if changed or newly_passing > 0:
                no_progress_streak = 0
            else:
                no_progress_streak += 1
                if no_progress_streak >= DEFAULT_BUDGETS.no_progress_cap:
                    halted = "no_progress"
                    store.append_audit(record, "fix_loop_halted",
                                       {"reason": "no_progress", "issue": key})
                    break
            open_after = [f for v in after for f in v.findings if f.status == "open"]
            candidate = _best_version(record, len(open_after),
                                      sum(1 for f in open_after
                                          if f.severity == "blocker"))
            if _is_better(candidate, best):
                best = candidate
            if not _still_open(after, finding):
                finding.status = "fixed"
                fixed.append(finding.to_dict())
                break
            open_before = open_after
        if finding.status != "fixed":
            finding.status = "parked"
            parked.append(finding.to_dict())
            store.append_audit(record, "issue_parked",
                               {"issue": key,
                                "attempts": record.fix_attempts.get(key, 0)})

    if best is None:
        open_now = _open_findings(parsed)
        best = _best_version(record, len(open_now),
                             sum(1 for f in open_now if f.severity == "blocker"))
    result: dict[str, Any] = {
        "escalated": parked,
        "best_version": best,
        "fixed": fixed,
        "parked": parked,
    }
    if halted is not None:
        result["halted"] = halted
    return result
