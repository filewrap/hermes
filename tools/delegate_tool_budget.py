"""Autonomous backbone budgets (DESIGN.md §3): per-run and per-worker token caps.

The coordinator's tokens reach the run record through ``checkpoint_tool`` only;
workers report their own responses here, so the two writers never double-count
the same response. All reads fail open: a missing record or unreadable config
never blocks a spawn or a turn — the per-iteration check is the backstop.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module


def report_worker_response_usage(agent: Any, prompt_tokens: int, completion_tokens: int) -> None:
    """``_on_response_usage`` hook for workers: add one response's tokens to the run ledger.

    Installed on the child at build time by :func:`arm_worker_budget`; the
    agent core invokes it after every response with usage (see
    ``agent/turn_usage.py::record_response_usage``). Never raises into the turn.
    """
    home = getattr(agent, "_autonomous_run_home", None)
    run_id = getattr(agent, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        from gateway import autonomous_state

        autonomous_state.add_tokens(home, run_id, int(prompt_tokens or 0), int(completion_tokens or 0))
    except Exception:
        logger.debug("Autonomous worker token report failed for run %s", run_id, exc_info=True)


def arm_worker_budget(child: Any, parent_agent: Any) -> None:
    """Stamp an autonomous run's budget context onto a freshly built child.

    No-op unless the parent carries run context (set by
    ``gateway/autonomous_turn.py::arm_conversation`` on the coordinator): only
    workers of an autonomous run are capped. The child reports its own tokens
    after every response and checks both its own cap and the run cap at the
    top of each iteration.
    """
    home = getattr(parent_agent, "_autonomous_run_home", None)
    run_id = getattr(parent_agent, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    child._autonomous_run_home = home
    child._autonomous_run_id = run_id
    from tools.delegate_tool_config import _get_max_tokens_per_worker

    child._autonomous_worker_token_cap = _get_max_tokens_per_worker()
    child._on_response_usage = report_worker_response_usage
    from gateway import autonomous_state

    child._autonomous_run_budget_check = autonomous_state.make_run_budget_check(home, run_id)


def run_budget_refusal(parent_agent: Any) -> Optional[str]:
    """Spawn-time refusal when the parent's autonomous run already spent its budget.

    Returns the refusal text, else None. Checked before any child is built, so
    a spent run refuses the spawn instead of starting work it cannot finish.
    """
    home = getattr(parent_agent, "_autonomous_run_home", None)
    run_id = getattr(parent_agent, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return None
    try:
        from gateway import autonomous_state

        record = autonomous_state.load_run(home, run_id)
        if record is None:
            return None
        cap = autonomous_state._config_max_tokens_per_run(home)
        if cap <= 0:
            return None
        total = autonomous_state.run_token_total(record)
        if total < cap:
            return None
        return (
            f"Budget limit reached: autonomous run {run_id} has used {total:,} of {cap:,} tokens "
            f"(autonomous.max_tokens_per_run). Raise the cap or start a fresh run before spawning more workers."
        )
    except Exception:
        logger.debug("Autonomous run budget check failed for run %s", run_id, exc_info=True)
        return None
