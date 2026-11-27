"""Autonomous-mode boot wiring for GatewayRunner.

Split out of ``gateway/run_startup.py``: a restart never disables
autonomous mode, and the auto-resume drain must never restart an
autonomous turn. Module functions take the runner, the same shape as
``gateway.shutdown_flush.recover_gateway_pending``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("gateway.run")

_AUTONOMOUS_AUTO_RESUME_BLOCK_STATES = frozenset({"running", "failed", "stopped"})


def autonomous_auto_resume_blocker(home: str | Path, session_key: str) -> Optional[dict[str, Any]]:
    """The autonomous run record blocking an auto-resume for this session, if any.

    A record in ``running`` (reconnect path), ``failed`` (what the boot
    marking leaves behind) or ``stopped`` (an owner-ended run with a leftover
    resume mark) means the interrupted turn belonged to the run: restarting
    it as an ordinary turn would rerun the goal's work outside the run
    record, and the owner decides via the resume ask on re-enable. Any other
    state — ``done``, ``idle``, or no record — leaves the drain alone.
    """
    from gateway import autonomous_state

    record = autonomous_state.find_run_for_session(home, session_key)
    if record is not None and str(record.get("state")) in _AUTONOMOUS_AUTO_RESUME_BLOCK_STATES:
        return record
    return None


async def fail_interrupted_autonomous_run_on_boot(runner) -> None:
    """Boot: mark the interrupted autonomous run failed; the mode stays as it was.

    Runs BEFORE the auto-resume drain in ``_start_finish_wiring``. The live
    flag and the pinned message are left untouched — autonomous mode never
    disables itself, so the pinned enable card still claims a mode that is
    on — and the drain sees the failed record and leaves the session for
    the resume ask. Best-effort throughout: a failure here must never abort
    startup. The runner argument keeps the boot-wiring shape; the step
    needs no delivery adapter anymore (nothing is unpinned or deleted).
    """
    try:
        from gateway import autonomous_state
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        failed = autonomous_state.fail_interrupted_run_on_boot(home)
        if failed is not None:
            logger.info("Autonomous run %s was interrupted by the restart; marked failed",
                        failed.get("run_id"))
    except Exception:
        logger.exception("Autonomous boot marking failed; the live flag is untouched")
