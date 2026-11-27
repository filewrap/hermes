"""``web-designer`` tools — the agent-facing surface of the add-on.

Five tools, all load-on-demand: they live in the ``web-designer``
toolset (never in ``_HERMES_CORE_TOOLS``) and are named in the default
defer list, so ordinary turns never see them at all. The skill
(``skills/web-design/SKILL.md``) loads them when a web-design run starts.

Each handler returns a JSON string and returns ``tool_error`` on
refusal — handlers never raise. The machine worker owns
``gateway.web_designer.load_gate``, ``checkpoints``, ``judgers`` and
``machine``; this file only calls their public functions, through
:func:`_call_public`, which passes only the kwargs each function
accepts. While those modules do not exist yet the tools fail closed
with a plain error naming the missing module.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

WEB_DESIGNER_TOOLS = (
    "web_designer_start", "web_designer_status", "web_designer_approve",
    "web_designer_stop", "web_designer_judge",
)
_JUDGERS = ("visual", "a11y", "performance", "content", "responsive")
_DECISIONS = ("approve", "approve_with_changes", "reject", "pick")
_TERMINAL_STATUSES = ("done", "stopped", "failed")


class _Unavailable(Exception):
    """A machine-worker module (or public function) is not there yet."""


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _call_public(module_name: str, names: tuple[str, ...],
                 kwargs: dict[str, Any]) -> Any:
    """Call the first public function in ``names`` on ``module_name``.

    Only the kwargs the function accepts are passed, so a machine-worker
    function with a narrower signature still works. Raises
    :class:`_Unavailable` when the module or every named function is
    missing — the caller turns that into a ``tool_error``.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise _Unavailable(f"{module_name} is not available yet") from exc
    for name in names:
        fn = getattr(module, name, None)
        if not callable(fn):
            continue
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return fn(**kwargs)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(**kwargs)
        accepted = {key: value for key, value in kwargs.items() if key in params}
        return fn(**accepted)
    raise _Unavailable(
        f"{module_name} has no public function among {', '.join(names)}")


def _home() -> str:
    """The routing process home: runs live under the active profile home."""
    # Looked up at call time, like autonomous_done_tool, so tests can
    # patch the hermes_constants seam.
    from hermes_constants import get_routing_process_hermes_home

    return str(get_routing_process_hermes_home())


def _agent_ids(parent_agent: Any) -> tuple[str, str, str]:
    """Best-effort (thread_id, user_id, chat_id) for the calling turn.

    The agent exposes its session key; user and chat ids ride it only
    when the turn binds them, so missing ids stay empty and the load
    gate decides whether that is enough.
    """
    if parent_agent is None:
        return "", "", ""
    thread_id = str(
        getattr(parent_agent, "gateway_session_key", "")
        or getattr(parent_agent, "session_id", "") or "")
    user_id = str(
        getattr(parent_agent, "user_id", "")
        or getattr(parent_agent, "telegram_user_id", "") or "")
    chat_id = str(getattr(parent_agent, "chat_id", "") or "")
    return thread_id, user_id, chat_id


def _load_run(home: str, run_id: str) -> Any:
    """The run record, or None when it does not exist."""
    from gateway.web_designer import store

    try:
        return store.load_run(home, run_id)
    except Exception:  # health: allow BLE001 -- an unreadable store reads as "no such run"
        logger.debug("web-designer could not load run %s", run_id, exc_info=True)
        return None


def _normalize_gate_result(result: Any) -> dict[str, Any]:
    """A load-gate answer as ``{"allowed": bool, "run_id": str|None,
    "reason": str, "redirect": str|None}``, whatever shape the gate returns.

    The real shape is ``load_gate.decide_load``'s: ``{"load": bool,
    "reason": str, "redirect": str|None, "run": RunRecord|None}``.
    """
    if result is None:
        return {"allowed": False, "run_id": None,
                "reason": "Load gate declined the request.", "redirect": None}
    if isinstance(result, bool):
        return {"allowed": result, "run_id": None,
                "reason": "" if result else "Load gate declined the request.",
                "redirect": None}
    if isinstance(result, (list, tuple)):
        allowed = bool(result[0]) if result else False
        reason = str(result[1]) if len(result) > 1 and result[1] else ""
        run_id = str(result[2]) if len(result) > 2 and result[2] else None
        return {"allowed": allowed, "run_id": run_id, "reason": reason,
                "redirect": None}
    if isinstance(result, dict):
        if "load" in result:
            run = result.get("run")
            run_id = getattr(run, "run_id", None) if run is not None else None
            return {
                "allowed": bool(result["load"]),
                "run_id": str(run_id) if run_id else None,
                "reason": str(result.get("reason") or ""),
                "redirect": result.get("redirect"),
            }
        allowed = result.get("allowed")
        if allowed is None:
            allowed = result.get("ok", result.get("run_id") is not None)
        return {
            "allowed": bool(allowed),
            "run_id": str(result["run_id"]) if result.get("run_id") else None,
            "reason": str(result.get("reason") or result.get("message") or ""),
            "redirect": result.get("redirect"),
        }
    run_id = getattr(result, "run_id", None)
    if run_id:
        return {"allowed": True, "run_id": str(run_id), "reason": "",
                "redirect": None}
    return {"allowed": False, "run_id": None,
            "reason": "Load gate returned an unusable answer.",
            "redirect": None}


