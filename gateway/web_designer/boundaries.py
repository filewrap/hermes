"""Executor-enforced boundaries for the web-designer add-on (audit 5).

Caps are enforced in code, not prompt advice: iteration, token, cost,
tool-call and wall-clock budgets, fix-loop caps, the sandbox and size
caps, the licensing allowlist with its provenance log, and the
never-without-approval gates. A breach raises :class:`BoundaryViolation`;
:func:`check_budgets` otherwise returns warnings (80% token budget).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from utils import atomic_json_write

from . import store
from .types import DEFAULT_BUDGETS, Budgets, RunRecord

__all__ = [
    "BoundaryViolation",
    "check_budgets",
    "check_fix_loop_cap",
    "check_license",
    "check_no_progress",
    "check_path_within",
    "check_same_error",
    "check_sandbox",
    "read_daily_cost",
    "record_asset",
    "record_usage",
    "require_approval",
]

#: The daily cost cap (audit 5.8) is per calendar day in the user's
#: timezone, not per run: the ledger below accumulates every run's cost
#: under one profile home for that day.
_IST = ZoneInfo("Asia/Kolkata")


class BoundaryViolation(Exception):
    """A boundary cap was breached or a gated action was refused."""


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _today_ist() -> date:
    return datetime.now(_IST).date()


def daily_cost_path(home: str | Path, day: date) -> Path:
    """``<home>/web-designer/daily_cost/<YYYY-MM-DD>.json`` — one ledger
    file per calendar day (Asia/Kolkata)."""
    return store.web_designer_home(home) / "daily_cost" / f"{day.isoformat()}.json"


def read_daily_cost(home: str | Path, *, day: Optional[date] = None) -> float:
    """Total cost recorded for *day* (default today, Asia/Kolkata)
    across every run under this home. Missing or unreadable ledger
    counts as zero — the cap fails open only on a broken ledger, and
    the next :func:`record_usage` rewrites it."""
    path = daily_cost_path(home, day or _today_ist())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("total_cost_usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _add_daily_cost(home: str | Path, amount_usd: float) -> float:
    """Add *amount_usd* to today's ledger and return the new total."""
    day = _today_ist()
    path = daily_cost_path(home, day)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    total = read_daily_cost(home, day=day) + amount_usd
    atomic_json_write(path, {"date": day.isoformat(),
                             "total_cost_usd": total}, mode=0o600)
    return total


def record_usage(
    record: RunRecord,
    *,
    input_tokens: int,
    output_tokens: int,
    tool_calls: int = 0,
    rate_usd_per_1k: float = 0.01,
    tool: Optional[str] = None,
    tool_args: Any = None,
) -> RunRecord:
    """Add token/tool usage and its estimated cost to the run.

    ``rate_usd_per_1k`` is a documented estimate (default $0.01/1k
    tokens), overridable per provider; it is never silently zero — a
    zero or negative rate is refused so cost is never under-reported.
    When *tool* is given the call is logged for the same-tool repeat cap.
    The cost delta is also added to the home's daily ledger, so the
    $10/day cap (audit 5.8) accumulates across runs.
    """
    if input_tokens < 0 or output_tokens < 0 or tool_calls < 0:
        raise ValueError("usage counts cannot be negative")
    if rate_usd_per_1k <= 0:
        raise ValueError("rate_usd_per_1k must be a positive estimate, not zero")
    record.input_tokens += input_tokens
    record.output_tokens += output_tokens
    record.tool_calls += tool_calls
    delta = (input_tokens + output_tokens) / 1000 * rate_usd_per_1k
    record.cost_usd += delta
    if delta > 0:
        _add_daily_cost(record.home, delta)
    if tool is not None:
        log = record.stage_data.setdefault("tool_calls_log", [])
        log.append({"tool": tool, "args": tool_args})
        record.stage_data["tool_calls_log"] = log[-30:]
    store.save_run(record)
    return record


def _same_tool_repeat(record: RunRecord, budgets: Budgets) -> bool:
    log = record.stage_data.get("tool_calls_log") or []
    if len(log) < budgets.same_tool_repeat_cap:
        return False
    tail = log[-budgets.same_tool_repeat_cap:]
    first = tail[0]
    return all(entry == first for entry in tail)


