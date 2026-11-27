"""The 30-stage web-designer state machine (audit 6.1-6.30).

Explicit, ordered, checkpointed after every stage: a stage completes
only when its record matches its contract, and a stage with no record
refuses cleanly instead of faking a pass. Stages 27/28/30 are
machine-level — the machine generates their data, it does not ask an
agent to perform them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from . import store
from .types import STAGE_KEYS, STAGE_ORDER, RunRecord, Verdict

__all__ = [
    "STAGE_RECORDS",
    "MachineError",
    "StageRefused",
    "advance",
    "fail_run",
    "resume",
    "rollback",
    "stop_run",
]


class MachineError(Exception):
    """Out-of-order advance, unknown stage, or a run that cannot move."""


class StageRefused(MachineError):
    """A stage record failed its contract; the exact missing requirement
    is in the message. The stage does not complete."""


#: Stage -> required record keys. Machine-level stages (27/28/30) take no
#: caller record: the machine generates their data.
STAGE_RECORDS: dict[str, tuple[str, ...]] = {
    "load_gate": ("decision",),
    "intake": ("purpose", "audience", "primary_action"),
    "brief": ("problem", "audience", "primary_action", "success_measure",
              "tone", "non_goals", "intent"),
    "scope_freeze": ("pages", "features", "out_of_scope", "delivery_form"),
    "research": ("note", "sources"),
    "ia": ("sitemap", "navigation", "flows"),
    "wireframe": ("pages",),
    "copy": ("pages", "meta"),
    "direction": ("directions", "chosen"),
    "font": ("families", "scale"),
    "colour": ("roles", "contrast_table"),
    "theme": ("default", "surfaces"),
    "icon": ("icon_set",),
    "tokens": ("tokens_path",),
    "components": ("inventory",),
    "stack": ("stack", "files", "commands"),
    "build": ("files", "build_log", "token_drift"),
    "preview": ("url_or_path", "screenshots", "console_log"),
    "judge_visual": ("verdict",),
    "judge_a11y": ("verdict",),
    "judge_perf": ("verdict",),
    "judge_content": ("verdict",),
    "judge_responsive": ("verdict",),
    "fix_loop": ("iterations", "remaining"),
    "checkpoints": ("decisions",),
    "delivery": ("package", "verdict_summary"),
    "checkpointing": (),
    "rollback": (),
    "revisions": ("classification",),
    "routing_audit": (),
}

_MACHINE_STAGES = ("checkpointing", "rollback", "routing_audit")
_JUDGE_STAGES = {
    "judge_visual": "visual", "judge_a11y": "a11y",
    "judge_perf": "performance", "judge_content": "content",
    "judge_responsive": "responsive",
}


def _refuse(stage: str, requirement: str) -> None:
    raise StageRefused(f"stage {stage!r} refused: {requirement}")


def _require_keys(stage: str, data: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in data or data[k] is None]
    if missing:
        _refuse(stage, f"missing required keys: {', '.join(missing)}")


def _blank(stage: str, data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        _refuse(stage, f"{key!r} must be a non-empty string")


def _non_empty_list(stage: str, data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        _refuse(stage, f"{key!r} must be a non-empty list")


def _present_list(stage: str, data: dict[str, Any], key: str) -> None:
    if not isinstance(data.get(key), list):
        _refuse(stage, f"{key!r} must be a list")


def _next_incomplete(record: RunRecord) -> Optional[str]:
    for key in STAGE_KEYS:
        if key not in record.completed_stages:
            return key
    return None


def _check_wireframe(stage: str, data: dict[str, Any]) -> None:
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        _refuse(stage, "'pages' must be a non-empty list")
    for page in pages:
        sections = page.get("sections") if isinstance(page, dict) else None
        if not isinstance(sections, list) or not sections:
            _refuse(stage, "every wireframe page needs a non-empty 'sections' list")


def _scan_verify_flags(value: Any) -> bool:
    if isinstance(value, str):
        return "[verify]" in value
    if isinstance(value, dict):
        return any(_scan_verify_flags(v) for v in value.values())
    if isinstance(value, list):
        return any(_scan_verify_flags(v) for v in value)
    return False


def _contrast_ok(entry: Any) -> bool:
    if isinstance(entry, (int, float)):
        return entry >= 4.5
    if not isinstance(entry, dict):
        return False
    if "aa" in entry:
        return bool(entry["aa"])
    if "passes_aa" in entry:
        return bool(entry["passes_aa"])
    ratio = entry.get("ratio")
    if not isinstance(ratio, (int, float)):
        return False
    threshold = 3.0 if entry.get("large") else 4.5
    return ratio >= threshold


def _check_colour(stage: str, data: dict[str, Any]) -> None:
    table = data.get("contrast_table")
    pairs = table.values() if isinstance(table, dict) else table
    if not isinstance(pairs, (list, dict)) or not pairs:
        _refuse(stage, "'contrast_table' must list every text/background pair")
    entries = list(pairs.values()) if isinstance(pairs, dict) else list(pairs)
    if not entries or not all(_contrast_ok(e) for e in entries):
        _refuse(stage, "every contrast pair must meet WCAG 2.2 AA")


def _check_direction(stage: str, data: dict[str, Any]) -> None:
    directions = data.get("directions")
    if not isinstance(directions, list) or not 2 <= len(directions) <= 3:
        _refuse(stage, "'directions' must list 2-3 directions")
    chosen = data.get("chosen")
    if isinstance(chosen, list):
        _refuse(stage, "'chosen' must name exactly one direction")
    names = [d.get("name") if isinstance(d, dict) else d for d in directions]
    if chosen not in names:
        _refuse(stage, "'chosen' must be one of the explored directions")


def _check_preview(stage: str, data: dict[str, Any]) -> None:
    shots = data.get("screenshots")
    if not isinstance(shots, dict):
        _refuse(stage, "'screenshots' must map viewports to captures")
    for width in ("1440", "768", "375"):
        entry = shots.get(width, shots.get(int(width)))
        if isinstance(entry, str) and entry.strip():
            continue
        if (isinstance(entry, dict) and isinstance(entry.get("unavailable"), str)
                and entry["unavailable"].strip()):
            continue
        _refuse(stage, f"screenshots need a 1440/768/375 entry for {width}")


def _check_judge(stage: str, data: dict[str, Any]) -> None:
    verdict = data.get("verdict")
    if not isinstance(verdict, dict):
        _refuse(stage, "'verdict' must be a verdict dict")
    try:
        parsed = Verdict.from_dict(verdict)
    except (KeyError, TypeError, ValueError) as exc:
        _refuse(stage, f"'verdict' is malformed: {exc}")
    if parsed.judger != _JUDGE_STAGES[stage]:
        _refuse(stage, f"verdict judger must be {_JUDGE_STAGES[stage]!r}")


def _validate_tokens_file(stage: str, path_str: str) -> None:
    path = Path(path_str)
    if not path.is_file():
        _refuse(stage, f"tokens file does not exist: {path_str}")
    try:
        tokens = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(stage, f"tokens file is not valid JSON: {exc}")
    if not isinstance(tokens, dict) or not tokens:
        _refuse(stage, "tokens file must be a non-empty JSON object")
    try:
        from . import tokens as tokens_mod
    except ImportError:
        return  # tokens worker not landed yet; JSON validity is the check
    for name in ("validate_tokens_file", "validate_tokens", "validate"):
        validator = getattr(tokens_mod, name, None)
        if callable(validator):
            try:
                validator(path_str if "file" in name else tokens)
            except (ValueError, TypeError, KeyError) as exc:
                _refuse(stage, f"tokens module rejected the file: {exc}")
            return
    loader = getattr(tokens_mod, "load_tokens", None)
    if callable(loader):
        try:
            loader(path_str)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            _refuse(stage, f"tokens module could not load the file: {exc}")


def _check_tokens(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    path_str = data.get("tokens_path")
    if not isinstance(path_str, str) or not path_str.strip():
        _refuse(stage, "'tokens_path' must name the tokens file")
    _validate_tokens_file(stage, path_str)
    record.tokens_path = path_str


def _v_intake(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    for key in ("purpose", "audience", "primary_action"):
        _blank(stage, data, key)


def _v_brief(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    for key in ("problem", "audience", "primary_action",
                "success_measure", "tone", "intent"):
        _blank(stage, data, key)
    _present_list(stage, data, "non_goals")


def _v_scope_freeze(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _non_empty_list(stage, data, "pages")
    _present_list(stage, data, "features")
    _present_list(stage, data, "out_of_scope")
    _blank(stage, data, "delivery_form")


def _v_research(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _blank(stage, data, "note")
    _non_empty_list(stage, data, "sources")


def _v_ia(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    for key in ("sitemap", "navigation", "flows"):
        if not data.get(key):
            _refuse(stage, f"{key!r} must be present and non-empty")


def _v_wireframe(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _check_wireframe(stage, data)


def _v_copy(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _non_empty_list(stage, data, "pages")
    if not isinstance(data.get("meta"), dict) or not data["meta"]:
        _refuse(stage, "'meta' must be a non-empty mapping")
    if _scan_verify_flags(data):
        _refuse(stage, "unresolved '[verify]' flags block completion")


def _v_direction(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _check_direction(stage, data)


def _v_font(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    families = data.get("families")
    if not isinstance(families, list) or not 1 <= len(families) <= 2:
        _refuse(stage, "'families' must list 1-2 typefaces")
    if not data.get("scale"):
        _refuse(stage, "'scale' must be present")


def _v_colour(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if not data.get("roles"):
        _refuse(stage, "'roles' must be present")
    _check_colour(stage, data)


def _v_theme(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if data.get("default") not in ("light", "dark"):
        _refuse(stage, "'default' must be 'light' or 'dark'")
    if not data.get("surfaces"):
        _refuse(stage, "'surfaces' must be present")


def _v_icon(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _blank(stage, data, "icon_set")


def _v_tokens(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _check_tokens(stage, data, record)


def _v_components(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if not data.get("inventory"):
        _refuse(stage, "'inventory' must be present and non-empty")


def _v_stack(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    for key in ("stack", "files", "commands"):
        if not data.get(key):
            _refuse(stage, f"{key!r} must be present and non-empty")


def _v_build(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if not data.get("files"):
        _refuse(stage, "'files' must be present and non-empty")
    if "build_log" not in data:
        _refuse(stage, "'build_log' must be present")
    if data.get("token_drift"):
        _refuse(stage, "'token_drift' must be empty — every value tokenised")


def _v_preview(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _blank(stage, data, "url_or_path")
    _check_preview(stage, data)
    if "console_log" not in data:
        _refuse(stage, "'console_log' must be present")


def _v_judge(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _check_judge(stage, data)


def _v_fix_loop(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if not isinstance(data.get("iterations"), int):
        _refuse(stage, "'iterations' must be an integer")
    _present_list(stage, data, "remaining")


def _v_checkpoints(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _non_empty_list(stage, data, "decisions")


def _v_delivery(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    package = data.get("package")
    if not isinstance(package, dict) or "manifest" not in package:
        _refuse(stage, "'package' must carry a 'manifest'")
    if not data.get("verdict_summary"):
        _refuse(stage, "'verdict_summary' must be present")


def _v_revisions(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    if data.get("classification") not in ("fix", "change", "new_scope"):
        _refuse(stage, "'classification' must be fix, change or new_scope")


#: Stage -> its record validator (dict -> handler, never a branch ladder).
_STAGE_VALIDATORS: dict[str, Callable[[str, dict[str, Any], RunRecord], None]] = {
    "intake": _v_intake, "brief": _v_brief, "scope_freeze": _v_scope_freeze,
    "research": _v_research, "ia": _v_ia, "wireframe": _v_wireframe,
    "copy": _v_copy, "direction": _v_direction, "font": _v_font,
    "colour": _v_colour, "theme": _v_theme, "icon": _v_icon,
    "tokens": _v_tokens, "components": _v_components, "stack": _v_stack,
    "build": _v_build, "preview": _v_preview,
    "judge_visual": _v_judge, "judge_a11y": _v_judge, "judge_perf": _v_judge,
    "judge_content": _v_judge, "judge_responsive": _v_judge,
    "fix_loop": _v_fix_loop, "checkpoints": _v_checkpoints,
    "delivery": _v_delivery, "revisions": _v_revisions,
}


def _validate(stage: str, data: dict[str, Any], record: RunRecord) -> None:
    _require_keys(stage, data, STAGE_RECORDS[stage])
    handler = _STAGE_VALIDATORS.get(stage)
    if handler is not None:
        handler(stage, data, record)


def _machine_data(stage: str, record: RunRecord) -> dict[str, Any]:
    entries = len(store.read_audit(record))
    if stage == "checkpointing":
        return {"checkpoint_written": True,
                "completed_stages": list(record.completed_stages),
                "audit_entries": entries}
    if stage == "rollback":
        return {"rollback_available": True,
                "completed_stages": list(record.completed_stages),
                "audit_entries": entries}
    return {"audit_entries": entries,
            "stages_completed": len(record.completed_stages)}


def advance(
    record: RunRecord, stage: str, *, data: Optional[dict[str, Any]] = None,
) -> RunRecord:
    """Complete *stage* — the next incomplete stage, in order — and
    checkpoint it before the run moves on. Raises :class:`StageRefused`
    when the record fails its contract and :class:`MachineError` when the
    stage is out of order or the run cannot move."""
    if stage not in STAGE_ORDER:
        raise MachineError(f"unknown web-designer stage {stage!r}")
    if record.status != "running":
        raise MachineError(
            f"run {record.run_id} is {record.status!r}; only a running run advances")
    expected = _next_incomplete(record)
    if expected is None:
        raise MachineError(f"run {record.run_id} has completed every stage")
    if stage != expected:
        raise MachineError(
            f"stage {stage!r} is not next: expected {expected!r}")
    if stage in _MACHINE_STAGES:
        if data is not None and not isinstance(data, dict):
            raise MachineError(f"stage {stage!r} takes a mapping or no data")
        data = dict(data) if data else _machine_data(stage, record)
    else:
        if not isinstance(data, dict):
            _refuse(stage, "a stage record is required — refusing without one")
        _validate(stage, data, record)
    store.complete_stage(record, stage, data=data)
    record.stage = _next_incomplete(record) or stage
    store.save_run(record)
    return record


def resume(home: str, run_id: str) -> tuple[RunRecord, Optional[str]]:
    """Load a run and return ``(record, next incomplete stage)`` — None
    when every stage is complete. Completed stages are skipped, never
    re-run. Raises :class:`MachineError` for an unknown run."""
    record = store.load_run(home, run_id)
    if record is None:
        raise MachineError(f"unknown web-designer run {run_id!r}")
    return record, _next_incomplete(record)


def rollback(record: RunRecord, to_stage: str) -> RunRecord:
    """Restore the last good checkpoint at *to_stage*: completed stages
    and stage data after it are dropped, the audit log is kept, and the
    rollback itself is recorded. The run then resumes after *to_stage*."""
    if to_stage not in record.completed_stages:
        raise MachineError(
            f"cannot roll back to {to_stage!r}: it is not a completed stage")
    keep = STAGE_ORDER[to_stage]
    record.completed_stages = [
        s for s in record.completed_stages if STAGE_ORDER[s] <= keep]
    record.stage_data = {
        s: v for s, v in record.stage_data.items()
        if s in STAGE_ORDER and STAGE_ORDER[s] <= keep}
    following = [s for s in STAGE_KEYS if STAGE_ORDER[s] > keep]
    record.stage = following[0] if following else to_stage
    store.append_audit(record, "rollback", {"to_stage": to_stage})
    store.save_run(record)
    return record


def stop_run(record: RunRecord, *, by: str) -> RunRecord:
    """Halt immediately: checkpoint first, then transition to stopped.
    The current iteration is not finished first (audit 5.34)."""
    store.save_run(record)
    store.append_audit(record, "run_stopped", {"by": by})
    return store.transition(record, "stopped", reason=f"stopped by {by}")


def fail_run(record: RunRecord, error: str) -> RunRecord:
    """Transition to failed with the error recorded on the run."""
    record.stage_data["failure"] = {"error": error}
    store.save_run(record)
    store.append_audit(record, "run_failed", {"error": error})
    return store.transition(record, "failed", reason=error[:200])