def _verdict_summary(record: Any) -> list[dict[str, Any]]:
    """One summary dict per recorded judger verdict, if any."""
    stage_data = getattr(record, "stage_data", {}) or {}
    verdicts = stage_data.get("verdicts") if isinstance(stage_data, dict) else None
    out = []
    for verdict in verdicts or []:
        if isinstance(verdict, dict):
            out.append({
                "judger": verdict.get("judger"),
                "passed": bool(verdict.get("passed", False)),
                "findings": len(verdict.get("findings") or []),
                "unchecked": list(verdict.get("unchecked") or []),
                "unjudged": bool(verdict.get("unjudged", False)),
            })
        else:
            out.append({
                "judger": getattr(verdict, "judger", None),
                "passed": bool(getattr(verdict, "passed", False)),
                "findings": len(getattr(verdict, "findings", []) or []),
                "unchecked": list(getattr(verdict, "unchecked", []) or []),
                "unjudged": bool(getattr(verdict, "unjudged", False)),
            })
    return out


def web_designer_start(request_text: Optional[str] = None, *,
                       parent_agent: Any = None) -> str:
    """Run the load gate for a website/webapp request and report the run."""
    try:
        if not isinstance(request_text, str) or not request_text.strip():
            return tool_error("web_designer_start needs the request text.")
        home = _home()
        thread_id, user_id, chat_id = _agent_ids(parent_agent)
        try:
            result = _call_public(
                "gateway.web_designer.load_gate",
                ("decide_load", "evaluate", "evaluate_load_gate", "check",
                 "load_gate"),
                {"text": request_text, "request_text": request_text,
                 "request": request_text,
                 # A live turn exists only for an admitted user: the
                 # gateway admits before the turn starts. A bare call
                 # (no agent) is not admitted and fails closed.
                 "admitted": parent_agent is not None,
                 "home": home, "thread_id": thread_id,
                 "user_id": user_id, "chat_id": chat_id})
        except _Unavailable as exc:
            return tool_error(
                f"{exc}; the web-designer machine is not installed yet.")
        gate = _normalize_gate_result(result)
        if not gate["allowed"]:
            payload = {"status": "declined",
                       "reason": gate["reason"] or "Not a website or webapp request."}
            if gate.get("redirect"):
                payload["redirect"] = gate["redirect"]
            return _ok(**payload)
        run_id = gate["run_id"]
        if not run_id:
            # The gate approved without creating the run: create it here
            # (born idle, then running once the gate has passed).
            from gateway.web_designer import store

            record = store.create_run(
                home=home, thread_id=thread_id, user_id=user_id, chat_id=chat_id)
            store.start_run(record)
            run_id = record.run_id
        return _ok(status="started", run_id=run_id)
    except Exception:  # health: allow BLE001 -- a tool handler never raises; the error is the answer
        logger.debug("web_designer_start failed", exc_info=True)
        return tool_error("web_designer_start failed; try again.")


def web_designer_status(run_id: Optional[str] = None) -> str:
    """Run status, stage, budgets used, and last judger verdicts."""
    try:
        if not isinstance(run_id, str) or not run_id:
            return tool_error("web_designer_status needs a run_id.")
        record = _load_run(_home(), run_id)
        if record is None:
            return tool_error(f"No web-designer run {run_id}.")
        from gateway.web_designer.skills import skills_for_stage
        from gateway.web_designer.tool_registry import (
            live_checker_available, registry_summary, tools_with_checkers,
        )
        from gateway.web_designer.types import STAGE_ORDER

        tools = registry_summary()
        tools["available_live"] = [
            t["name"] for t in tools_with_checkers()
            if live_checker_available(t["name"])]
        return _ok(
            status=record.status, run_id=record.run_id,
            stage=record.stage,
            stage_number=STAGE_ORDER.get(record.stage, 0),
            total_stages=len(STAGE_ORDER),
            skills=list(skills_for_stage(record.stage)),
            tokens={"input": record.input_tokens, "output": record.output_tokens,
                    "total": record.total_tokens},
            cost_usd=record.cost_usd, tool_calls=record.tool_calls,
            iterations=record.iterations,
            verdicts=_verdict_summary(record),
            tools=tools)
    except Exception:  # health: allow BLE001 -- a tool handler never raises; the error is the answer
        logger.debug("web_designer_status failed", exc_info=True)
        return tool_error("web_designer_status failed; try again.")