def check_budgets(
    record: RunRecord, *, budgets: Budgets = DEFAULT_BUDGETS,
) -> list[str]:
    """Return warnings, or raise :class:`BoundaryViolation` on a breach.

    Warnings: token budget at 80%. Breaches: iteration cap, token budget,
    cost budget, daily cost budget (across runs, audit 5.8), tool-call cap
    (including the same tool + args 3x), wall-clock 120 min/build and
    45 min/revision, revision cap 2.
    """
    warnings: list[str] = []
    if record.iterations >= budgets.iteration_cap:
        raise BoundaryViolation(
            f"iteration cap reached: {record.iterations}/{budgets.iteration_cap}")
    if record.total_tokens >= budgets.token_budget:
        raise BoundaryViolation(
            f"token budget reached: {record.total_tokens}/{budgets.token_budget}")
    if record.total_tokens >= budgets.token_budget * budgets.token_warn_fraction:
        warnings.append(
            f"token budget {budgets.token_warn_fraction:.0%} used: "
            f"{record.total_tokens}/{budgets.token_budget}")
    if record.cost_usd >= budgets.cost_budget_usd:
        raise BoundaryViolation(
            f"cost budget reached: ${record.cost_usd:.2f}/${budgets.cost_budget_usd:.2f}")
    daily_total = read_daily_cost(record.home)
    if daily_total >= budgets.daily_cost_budget_usd:
        raise BoundaryViolation(
            f"daily cost budget reached: "
            f"${daily_total:.2f}/${budgets.daily_cost_budget_usd:.2f} today")
    if record.tool_calls >= budgets.tool_call_cap:
        raise BoundaryViolation(
            f"tool-call cap reached: {record.tool_calls}/{budgets.tool_call_cap}")
    if _same_tool_repeat(record, budgets):
        last = (record.stage_data.get("tool_calls_log") or [])[-1]
        raise BoundaryViolation(
            f"same tool+args repeated {budgets.same_tool_repeat_cap}x: "
            f"{last.get('tool')}")
    started = _parse_iso(record.started_at)
    if started is not None:
        elapsed = (datetime.now(UTC) - started).total_seconds() / 60
        if elapsed >= budgets.wall_clock_build_minutes:
            raise BoundaryViolation(
                f"wall-clock build cap reached: {elapsed:.0f} min")
    revision_start = _parse_iso(
        str(record.stage_data.get("revision_started_at") or ""))
    if revision_start is not None:
        elapsed = (datetime.now(UTC) - revision_start).total_seconds() / 60
        if elapsed >= budgets.wall_clock_revision_minutes:
            raise BoundaryViolation(
                f"wall-clock revision cap reached: {elapsed:.0f} min")
    if record.revision_rounds > budgets.revision_cap:
        raise BoundaryViolation(
            f"revision cap exceeded: {record.revision_rounds}/{budgets.revision_cap}")
    return warnings


def check_fix_loop_cap(record: RunRecord, issue_key: str) -> int:
    """Attempts used on one issue; raises when the 3-cycle cap is hit."""
    used = int(record.fix_attempts.get(issue_key, 0))
    if used >= DEFAULT_BUDGETS.fix_loop_cap:
        raise BoundaryViolation(
            f"fix-loop cap reached for {issue_key}: {used} attempts")
    return used


def check_same_error(record: RunRecord, error_text: str) -> None:
    """3 consecutive identical errors stop patching that error (5.2)."""
    log = record.stage_data.setdefault("error_log", [])
    log.append(error_text)
    record.stage_data["error_log"] = log[-3:]
    store.save_run(record)
    tail = record.stage_data["error_log"]
    if len(tail) == 3 and tail[0] == tail[1] == tail[2]:
        raise BoundaryViolation(
            f"same error 3x in a row, stop patching it: {error_text[:200]}")


def check_no_progress(record: RunRecord, changed: bool) -> int:
    """3 iterations with no change halt the loop (5.3). Returns the
    current no-progress streak; raises at the cap."""
    streak = int(record.stage_data.get("no_progress_streak", 0))
    streak = 0 if changed else streak + 1
    record.stage_data["no_progress_streak"] = streak
    store.save_run(record)
    if streak >= DEFAULT_BUDGETS.no_progress_cap:
        raise BoundaryViolation(
            f"no progress in {streak} iterations — halt and report")
    return streak


