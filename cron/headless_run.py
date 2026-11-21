"""Run this headless with a runId: the generic headless-run entry point.

Other subsystems invoke the scheduler without owning a stored cron job:
``run_headless(run_id=..., prompt=..., ...)`` runs one ad-hoc agent turn
through the shared ``run_one_job`` seam (execution ledger, detached-worker
handoff, fire-claim delivery fence, delivery queue) and returns whether the
run was processed. The run is keyed by ``run_id`` through
``gateway.platforms.launch_idempotency``: a repeated call with the same
``run_id`` and the same spec joins or replays instead of running twice; the
same ``run_id`` with a different spec is a conflict and raises.

The delivery fence is NOT re-implemented here. Hermes already holds
occurrence authority in ``_FireOwnership`` (``cron/scheduler.py:2979``):
the fire claim is re-validated before every side effect
(``side_effect_fence`` / ``lost``) and is never serialized into the delivery
payload or the queue row — only job JSON + content cross that boundary,
which is the external-agent fence's "host-held occurrence authority, never
serialized" rule in Hermes' own shape. ``run_headless`` reuses that fence
by going through ``run_one_job`` instead of building a second one.

The run executes under the caller's ambient profile scope (the ticker binds
``_profile_cron_scope``, the gateway binds its runtime scope before calling
in); ``run_headless`` binds no scope itself, exactly like ``run_one_job``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The transient job id doubles as an output-directory name, so the caller
# runId is sanitized to filesystem-safe characters; the raw runId is kept on
# the job as ``headless_run_id``.
_JOB_ID_SAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _job_id_for(run_id: str) -> str:
    safe = _JOB_ID_SAFE.sub("-", run_id).strip("-") or "run"
    return f"headless-{safe}"


def build_headless_job(
    *,
    run_id: str,
    prompt: str,
    delivery: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    script: Optional[str] = None,
    no_agent: bool = False,
) -> dict[str, Any]:
    """The transient (never stored in ``jobs.json``) job dict for one headless
    run. ``run_one_job`` treats a dict that is not in the store as a handed-in
    job: the execution ledger still records the fire, ``claim_dispatch`` and
    ``mark_job_run`` tolerate the missing store row, and delivery follows the
    same ``deliver`` lane stored jobs use (default ``local``: save, no send).
    """
    job: dict[str, Any] = {
        "id": _job_id_for(run_id),
        "headless_run_id": run_id,
        "name": f"headless:{run_id}",
        "prompt": prompt,
        "deliver": delivery or "local",
        "origin": "headless",
        "enabled": True,
        "no_agent": bool(no_agent),
    }
    if model:
        job["model"] = model
    if provider:
        job["provider"] = provider
    if script:
        job["script"] = script
    return job


def run_headless(
    *,
    run_id: str,
    prompt: str,
    delivery: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    script: Optional[str] = None,
    no_agent: bool = False,
    extra_prompt: Optional[str] = None,
    cancel_event=None,
    adapters=None,
    loop=None,
    verbose: bool = False,
) -> bool:
    """Run one headless agent turn for ``run_id``. True when this call (or the
    replayed first call) processed the run; False when another process owns a
    live run for the same id. A ``run_id`` reused with a different spec raises
    ``LaunchIdempotencyConflict`` instead of running the wrong work."""
    from cron.scheduler import run_one_job
    from gateway.platforms.launch_idempotency import (
        LaunchIdempotency,
        request_fingerprint,
    )

    if not str(run_id or "").strip():
        raise ValueError("run_headless requires a non-empty run_id")
    job = build_headless_job(
        run_id=run_id,
        prompt=prompt,
        delivery=delivery,
        model=model,
        provider=provider,
        script=script,
        no_agent=no_agent,
    )
    spec = {
        "run_id": run_id,
        "prompt": prompt,
        "delivery": job["deliver"],
        "model": model,
        "provider": provider,
        "script": script,
        "no_agent": no_agent,
    }
    outcome = LaunchIdempotency().launch_once(
        scope=LaunchIdempotency.headless_scope(),
        key=str(run_id),
        fingerprint=request_fingerprint(spec),
        run_id=f"headless:{run_id}",
        run=lambda: run_one_job(
            job,
            adapters=adapters,
            loop=loop,
            verbose=verbose,
            extra_prompt=extra_prompt,
            cancel_event=cancel_event,
        ),
        status_extra=lambda processed: {"processed": bool(processed)},
    )
    if outcome.outcome == "created":
        return bool(outcome.result)
    if outcome.outcome == "joined":
        return bool(outcome.result)
    if outcome.outcome == "replayed" and outcome.record is not None:
        return bool((outcome.record.get("status") or {}).get("processed", True))
    logger.info(
        "Headless run %s already owned by a live run; this call ran nothing", run_id
    )
    return False