def web_designer_approve(
    run_id: Optional[str] = None, gate: Optional[str] = None,
    decision: Optional[str] = None, *,
    note: Optional[str] = None, choice: Optional[int] = None,
    parent_agent: Any = None,
) -> str:
    """Record a checkpoint decision (brief / wireframe / direction / preview).

    ``checkpoints.record_decision`` knows exactly three decisions
    (approve, approve_with_changes, reject): ``approve_with_changes``
    needs the listed changes, passed here as ``note``; ``reject`` records
    ``note`` as the reason. A ``pick`` is not a checkpoint decision — it
    records the chosen direction in the run's stage data (the machine's
    direction validator requires ``stage_data["direction"]["chosen"]`` to
    name one of the explored directions) and then approves the direction
    gate, so it is only valid for gate ``direction`` with a ``choice``.
    """
    try:
        from gateway.web_designer.types import GATE_STAGES

        if not isinstance(run_id, str) or not run_id:
            return tool_error("web_designer_approve needs a run_id.")
        if gate not in GATE_STAGES:
            return tool_error(
                f"web_designer_approve gate must be one of "
                f"{', '.join(sorted(GATE_STAGES))}.")
        if decision not in _DECISIONS:
            return tool_error(
                f"web_designer_approve decision must be one of "
                f"{', '.join(_DECISIONS)}.")
        record = _load_run(_home(), run_id)
        if record is None:
            return tool_error(f"No web-designer run {run_id}.")
        _, user_id, _ = _agent_ids(parent_agent)
        if decision == "pick":
            return _approve_pick(record, gate, choice, user_id)
        if decision == "approve_with_changes" and not (note or "").strip():
            return tool_error(
                "web_designer_approve approve_with_changes needs note "
                "with the listed changes.")
        try:
            _call_public(
                "gateway.web_designer.checkpoints",
                ("record_decision", "record_checkpoint_decision", "decide"),
                {"record": record, "run": record, "gate": gate,
                 "decision": decision, "changes": note,
                 "note": note, "actor": user_id, "actor_id": user_id})
        except _Unavailable as exc:
            return tool_error(
                f"{exc}; the web-designer machine is not installed yet.")
        fresh = _load_run(_home(), run_id) or record
        return _ok(status="recorded", run_id=run_id, gate=gate,
                   decision=decision, run_status=fresh.status,
                   stage=fresh.stage)
    except Exception:  # health: allow BLE001 -- a tool handler never raises; the error is the answer
        logger.debug("web_designer_approve failed", exc_info=True)
        return tool_error("web_designer_approve failed; try again.")


def _approve_pick(record: Any, gate: str, choice: Optional[int],
                  user_id: str) -> str:
    """A direction pick: persist the chosen direction, then approve.

    ``choice`` is the index into the run's explored directions
    (stage data first, the gate request payload second). Returns a
    ``tool_error`` when the gate is not ``direction``, the choice is
    missing, or the run lists no directions to pick from.
    """
    from gateway.web_designer import store

    if gate != "direction":
        return tool_error(
            "web_designer_approve pick is only for the direction gate.")
    if not isinstance(choice, int):
        return tool_error(
            "web_designer_approve pick needs choice with the option index.")
    name = _direction_name(record, choice)
    if name is None:
        return tool_error(
            "web_designer_approve pick: the run lists no direction "
            f"at index {choice}.")
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    direction = stage_data.get("direction")
    if not isinstance(direction, dict):
        direction = {}
        stage_data["direction"] = direction
    direction["chosen"] = name
    try:
        store.save_run(record)
        _call_public(
            "gateway.web_designer.checkpoints",
            ("record_decision", "record_checkpoint_decision", "decide"),
            {"record": record, "run": record, "gate": gate,
             "decision": "approve", "actor": user_id, "actor_id": user_id})
    except _Unavailable as exc:
        return tool_error(
            f"{exc}; the web-designer machine is not installed yet.")
    except ValueError as exc:
        return tool_error(f"web_designer_approve refused: {exc}")
    fresh = _load_run(_home(), record.run_id) or record
    return _ok(status="recorded", run_id=record.run_id, gate=gate,
               decision="pick", choice=choice, chosen=name,
               run_status=fresh.status, stage=fresh.stage)