def check_path_within(project_dir: str | Path, path: str | Path) -> Path:
    """Resolve *path* and refuse it unless it stays under *project_dir*."""
    base = Path(project_dir).resolve()
    target = Path(path).resolve() if Path(path).is_absolute() else (base / path).resolve()
    if target != base and base not in target.parents:
        raise BoundaryViolation(
            f"path escapes the project sandbox: {path} (project: {base})")
    return target


def check_sandbox(
    project_dir: str | Path, *, budgets: Budgets = DEFAULT_BUDGETS,
) -> dict[str, Any]:
    """Enforce the 5.29 size caps over the project tree. Returns a summary
    (file count, total bytes, largest files); raises on any breach."""
    base = Path(project_dir)
    files: list[tuple[Path, int]] = []
    total = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        files.append((path, size))
        total += size
        if size > budgets.max_file_mb * 1024 * 1024:
            raise BoundaryViolation(
                f"file over {budgets.max_file_mb:g} MB cap: {path} ({size} bytes)")
    if len(files) > budgets.max_files:
        raise BoundaryViolation(
            f"file count over cap: {len(files)}/{budgets.max_files}")
    if total > budgets.max_total_mb * 1024 * 1024:
        raise BoundaryViolation(
            f"project over {budgets.max_total_mb:g} MB cap: {total} bytes")
    largest = sorted(files, key=lambda item: item[1], reverse=True)[:5]
    return {
        "files": len(files), "total_bytes": total,
        "largest": [{"path": str(p), "bytes": s} for p, s in largest],
    }


#: Never-without-approval actions (audit 5.23-5.28). There is no
#: auto-approve path: every call returns a denial naming the action and
#: the approval it needs.
_APPROVAL_ACTIONS = {
    "publish": "publish/deploy",
    "deploy": "publish/deploy",
    "spend": "spend money",
    "pii": "use real user data / PII",
    "contact": "contact a third party",
    "dns": "change DNS / domain / hosting",
    "domain": "change DNS / domain / hosting",
    "hosting": "change DNS / domain / hosting",
    "delete": "delete / destructive operation",
    "destructive": "delete / destructive operation",
}


def require_approval(action: str, *, detail: str = "") -> dict[str, Any]:
    """Deny a gated action. Always returns a denial — no auto-approve
    path exists; the user must explicitly approve the named action."""
    name = _APPROVAL_ACTIONS.get((action or "").strip().lower(), action or "unknown action")
    reason = (
        f"{name} requires explicit user approval before it runs"
        + (f": {detail}" if detail else "")
        + "; blocked by the executor, not just the prompt."
    )
    return {"approved": False, "action": name, "reason": reason}


#: Licence allowlist (audit 5.30). Keys are normalised licence labels.
_ALLOWED_LICENSES = {
    "unsplash license", "unsplash", "pexels license", "pexels",
    "sil ofl", "ofl", "ofl-1.1", "mit", "isc",
}
_BLOCKED_HOSTS = ("flaticon", "freepik")


def check_license(url: str, license: str) -> bool:
    """True only for an allowlisted licence. Attribution-required,
    personal-use-only, unknown licences and attribution-required hosts
    (Flaticon, Freepik) are blocked."""
    lowered_url = (url or "").lower()
    if any(host in lowered_url for host in _BLOCKED_HOSTS):
        return False
    label = (license or "").strip().lower()
    if not label or "attribution" in label or "personal use" in label:
        return False
    if "cc by" in label or "creative commons" in label:
        return False
    return label in _ALLOWED_LICENSES


def _assets_path(record: RunRecord) -> Path:
    return store.run_dir(record.home, record.run_id) / "assets.json"


def record_asset(record: RunRecord, source_url: str, license: str) -> dict[str, Any]:
    """Write one allowlisted asset's provenance to the run's assets.json.
    A blocked licence raises :class:`BoundaryViolation` and records
    nothing."""
    if not check_license(source_url, license):
        raise BoundaryViolation(
            f"asset licence blocked: {license!r} from {source_url}")
    path = _assets_path(record)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        assets = existing.get("assets", []) if isinstance(existing, dict) else []
    except (OSError, ValueError):
        assets = []
    entry = {"source_url": source_url, "license": license,
             "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if entry["source_url"] not in {a.get("source_url") for a in assets if isinstance(a, dict)}:
        assets.append(entry)
    atomic_json_write(path, {"assets": assets}, mode=0o600)
    store.append_audit(record, "asset_recorded",
                       {"source_url": source_url, "license": license})
    return entry
