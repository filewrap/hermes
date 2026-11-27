#!/usr/bin/env python3
"""F6 session-status readout — thin agent tool over F2 + the live usage gauge.

Reports the three things the model cannot otherwise obtain about its own run:
its model identity, its usage percent, and the current session's label/state.

- Model: the live agent's resolved ``model``/``provider`` first (the route the
  turn is actually answering on, normalized/pinned in ``agent_init``), then the
  session's most recent persisted route (``get_recent_session_model_route`` —
  the same recency source ``/status`` and ``/usage`` use when no agent is
  resident), then the session row's ``model``. The ``source`` field names which
  of the three answered, so a mismatch against config is diagnosable.
- Usage: structured windows from the live credits gauge
  (``agent.account_usage.live_credits_usage_windows``) — the dev fixture when
  set, else the agent's ``_credits_state`` header gauge, the same numbers
  ``/usage`` renders. No portal fetch: a thin readout must not block on
  network I/O, and a provider with no gauge reports ``windows: []`` with a
  reason instead of a fabricated percent.
- Session: the F2 row projection (label, source, model, activity, pin/archive
  state, preview) plus ``state`` from ``session_lifecycle_statuses`` — the one
  indexed last-message lookup the sessions picker already uses, never a
  transcript scan — and ``running`` (true only for the current session: a tool
  call only happens mid-turn).

Deliberately NOT ported from the external agent's ``session-status-tool.ts`` (746 lines):
no ``model`` override parameter (that is a mutation; this tool reports only),
no capabilities block and no ``changesSince`` state-change stream (no Hermes
equivalents), no cross-profile reads (the usage gauge belongs to this agent,
so a foreign session's readout would mix owners), and no gateway visibility
machinery (Hermes admission gates the tool itself). No ``check_fn`` either:
the tool stays visible when the DB or the gauge is missing and reports
per-section availability, like ``github_identity_status``.
"""

import json
import logging
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger("tools.session_status")


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _resolve_model(agent, db, session_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """``{model, provider, source}``: live agent, else recent route, else row."""
    model = (getattr(agent, "model", None) or "").strip() if agent is not None else ""
    provider = (getattr(agent, "provider", None) or "").strip() if agent is not None else ""
    if model or provider:
        return {"model": model or None, "provider": provider or None, "source": "live-agent"}
    route: dict[str, Any] = {}
    if db is not None and session_id:
        try:
            route = db.get_recent_session_model_route(session_id) or {}
        except Exception:  # health: allow BLE001 -- route lookup is best-effort; the row is the fallback
            logging.debug("session_status: recent route lookup failed", exc_info=True)
    if route.get("model"):
        return {"model": route.get("model"), "provider": route.get("billing_provider") or None,
                "source": "recent-route"}
    row_model = (row.get("model") or "").strip()
    return {"model": row_model or None, "provider": None,
            "source": "session-row" if row_model else "unknown"}


def session_status(session_id: Optional[str] = None, db=None, agent=None,
                   current_session_id: Optional[str] = None) -> str:
    """Read the current session's status: model identity, usage, label/state."""
    from agent.account_usage import live_credits_usage_windows
    from tools.sessions_tools import _project_row, _release, _resolve_db

    target = (session_id or current_session_id or "").strip()
    if not target:
        return tool_error("session_status needs session_id (no current session in this context)",
                          success=False)

    row: dict[str, Any] = {}
    session_block: dict[str, Any] = {"session_id": target, "available": False}
    model_block = {"model": None, "provider": None, "source": "unknown"}
    owned_db = None
    try:
        db, owned, error = _resolve_db(db, write=False)
        if owned:
            owned_db = db
        if error is not None:
            session_block["error"] = error
        elif db is None:
            session_block["error"] = "session store unavailable"
        else:
            row = db.get_session(target) or {}
            if not row:
                session_block["error"] = f"session '{target}' not found"
            else:
                session_block = _project_row(row)
                session_block["available"] = True
                try:
                    statuses = db.session_lifecycle_statuses([target])
                    session_block["state"] = statuses.get(target, "empty")
                except Exception:  # health: allow BLE001 -- state is best-effort; the row already answered
                    logging.debug("session_status: lifecycle state lookup failed", exc_info=True)
                    session_block["state"] = None
                session_block["running"] = bool(current_session_id) and target == current_session_id
        # While the handle is still open: the recent-route lookup reads the DB.
        model_block = _resolve_model(agent, db, target, row)
    except Exception as e:  # health: allow BLE001 -- tool boundary: DB failures surface as tool errors, never a crash
        logging.error("session_status failed", exc_info=True)
        return tool_error(f"session_status: {e}", success=False)
    finally:
        if owned_db is not None:
            _release(True, owned_db)

    try:
        usage = live_credits_usage_windows(agent)
    except Exception:  # health: allow BLE001 -- usage is best-effort; model + session already answered
        logging.debug("session_status: usage gauge read failed", exc_info=True)
        usage = {"windows": [], "unavailable_reason": "usage gauge read failed"}
    return _ok(model=model_block, usage=usage, session=session_block,
               current_session_id=current_session_id)


# =========================================================================
# Schema + registration
# =========================================================================

SESSION_STATUS_SCHEMA = {
    "name": "session_status",
    "description": (
        "Read this session's status: model, usage, label, state, activity. "
        "Reports the model answering right now (model id "
        "and provider, with where the id came from), the live usage windows "
        "with percent used, and the session's label, state and activity. "
        "Omit session_id to report the current session. Read-only: it reports "
        "the model, it never changes it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session to report on. Omit for the current session.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="session_status",
    toolset="sessions",
    schema=SESSION_STATUS_SCHEMA,
    handler=lambda args, **kw: session_status(
        session_id=args.get("session_id"),
        db=kw.get("db"), agent=kw.get("agent"),
        current_session_id=kw.get("current_session_id")),
    emoji="📊")