def _direction_name(record: Any, choice: int) -> Optional[str]:
    """The explored direction name at ``choice``, or None."""
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    candidates = []
    direction = stage_data.get("direction")
    if isinstance(direction, dict):
        candidates.append(direction.get("directions"))
    pending = stage_data.get("pending_checkpoint")
    if isinstance(pending, dict):
        payload = pending.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("directions"))
    for directions in candidates:
        if isinstance(directions, list) and 0 <= choice < len(directions):
            item = directions[choice]
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("label") or "")
            else:
                name = str(item)
            return name or None
    return None


def _agent_by(parent_agent: Any) -> str:
    """Who stopped the run, for the audit log."""
    _, user_id, _ = _agent_ids(parent_agent)
    return user_id or "kill switch"


def web_designer_stop(run_id: Optional[str] = None, *,
                      parent_agent: Any = None) -> str:
    """Kill switch: halt the run immediately."""
    try:
        if not isinstance(run_id, str) or not run_id:
            return tool_error("web_designer_stop needs a run_id.")
        record = _load_run(_home(), run_id)
        if record is None:
            return tool_error(f"No web-designer run {run_id}.")
        if record.status in _TERMINAL_STATUSES:
            return _ok(status=record.status, run_id=run_id,
                       note="Run already ended.")
        try:
            _call_public(
                "gateway.web_designer.machine",
                ("stop_run", "halt_run", "halt", "stop"),
                {"record": record, "run": record, "run_id": run_id,
                 "reason": "kill switch",
                 "by": _agent_by(parent_agent)})
        except _Unavailable:
            # No machine module yet: the store transition is the same halt
            # (running / awaiting_checkpoint / needs_input / paused -> stopped).
            from gateway.web_designer import store

            try:
                store.transition(record, "stopped", reason="kill switch")
            except ValueError as exc:
                return tool_error(f"web_designer_stop refused: {exc}")
        fresh = _load_run(_home(), run_id) or record
        return _ok(status=fresh.status, run_id=run_id)
    except Exception:  # health: allow BLE001 -- a tool handler never raises; the error is the answer
        logger.debug("web_designer_stop failed", exc_info=True)
        return tool_error("web_designer_stop failed; try again.")


def web_designer_judge(run_id: Optional[str] = None,
                       judger: Optional[str] = None) -> str:
    """Run one judger over the run's project dir and return its verdict.

    Each judger lives in its own module —
    ``gateway.web_designer.judgers.<judger>.judge(project_dir, *, tokens,
    screenshots, live_metrics, brief, content_draft, layout, axe_results,
    baselines)`` — and returns a ``Verdict``. The optional inputs come
    from the run record when the run has them (brief, copy draft,
    preview screenshots and layout boxes, a Lighthouse run, an axe run,
    baseline screenshots, tokens file); anything missing stays None and
    the judger lists those rules as unchecked instead of guessing.
    """
    try:
        if not isinstance(run_id, str) or not run_id:
            return tool_error("web_designer_judge needs a run_id.")
        if judger not in _JUDGERS:
            return tool_error(
                f"web_designer_judge judger must be one of "
                f"{', '.join(_JUDGERS)}.")
        record = _load_run(_home(), run_id)
        if record is None:
            return tool_error(f"No web-designer run {run_id}.")
        try:
            result = _call_public(
                f"gateway.web_designer.judgers.{judger}",
                ("judge", "run_judger", "run"),
                _judge_inputs(record))
        except _Unavailable as exc:
            return tool_error(
                f"{exc}; the web-designer machine is not installed yet.")
        if hasattr(result, "to_dict"):
            verdict = result.to_dict()
        elif isinstance(result, dict):
            verdict = dict(result)
        else:
            return tool_error(
                "web_designer_judge got an unusable verdict from the judger.")
        verdict.setdefault("run_id", run_id)
        return json.dumps(verdict, ensure_ascii=False)
    except Exception:  # health: allow BLE001 -- a tool handler never raises; the error is the answer
        logger.debug("web_designer_judge failed", exc_info=True)
        return tool_error("web_designer_judge failed; try again.")


