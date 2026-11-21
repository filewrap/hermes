"""Per-turn request-override merging for reused gateway agents.

Split out of ``gateway/run_turn_runner.py`` (over the file-size cap).
"""


def merge_turn_request_overrides(agent, turn_route) -> None:
    """Merge, never overwrite: init-time request overrides (e.g. a custom provider's extra_body)
    must survive every reused-agent turn. Drop only the PREVIOUS turn's routing overrides before
    layering this turn's, so stale per-turn values never linger."""
    overrides = dict(getattr(agent, "request_overrides", {}) or {})
    for key, value in (getattr(agent, "_gateway_turn_request_overrides", {}) or {}).items():
        if overrides.get(key) == value:
            overrides.pop(key, None)
    turn_overrides = dict(turn_route.get("request_overrides") or {})
    overrides.update(turn_overrides)
    agent.request_overrides = overrides
    agent._gateway_turn_request_overrides = turn_overrides
