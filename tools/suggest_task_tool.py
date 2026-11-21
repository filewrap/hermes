#!/usr/bin/env python3
"""F7 ``suggest_task`` / ``dismiss_task`` — operator-approved follow-up suggestions.

External-agent port (``src/agents/tools/task-suggestion-tools.ts``): the model
suggests a follow-up (title ≤60 chars, prompt ≤32 KB, tldr), gets a
``task_id`` back, and the follow-up starts only when the operator approves
the card. Hermes shape of the same contract:

- **Pending-suggestion store** — one JSON record per suggestion under
  ``<home>/pending/suggestions/<task_id>.json`` (0700 dir, 0600 file,
  ``atomic_json_write``), the same layout ``tools/write_approval.py`` stages
  pending memory/skill writes in. The record is the audit trail: status
  moves ``pending → approved → started`` (or ``pending → denied``), and
  ``dismiss_task`` deletes a pending record.
- **Card / approval presentation** — the suggestion is presented through
  Hermes' existing approvals machinery, not a new gate: the tool blocks in
  ``tools.approval_gateway_wait._await_gateway_decision`` with the session's
  registered gateway notify callback — the same wait the terminal guard
  uses. The runner renders it as a native approval card on adapters that
  have one (Telegram: ``TelethonAdapter._send_exec_approval_prompt``,
  inline Approve-once / Deny buttons, owner-only taps) and as plain text
  with ``/approve`` steps elsewhere. Only Approve-once / Deny are offered
  (``allow_permanent=False, allow_session=False``): a suggestion is a
  one-shot, never a standing grant. The wait is bounded by
  ``approvals.timeout`` (default 300 s); silence is not consent — a
  timeout leaves the suggestion pending in the store, and the card is
  edited in place to a timed-out notice by the runner's settle hook.
  A tap or ``/approve`` resolves the wait; ``/deny <reason>`` relays the
  reason verbatim.
- **Approver = owner only.** The card metadata stamps the turn's owner
  (``run_turn_runner._render_approval_sync``) and the adapter denies
  non-owner taps; ``/approve`` resolves only for a subscriber holding
  ``session:approve``.
- **Follow-up start** — an approved suggestion starts as a delegate child
  run carrying the suggested prompt, through the agent's own
  ``_dispatch_delegate_task`` call site (same depth / spawn-pause / routing
  / budget gates as a model-called ``delegate_task``; top-level runs start
  in the background and re-enter on completion, a delegated child stays
  synchronous). The external agent starts a new session; Hermes has no
  session-create tool primitive, so the delegate child run — its own
  session id, listed in ``agents_list`` — is the port's equivalent. The
  record is re-read before the spawn, so a suggestion dismissed while the
  card was waiting is never started.
- **Autonomous mode bypass** — the user's ruling (2026-11-11): approval-gated
  normally, but autonomous mode takes NO approvals — guard-free, runs
  infinity, suggestions auto-run. The bypass lives behind the single seam
  :func:`_autonomous_mode_enabled`, wired to the runner's per-turn
  autonomous flag (``agent/autonomous_context.py``). When it returns True,
  ``suggest_task`` records the suggestion as auto-approved and starts the
  follow-up at once: no card, no wait, no approval.
- **No card available** (cron, batch, a surface with no gateway notify):
  the suggestion is staged and returned as pending — never silently
  committed, never silently dropped — with the reason, so the model can
  re-present it in a session with a human or dismiss it.

The store is per profile home (``get_hermes_home()`` at call time): one
process serves many profiles, and a suggestion belongs to the profile
whose turn suggested it.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_TITLE_MAX_CHARS = 60
_PROMPT_MAX_CHARS = 32 * 1024
_CARD_DESCRIPTION_MAX_CHARS = 500
_STORE_DIRNAME = "suggestions"


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


# --- Autonomous-mode seam ---------------------------------------------------
# Autonomous mode is built: the master switch lives in config
# (``platforms.telegram.autonomous_mode``), the live flag in the per-launch-home
# run store (``gateway/autonomous_state.py``), and the runner sets the per-turn
# flag (``agent/autonomous_context.py``) around each turn of an autonomous
# session. This one function is the seam that answers "is THIS turn running
# autonomous (owner-only, DM-only, no guards, no turn limits)?" from that
# representation. Until the runner sets it, it is False, and every suggestion
# goes through the owner approval card below.
def _autonomous_mode_enabled() -> bool:
    """True when the current turn runs in autonomous mode (no approvals).

    Wired to the runner's per-turn flag: the runner sets the contextvar
    around each turn of an autonomous session and clears it after.
    """
    from agent.autonomous_context import is_autonomous_turn

    return is_autonomous_turn()


# --- Pending-suggestion store (file-backed, mirrors write_approval) --------

def _store_dir() -> Path:
    return get_hermes_home() / "pending" / _STORE_DIRNAME


def _record_path(task_id: str) -> Path:
    return _store_dir() / f"{task_id}.json"


def _new_task_id() -> str:
    return f"sug_{uuid.uuid4().hex[:8]}"


def _stage_record(title: str, prompt: str, tldr: Optional[str], session_key: str) -> dict[str, Any]:
    """Persist a pending suggestion and return its record."""
    record = {
        "id": _new_task_id(), "title": title, "prompt": prompt,
        "tldr": tldr or "", "status": "pending",
        "created_at": time.time(), "session_key": session_key,
        "approval": None, "request_id": None, "follow_up": None,
        "spawn_error": None, "deny_reason": None,
    }
    path = _record_path(record["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    atomic_json_write(path, record)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return record


def _save_record(record: dict[str, Any]) -> None:
    atomic_json_write(_record_path(str(record["id"])), record)


def _load_record(task_id: str) -> Optional[dict[str, Any]]:
    path = _record_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        logger.warning("Skipping unreadable suggestion record: %s", path)
        return None


def _discard_record(task_id: str) -> bool:
    try:
        path = _record_path(task_id)
        if path.exists():
            path.unlink()
            return True
    except Exception as exc:  # pragma: no cover - disk failure path
        logger.error("Failed to discard suggestion %s: %s", task_id, exc, exc_info=True)
    return False


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """The record as the model sees it: status and follow-up, never the prompt
    back at full length (the model already has it)."""
    return {
        "task_id": record.get("id"), "title": record.get("title"),
        "tldr": record.get("tldr") or None, "status": record.get("status"),
        "approval": record.get("approval"),
        "follow_up": record.get("follow_up"),
    }


# --- Follow-up start --------------------------------------------------------

def _start_follow_up(record: dict[str, Any], parent_agent: Any) -> dict[str, Any]:
    """Start the approved follow-up through the agent's own delegate dispatch.

    Returns ``{"started": True, "follow_up": {...}}`` or
    ``{"started": False, "error": ...}``. The caller marks the record; a
    failed spawn never silently becomes a start, and the model gets the
    reason plus the prompt so it can start the follow-up itself with
    ``delegate_task``.
    """
    dispatch = getattr(parent_agent, "_dispatch_delegate_task", None)
    if parent_agent is None or not callable(dispatch):
        return {"started": False,
                "error": "No live agent turn to start the follow-up in."}
    context = f"Suggested follow-up: {record.get('title')}"
    if record.get("tldr"):
        context += f"\n{record['tldr']}"
    try:
        raw = dispatch({"goal": record.get("prompt"), "context": context})
    except Exception as exc:  # health: allow BLE001 -- a spawn failure is a tool error, never a crash
        logger.debug("suggest_task: follow-up spawn failed", exc_info=True)
        return {"started": False, "error": f"Follow-up spawn failed: {type(exc).__name__}"}
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("success") is not True:
        error = ""
        if isinstance(result, dict):
            error = str(result.get("error") or "")
        return {"started": False,
                "error": error or "Follow-up spawn was refused."}
    return {"started": True, "follow_up": {
        "dispatch": result, "started_at": time.time()}}


def _approve_and_start(record: dict[str, Any], parent_agent: Any,
                       approval: str) -> dict[str, Any]:
    """Mark *record* approved (re-reading it first: a dismiss that landed
    while the card was waiting wins over the approval) and start the
    follow-up. Returns the tool-result payload."""
    fresh = _load_record(str(record["id"]))
    if fresh is None:
        return {"status": "dismissed",
                "message": "The suggestion was dismissed before the approval landed; "
                           "no follow-up was started."}
    if fresh.get("status") != "pending":
        return {"status": str(fresh.get("status")),
                "message": "The suggestion is no longer pending; no follow-up was started."}
    fresh["status"] = "approved"
    fresh["approval"] = approval
    _save_record(fresh)
    spawn = _start_follow_up(fresh, parent_agent)
    if spawn["started"]:
        fresh["status"] = "started"
        fresh["follow_up"] = spawn["follow_up"]
        _save_record(fresh)
        return {**_public(fresh),
                "message": "Approved — the follow-up started with the suggested prompt."}
    fresh["spawn_error"] = spawn["error"]
    _save_record(fresh)
    return {**_public(fresh),
            "message": ("Approved, but the follow-up could not be started: "
                        f"{spawn['error']} Start it yourself with delegate_task, "
                        "goal = the suggested prompt.")}


# --- suggest_task ------------------------------------------------------------

def suggest_task(title: Any = None, prompt: Any = None, tldr: Any = None,
                 parent_agent: Any = None,
                 current_session_id: Optional[str] = None) -> str:
    """Suggest a follow-up task: stage it, get the owner's approval on a card
    (or auto-run it in autonomous mode), and start it on approval."""
    if not isinstance(title, str) or not title.strip():
        return tool_error("title must be a non-empty string.", success=False)
    title = title.strip()
    if len(title) > _TITLE_MAX_CHARS:
        return tool_error(f"title must be at most {_TITLE_MAX_CHARS} characters.", success=False)
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error("prompt must be a non-empty string.", success=False)
    prompt = prompt.strip()
    if len(prompt) > _PROMPT_MAX_CHARS:
        return tool_error(f"prompt must be at most {_PROMPT_MAX_CHARS} characters.", success=False)
    if tldr is not None and not isinstance(tldr, str):
        return tool_error("tldr must be a string.", success=False)
    tldr = (tldr or "").strip() or None

    from tools.approval_context import get_current_session_key

    session_key = get_current_session_key()
    record = _stage_record(title, prompt, tldr, session_key)

    # Autonomous mode: no approvals, no card, no wait — the suggestion
    # auto-runs. The record carries approval="autonomous" so the bypass is
    # auditable, never silent. Turn taint (PROVISIONAL, wave 5): a tainted turn
    # cannot approve things, so it never takes this branch — the suggestion
    # stays pending for the owner instead.
    if _autonomous_mode_enabled():
        from agent.turn_taint import refuse_tainted_auto_approve

        refusal = refuse_tainted_auto_approve(parent_agent)
        if refusal is not None:
            return _ok(**{**_public(record), "status": "pending",
                          "message": refusal})
        return _ok(**_approve_and_start(record, parent_agent, "autonomous"))

    # Approval card through the existing gateway machinery. No registered
    # notify callback means nobody can answer a card in this session (cron,
    # batch, a surface without a gateway): stage-only, like write_approval
    # staging a write nobody can review inline — pending, never committed,
    # never dropped.
    from tools.approval import _gateway_notify_cb
    from tools.approval_gateway_wait import _await_gateway_decision

    notify_cb = _gateway_notify_cb(session_key)
    if notify_cb is None:
        return _ok(**{**_public(record), "status": "pending",
                      "message": ("Staged as a pending suggestion (no approval card can be "
                                  "delivered in this session — nobody is attached to answer "
                                  "one). Re-present it in a session with the owner, or dismiss "
                                  "it with dismiss_task.")})

    request_id = uuid.uuid4().hex
    record["request_id"] = request_id
    _save_record(record)
    description = tldr or prompt[:_CARD_DESCRIPTION_MAX_CHARS]
    decision = _await_gateway_decision(
        session_key, notify_cb,
        {"command": title, "description": description,
         "pattern_key": f"suggest_task:{record['id']}",
         "pattern_keys": [f"suggest_task:{record['id']}"],
         "allow_permanent": False, "allow_session": False,
         "request_id": request_id},
        surface="gateway")
    if decision.get("notify_failed"):
        return _ok(**{**_public(record), "status": "pending",
                      "message": ("The approval card could not be delivered; the suggestion "
                                  "stays pending. Re-present it later or dismiss it.")})
    if decision.get("cancelled"):
        return _ok(**{**_public(record), "status": "pending",
                      "message": (f"The approval prompt was withdrawn before anyone answered "
                                  f"({decision['cancelled']}); the suggestion stays pending.")})
    if not decision.get("resolved"):
        return _ok(**{**_public(record), "status": "pending",
                      "message": ("No answer within the approval window; the suggestion stays "
                                  "pending. The owner can approve it when it is re-presented, "
                                  "or it can be dismissed with dismiss_task.")})
    choice = decision.get("choice")
    if choice is None or choice == "deny":
        record["status"] = "denied"
        record["deny_reason"] = decision.get("reason")
        _save_record(record)
        reason = decision.get("reason")
        return _ok(**_public(record),
                   message=("The owner declined the suggested follow-up"
                            + (f': "{reason}"' if reason else ".")
                            + " Nothing was started."))
    return _ok(**_approve_and_start(record, parent_agent, str(choice)))


# --- dismiss_task ------------------------------------------------------------

def dismiss_task(task_id: Any = None,
                 current_session_id: Optional[str] = None) -> str:
    """Remove a pending suggestion. A card still waiting on it is withdrawn
    (the waiter wakes with a withdrawal, never a user deny)."""
    if not isinstance(task_id, str) or not task_id.strip():
        return tool_error("task_id must be a suggestion id from suggest_task.", success=False)
    task_id = task_id.strip()
    record = _load_record(task_id)
    if record is None:
        return tool_error(f"Unknown suggestion id {task_id}.", success=False)
    if record.get("status") != "pending":
        return tool_error(
            f"Suggestion {task_id} is {record.get('status')}, not pending — only a pending "
            "suggestion can be dismissed.", success=False)
    request_id = record.get("request_id")
    session_key = record.get("session_key")
    if request_id and session_key:
        try:
            from tools.approval import withdraw_gateway_approval

            withdraw_gateway_approval(str(session_key), str(request_id),
                                      "Suggestion dismissed")
        except Exception:  # health: allow BLE001 -- a failed withdraw still dismisses the record
            logger.debug("dismiss_task: card withdraw failed", exc_info=True)
    _discard_record(task_id)
    return _ok(task_id=task_id, title=record.get("title"), status="dismissed",
               message="Suggestion dismissed; nothing was started.")


SUGGEST_TASK_SCHEMA = {
    "name": "suggest_task",
    "description": (
        "Suggest a follow-up task for the owner to approve. The suggestion is staged and shown "
        "to the owner as an approval card in this chat (Approve-once / Deny, owner-only); "
        "on approval the follow-up starts as its own agent run with the suggested prompt. "
        "If the owner does not answer within the approval window the suggestion stays pending "
        "— re-present it or dismiss it with dismiss_task. In autonomous mode the suggestion "
        "auto-runs with no card and no approval. Title at most 60 characters, prompt at most "
        "32 KB. Returns the task_id and the outcome (pending / approved / started / denied)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the follow-up (at most 60 characters). Shown on the approval card.",
            },
            "prompt": {
                "type": "string",
                "description": "The full prompt the follow-up run starts with (at most 32 KB).",
            },
            "tldr": {
                "type": "string",
                "description": "One-line summary shown on the approval card; defaults to the start of the prompt.",
            },
        },
        "required": ["title", "prompt"],
    },
}

DISMISS_TASK_SCHEMA = {
    "name": "dismiss_task",
    "description": (
        "Remove a pending suggestion created by suggest_task. Nothing is started, and an "
        "approval card still waiting on it is withdrawn. Only a pending suggestion can be "
        "dismissed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by suggest_task.",
            },
        },
        "required": ["task_id"],
    },
}


def check_suggest_task_requirements() -> bool:
    """No external requirement: a missing approval surface is a per-call outcome."""
    return True


registry.register(
    name="suggest_task",
    toolset="conversations",
    schema=SUGGEST_TASK_SCHEMA,
    handler=lambda args, **kw: suggest_task(
        title=args.get("title"), prompt=args.get("prompt"), tldr=args.get("tldr"),
        parent_agent=kw.get("parent_agent"),
        current_session_id=kw.get("current_session_id")),
    check_fn=check_suggest_task_requirements,
    emoji="💡")

registry.register(
    name="dismiss_task",
    toolset="conversations",
    schema=DISMISS_TASK_SCHEMA,
    handler=lambda args, **kw: dismiss_task(
        task_id=args.get("task_id"),
        current_session_id=kw.get("current_session_id")),
    check_fn=check_suggest_task_requirements,
    emoji="🗑️")