def _judge_inputs(record: Any) -> dict[str, Any]:
    """Optional judger inputs from the run record (None when absent)."""
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    tokens = None
    if getattr(record, "tokens_path", ""):
        try:
            from gateway.web_designer.tokens import load_tokens

            tokens = load_tokens(record.tokens_path)
        except Exception:  # health: allow BLE001 -- no tokens file means the token rules go unchecked
            logger.debug("web_designer_judge could not load tokens", exc_info=True)
            tokens = None
    preview = stage_data.get("preview")
    screenshots = preview.get("screenshots") if isinstance(preview, dict) else None
    # Optional inputs the coordinator stores during the run: layout
    # boxes from the live browser (preview stage), a Lighthouse run
    # (live_metrics), an axe run (axe_results), and the directory of
    # baseline screenshots for the visual diff. Anything missing stays
    # None and the judger lists those rules as unchecked instead of
    # guessing. The judgers accept the same kwargs, so one dict feeds
    # all five through _call_public's per-function filter.
    layout = preview.get("layout") if isinstance(preview, dict) else None
    baselines = preview.get("baselines") if isinstance(preview, dict) else None
    return {
        "project_dir": record.project_dir, "tokens": tokens,
        "screenshots": screenshots, "live_metrics": stage_data.get("live_metrics"),
        "brief": dict(record.brief or {}),
        "content_draft": stage_data.get("copy"),
        "layout": layout, "axe_results": stage_data.get("axe_results"),
        "baselines": baselines,
    }


def _always_available() -> bool:
    """The tool is always reachable — availability is the session's
    toolset, never this probe (see tools/AGENTS.md). Handlers gate
    each call themselves."""
    return True


def _handler(name: str):
    """The registry handler for one tool: args plus the live parent agent."""
    handlers = {
        "web_designer_start": lambda args, kw: web_designer_start(
            args.get("request_text"), parent_agent=kw.get("parent_agent")),
        "web_designer_status": lambda args, kw: web_designer_status(
            args.get("run_id")),
        "web_designer_approve": lambda args, kw: web_designer_approve(
            args.get("run_id"), args.get("gate"), args.get("decision"),
            note=args.get("note"), choice=args.get("choice"),
            parent_agent=kw.get("parent_agent")),
        "web_designer_stop": lambda args, kw: web_designer_stop(
            args.get("run_id"), parent_agent=kw.get("parent_agent")),
        "web_designer_judge": lambda args, kw: web_designer_judge(
            args.get("run_id"), args.get("judger")),
    }
    return lambda args, **kw: handlers[name](args, kw)


_SCHEMAS = {
    "web_designer_start": {
        "name": "web_designer_start",
        "description": (
            "Start a web-design run: checks the load gate for a website or webapp "
            "request and reports whether it was accepted, with the run id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request_text": {
                    "type": "string",
                    "description": "The user's request, in their own words.",
                },
            },
            "required": ["request_text"],
        },
    },
    "web_designer_status": {
        "name": "web_designer_status",
        "description": (
            "Report a web-design run: status, current stage, budgets used, and "
            "the last judger verdicts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id from web_designer_start.",
                },
            },
            "required": ["run_id"],
        },
    },
    "web_designer_approve": {
        "name": "web_designer_approve",
        "description": (
            "Record a human checkpoint decision for a web-design run: approve, "
            "approve with changes, reject, or pick one option at a gate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id from web_designer_start.",
                },
                "gate": {
                    "type": "string",
                    "enum": ["brief", "wireframe", "direction", "preview"],
                    "description": "Which checkpoint gate this decides.",
                },
                "decision": {
                    "type": "string",
                    "enum": ["approve", "approve_with_changes", "reject", "pick"],
                    "description": (
                        "\"approve\", \"approve_with_changes\", or \"reject\" for a gate; "
                        "\"pick\" with choice for a direction or option gate."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "The listed changes for approve_with_changes (required), "
                        "or the reason for a reject."
                    ),
                },
                "choice": {
                    "type": "integer",
                    "description": "Option index for a pick decision.",
                },
            },
            "required": ["run_id", "gate", "decision"],
        },
    },
    "web_designer_stop": {
        "name": "web_designer_stop",
        "description": (
            "Kill switch: halt a web-design run immediately. The run stops "
            "where it is and keeps its checkpointed state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id from web_designer_start.",
                },
            },
            "required": ["run_id"],
        },
    },
    "web_designer_judge": {
        "name": "web_designer_judge",
        "description": (
            "Run one judger over a web-design run's project and return its "
            "verdict: pass or fail, findings, and rules it could not check."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id from web_designer_start.",
                },
                "judger": {
                    "type": "string",
                    "enum": ["visual", "a11y", "performance", "content", "responsive"],
                    "description": "Which of the five judgers to run.",
                },
            },
            "required": ["run_id", "judger"],
        },
    },
}

for _name in WEB_DESIGNER_TOOLS:
    registry.register(
        name=_name,
        toolset="web-designer",
        schema=_SCHEMAS[_name],
        handler=_handler(_name),
        check_fn=_always_available)
