"""Web Designer Telegram UI: rich-message cards and ``wd:`` callbacks.

Builders return ``(html, markup)`` — Rich HTML for the card body plus a
native inline keyboard (``mtproto_rich.inline_markup``) for the action
buttons. Copy follows the fork rules: plain factual text, no emojis, no
hyped words, italic page indexation. The only copy pill in any card is
none: these cards carry callback buttons only, never copy pills.

The ``new`` badge on the progress card follows the existing settings
pattern (``rich_ui._settings_badge``): the bare word ``new`` right after
the feature name, tappable, answering exactly ``"new function — enjoy"``
as a toast with ``show_alert=False``.

Callback data (all under the 64-byte callback limit; run ids are 16 hex
chars):

- ``badge:new`` — badge tap, toast only.
- ``wd:approve:<run_id>`` / ``wd:changes:<run_id>`` / ``wd:reject:<run_id>``
  — brief-gate decisions.
- ``wd:direction:<run_id>:<n>`` — pick design direction ``n``.
- ``wd:pick:<run_id>:<kind>:<n>`` — pick option ``n`` for a font/colour/
  theme decision (``kind`` is the gate key).

The handler verifies the actor is the run owner or an admitted user on
that run, routes gate decisions to ``gateway.web_designer.checkpoints``
(the machine worker owns that module; this file only calls its public
functions), answers denied taps silently so nothing about the run leaks,
and edits the card in place with the outcome. Picks are not checkpoint
decisions — ``checkpoints`` knows only approve, approve_with_changes and
reject — so a direction pick persists the chosen direction in the run's
stage data and then approves the direction gate, and a font/colour/theme
pick persists the choice as stage data for the agent to complete.
"""
from __future__ import annotations

import html
import inspect
import logging
from typing import Any, Optional

from .mtproto_rich import inline_markup, rich_button

logger = logging.getLogger(__name__)

# Callback actions. The badge action is the unified ``badge:<kind>``
# template; the per-surface ``wd:badge:new`` handler stays as fallback
# for cards already sent carrying the old data.
BADGE_ACTION = "badge:new"
# Viewports the preview card reports, in order.
_PREVIEW_VIEWPORTS = ("1440", "768", "375")


class _CheckpointsUnavailable(Exception):
    """The checkpoints module (machine worker) is not importable yet."""


def _esc(value: Any) -> str:
    """Escape dynamic data for a Rich HTML card body."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a record, a dict, or a mapping-like object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _markup(rows: list[list[tuple[str, str, Optional[str]]]]) -> Any:
    """A native inline keyboard, or None when there are no buttons."""
    return inline_markup(rows) if rows else None


def _badge_html() -> str:
    """The blue ``new`` badge right after the feature name."""
    from .rich_ui import _settings_badge

    return _settings_badge("new", BADGE_ACTION)


def _budget_lines(run: Any, budgets_used: Optional[dict[str, Any]]) -> list[str]:
    """Token/cost/tool-call/iteration usage against the audit budgets."""
    from gateway.web_designer.types import DEFAULT_BUDGETS

    used = dict(budgets_used or {})
    tokens = int(used.get("tokens", _field(run, "total_tokens", 0)) or 0)
    cost = float(used.get("cost_usd", _field(run, "cost_usd", 0.0)) or 0.0)
    calls = int(used.get("tool_calls", _field(run, "tool_calls", 0)) or 0)
    iterations = int(used.get("iterations", _field(run, "iterations", 0)) or 0)
    caps = DEFAULT_BUDGETS
    return [
        f"Tokens: {tokens:,} / {caps.token_budget:,}",
        f"Cost: ${cost:.2f} / ${caps.cost_budget_usd:.2f}",
        f"Tool calls: {calls} / {caps.tool_call_cap}",
        f"Iterations: {iterations} / {caps.iteration_cap}",
    ]


def _stage_line(run: Any, stage: Optional[str]) -> str:
    """``Stage n / 30 — name`` for the run's current stage."""
    from gateway.web_designer.types import STAGE_NAMES, STAGE_ORDER

    key = stage or str(_field(run, "stage", "load_gate") or "load_gate")
    number = STAGE_ORDER.get(key, 0)
    name = STAGE_NAMES.get(key, key)
    return f"Stage {number} / {len(STAGE_ORDER)} — {name}"


def _verdict_lines(run: Any) -> list[str]:
    """One summary line per recorded judger verdict, if any."""
    stage_data = _field(run, "stage_data", {}) or {}
    verdicts = stage_data.get("verdicts") if isinstance(stage_data, dict) else None
    if not verdicts:
        return ["Verdicts: none yet."]
    lines = []
    for verdict in verdicts:
        judger = _field(verdict, "judger", "?")
        if _field(verdict, "unjudged", False):
            lines.append(f"Verdicts: {judger} — not judged.")
            continue
        outcome = "pass" if _field(verdict, "passed", False) else "fail"
        findings = _field(verdict, "findings", []) or []
        unchecked = _field(verdict, "unchecked", []) or []
        detail = f"{len(findings)} finding(s)" if findings else "no findings"
        if unchecked:
            detail += f", unchecked: {', '.join(str(r) for r in unchecked)}"
        lines.append(f"Verdicts: {judger} — {outcome} ({detail}).")
    return lines


def progress_card(
    run: Any, stage: Optional[str] = None,
    budgets_used: Optional[dict[str, Any]] = None,
) -> tuple[str, Any]:
    """Run status: stage, budget usage, last judger verdicts.

    Carries the ``new`` badge right after the feature name in the title
    row. No action buttons: the card reports state, it does not act.
    """
    run_id = _esc(_field(run, "run_id", ""))
    status = _esc(_field(run, "status", "idle"))
    lines = [f"<h1>Web Designer {_badge_html()}</h1>",
             f"<p>Run: <code>{run_id}</code></p>",
             f"<p>Status: {status}</p>",
             f"<p>{_esc(_stage_line(run, stage))}</p>"]
    lines.extend(f"<p>{_esc(line)}</p>" for line in _budget_lines(run, budgets_used))
    lines.extend(f"<p>{_esc(line)}</p>" for line in _verdict_lines(run))
    return "\n".join(lines), None


def _brief_fields(run: Any) -> list[tuple[str, str]]:
    """The restated brief as (label, value) rows, in fixed order."""
    brief = _field(run, "brief", {}) or {}
    keys = (
        ("problem", "Problem"), ("audience", "Audience"),
        ("primary_action", "Primary action"),
        ("success_measure", "Success measure"), ("tone", "Tone"),
        ("non_goals", "Non-goals"),
    )
    rows = []
    for key, label in keys:
        value = brief.get(key) if isinstance(brief, dict) else None
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(item) for item in value)
        rows.append((label, str(value)))
    return rows


def brief_card(run: Any) -> tuple[str, Any]:
    """The restated brief with Approve / Approve with changes / Reject."""
    run_id = str(_field(run, "run_id", ""))
    lines = ["<h1>Brief — restated</h1>"]
    fields = _brief_fields(run)
    if fields:
        lines.append("<ul>")
        lines.extend(
            f"<li><b>{_esc(label)}:</b> {_esc(value)}</li>"
            for label, value in fields)
        lines.append("</ul>")
    else:
        lines.append("<p>No brief recorded yet.</p>")
    lines.append(
        "<p>Check this against the intent, not the literal wording. "
        "Approve to freeze scope, or reject to stop the run.</p>")
    markup = _markup([
        [("Approve", f"wd:approve:{run_id}", "success"),
         ("Approve with changes", f"wd:changes:{run_id}", "primary")],
        [("Reject", f"wd:reject:{run_id}", "danger")],
    ])
    return "\n".join(lines), markup


def direction_card(run: Any, directions: list[Any]) -> tuple[str, Any]:
    """2–3 named directions, one pick button per direction."""
    run_id = str(_field(run, "run_id", ""))
    lines = ["<h1>Design direction</h1>",
             "<p>Pick one direction. The others are dropped.</p>", "<ul>"]
    rows: list[list[tuple[str, str, Optional[str]]]] = []
    for index, direction in enumerate(directions or []):
        name = _field(direction, "name", direction)
        detail = _field(direction, "description", "")
        lines.append(
            f"<li><b>{_esc(name)}</b>"
            + (f" — {_esc(detail)}" if detail else "") + "</li>")
        rows.append([(f"Pick {name}", f"wd:direction:{run_id}:{index}", "primary")])
    lines.append("</ul>")
    if not directions:
        lines.append("<p>No directions proposed yet.</p>")
    return "\n".join(lines), _markup(rows)


def decision_card(run: Any, kind: str, options: list[Any]) -> tuple[str, Any]:
    """Generic picker for font/colour/theme choices.

    The selected option comes first with a clear selected state
    (highlight-ables): a success-styled button labelled ``Selected —``,
    ahead of the unselected options.
    """
    run_id = str(_field(run, "run_id", ""))
    kind_label = str(kind or "").replace("_", " ").strip() or "choice"
    lines = [f"<h1>{_esc(kind_label.title())}</h1>"]
    ordered = sorted(
        enumerate(options or []),
        key=lambda pair: (not bool(_field(pair[1], "selected", False)), pair[0]))
    rows: list[list[tuple[str, str, Optional[str]]]] = []
    for index, option in ordered:
        label = str(_field(option, "label", option))
        detail = _field(option, "detail", "")
        selected = bool(_field(option, "selected", False))
        lines.append(
            f"<p>{'Selected: ' if selected else ''}<b>{_esc(label)}</b>"
            + (f" — {_esc(detail)}" if detail else "") + "</p>")
        button_label = f"Selected — {label}" if selected else label
        style = "success" if selected else None
        rows.append([(button_label, f"wd:pick:{run_id}:{kind}:{index}", style)])
    if not options:
        lines.append("<p>No options proposed yet.</p>")
    return "\n".join(lines), _markup(rows)


def preview_card(run: Any, screenshots: Optional[dict[str, Any]] = None) -> tuple[str, Any]:
    """Preview at 1440/768/375 plus the widget/local-viewer entry.

    The preview itself is the existing widget/local-viewer path, not a
    second browser or preview system: a preview URL rides as an in-body
    link button when the run has one. When no preview is available the
    card says so plainly instead of implying one exists.
    """
    run_id = _esc(_field(run, "run_id", ""))
    shots = dict(screenshots or {})
    preview_url = shots.get("preview_url") or _field(run, "preview_url", "")
    lines = ["<h1>Preview</h1>", f"<p>Run: <code>{run_id}</code></p>"]
    for viewport in _PREVIEW_VIEWPORTS:
        shot = shots.get(viewport)
        if shot:
            lines.append(f"<p>{viewport}px: {_esc(shot)}</p>")
        else:
            lines.append(f"<p>{viewport}px: no screenshot yet.</p>")
    if preview_url:
        lines.append(
            "<p>" + rich_button("Open preview", kind="url", value=str(preview_url))
            + "</p>")
    else:
        lines.append("<p>No preview available.</p>")
    lines.append(
        "<p>Approve to accept this preview, or reject to stop the run.</p>")
    markup = _markup([
        [("Approve", f"wd:approve:{run_id}", "success"),
         ("Approve with changes", f"wd:changes:{run_id}", "primary")],
        [("Reject", f"wd:reject:{run_id}", "danger")],
    ])
    return "\n".join(lines), markup


def verdict_card(verdict: Any) -> tuple[str, Any]:
    """One judger's verdict: pass/fail, findings, unchecked rules.

    Every finding is one line (rule, severity, location, expected vs
    actual, evidence). Unchecked rules are listed explicitly — never a
    silent pass. A judger that could not run at all is stated plainly.
    """
    judger = _esc(_field(verdict, "judger", "?"))
    lines = [f"<h1>Judger verdict — {judger}</h1>"]
    if _field(verdict, "unjudged", False):
        lines.append(
            "<p>Not judged: this judger could not run over the build.</p>")
        return "\n".join(lines), None
    outcome = "Pass" if _field(verdict, "passed", False) else "Fail"
    lines.append(f"<p>Result: {outcome}</p>")
    findings = _field(verdict, "findings", []) or []
    if findings:
        lines.append("<ul>")
        for finding in findings:
            rule = _esc(_field(finding, "rule_id", "?"))
            severity = _esc(_field(finding, "severity", "?"))
            location = _esc(_field(finding, "location", "?"))
            expected = _esc(_field(finding, "expected", "?"))
            actual = _esc(_field(finding, "actual", "?"))
            evidence = _esc(_field(finding, "evidence", ""))
            line = (f"<li>{rule} · {severity} · {location} — "
                    f"expected: {expected}; actual: {actual}")
            if evidence:
                line += f"; evidence: {evidence}"
            lines.append(line + "</li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No findings.</p>")
    unchecked = _field(verdict, "unchecked", []) or []
    if unchecked:
        lines.append(
            "<p>Not checked: "
            + ", ".join(_esc(rule) for rule in unchecked) + "</p>")
    else:
        lines.append("<p>Not checked: none.</p>")
    return "\n".join(lines), None


def delivery_card(run: Any, manifest: Optional[dict[str, Any]] = None) -> tuple[str, Any]:
    """Handoff package contents, judger summary, limitations, revisions.

    Delivery is the preview plus this handoff package only. There is no
    deploy target and no publish button: publish/deploy never happens
    without approval, and Phase 1 has no deploy path at all.
    """
    info = dict(manifest or {})
    run_id = _esc(_field(run, "run_id", ""))
    lines = ["<h1>Delivery</h1>", f"<p>Run: <code>{run_id}</code></p>",
             "<p>No deploy target. Delivery is the preview and this "
             "handoff package.</p>"]
    contents = info.get("contents") or []
    if contents:
        lines.append("<p>Package contents:</p><ul>")
        lines.extend(f"<li>{_esc(item)}</li>" for item in contents)
        lines.append("</ul>")
    summary = info.get("judger_summary") or {}
    if isinstance(summary, dict) and summary:
        lines.append("<p>Judger summary:</p><ul>")
        lines.extend(
            f"<li>{_esc(judger)}: {_esc(outcome)}</li>"
            for judger, outcome in summary.items())
        lines.append("</ul>")
    elif isinstance(summary, str) and summary:
        lines.append(f"<p>Judger summary: {_esc(summary)}</p>")
    limitations = info.get("limitations") or info.get("known_limitations") or []
    if limitations:
        lines.append("<p>Known limitations and defects:</p><ul>")
        lines.extend(f"<li>{_esc(item)}</li>" for item in limitations)
        lines.append("</ul>")
    revisions = info.get("revision_instructions") or ""
    if revisions:
        lines.append(
            f"<p>Revisions: {_esc(revisions)}</p>")
    return "\n".join(lines), None


# --- Callback handling -------------------------------------------------------


def _call_first(module: Any, names: tuple[str, ...], kwargs: dict[str, Any]) -> Any:
    """Call the first public function in ``names`` the module provides,
    passing only the kwargs its signature accepts."""
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
    raise _CheckpointsUnavailable(
        f"checkpoints module has no public function among {', '.join(names)}")


def _record_decision(
    record: Any, *, gate: str, decision: str,
    changes: Optional[Any] = None,
) -> Any:
    """Record a checkpoint decision via the checkpoints module.

    The machine worker owns that module; this only calls its public
    function ``record_decision`` (``record, gate, decision, *,
    changes, return_stage``). ``approve_with_changes`` needs the listed
    changes — a tap alone cannot supply them, so that call is refused
    by the module and the card says so.
    """
    try:
        from gateway.web_designer import checkpoints
    except ImportError as exc:
        raise _CheckpointsUnavailable(
            "gateway.web_designer.checkpoints is not available yet") from exc
    return _call_first(
        checkpoints,
        ("record_decision", "record_checkpoint_decision", "decide"),
        {"record": record, "run": record, "gate": gate, "decision": decision,
         "changes": changes, "note": changes})


def _home_for(adapter: Any, source: Any) -> str:
    """The profile home the run lives under for this callback source."""
    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    if callable(resolver):
        try:
            home = resolver(source)
        except Exception:  # health: allow BLE001 -- a bad resolver must not take the callback down; the launch home is the fallback
            home = None
        if home:
            return str(home)
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def _load_run(adapter: Any, source: Any, run_id: str) -> Any:
    """The run record for this callback, or None when it does not exist."""
    from gateway.web_designer import store

    try:
        return store.load_run(_home_for(adapter, source), run_id)
    except Exception:  # health: allow BLE001 -- an unreadable store reads as "no such run" for the tapper
        logger.debug("web-designer callback could not load run %s", run_id, exc_info=True)
        return None


def _is_actor_allowed(adapter: Any, source: Any, record: Any) -> bool:
    """The run owner, or an admitted user on that run."""
    actor = str(getattr(source, "user_id", "") or "")
    if not actor:
        return False
    if actor == str(_field(record, "user_id", "")):
        return True
    authorized = getattr(adapter, "_authorized", None)
    if callable(authorized):
        try:
            return bool(authorized(source))
        except Exception:  # health: allow BLE001 -- a failing admission probe denies, it never admits
            logger.debug("web-designer admission probe failed", exc_info=True)
    return False


async def _edit_card(adapter: Any, source: Any, event: Any,
                     html_text: str, markup: Any = None) -> None:
    """Edit the tapped card in place; a failed edit is logged, not raised."""
    try:
        peer = adapter._peer_for(source.chat_id)
        from .mtproto_rich import edit_request

        request = edit_request(
            peer, getattr(event, "message_id", 0), html_text,
            reply_markup=markup)
        await adapter._mtproto_rich_request(request)
    except Exception:  # health: allow BLE001 -- the tap was already answered; a failed edit must not raise
        logger.debug("web-designer card edit failed", exc_info=True)


def _outcome_html(record: Any, note: str) -> tuple[str, Any]:
    """The progress card plus a one-line outcome note, for the edit."""
    html_text, markup = progress_card(record)
    return html_text + f"\n<p><i>{_esc(note)}</i></p>", markup


def _fixed_action(spec: tuple[str, str, str]):
    """A parser for a no-choice action: (gate, decision, note)."""
    def parse(parts: list[str]) -> Optional[tuple[str, str, str]]:
        gate, decision, note = spec
        return gate, decision, note
    return parse


# Callback action -> parser of the ``wd:`` parts into
# (gate, decision, outcome note). A parser returning None means
# the payload is malformed: the tap is answered silently. Direction and
# option picks are not here: ``checkpoints`` knows no ``pick`` decision,
# so picks take their own path below (persist the choice, and for the
# direction gate approve it once the choice is recorded).
_ACTION_PARSERS = {
    "approve": _fixed_action(("brief", "approve", "Brief approved.")),
    "changes": _fixed_action(
        ("brief", "approve_with_changes", "Brief approved with changes.")),
    "reject": _fixed_action(("brief", "reject", "Brief rejected. Run stopped.")),
}


def _direction_name(record: Any, choice: int) -> Optional[str]:
    """The explored direction name at ``choice``, or None.

    Stage data first, the gate request payload second — the same two
    places the machine reads directions from.
    """
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


def _option_label(record: Any, kind: str, index: int) -> Optional[str]:
    """The option label at ``index`` for a font/colour/theme pick, or None."""
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    entry = stage_data.get(kind)
    if not isinstance(entry, dict):
        return None
    for key in ("options", "families", "roles", "choices", "directions"):
        items = entry.get(key)
        if isinstance(items, list) and 0 <= index < len(items):
            item = items[index]
            if isinstance(item, dict):
                return str(item.get("label") or item.get("name") or "") or None
            return str(item)
    return None


async def _handle_direction_pick(
    adapter: Any, event: Any, source: Any, record: Any, parts: list[str],
) -> bool:
    """``wd:direction:<run_id>:<n>`` — record the chosen direction, then
    approve the direction gate. The machine's direction validator
    requires ``stage_data["direction"]["chosen"]`` to name one of the
    explored directions, so the choice is persisted first; the gate
    decision follows through ``checkpoints``."""
    from .callback_answer import answer_callback
    from gateway.web_designer import store

    try:
        index = int(parts[3])
    except (IndexError, ValueError):
        await answer_callback(event)
        return True
    name = _direction_name(record, index)
    if name is None:
        await answer_callback(event)
        await _edit_card(
            adapter, source, event,
            "<h1>Web Designer</h1>"
            "<p>Direction not recorded — the run lists no direction "
            "at that index.</p>")
        return True
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    direction = stage_data.get("direction")
    if not isinstance(direction, dict):
        direction = {}
        stage_data["direction"] = direction
    direction["chosen"] = name
    try:
        store.save_run(record)
        _record_decision(record, gate="direction", decision="approve")
    except _CheckpointsUnavailable:
        await answer_callback(event)
        await _edit_card(
            adapter, source, event,
            "<h1>Web Designer</h1>"
            "<p>Direction recorded, but the gate decision was not — "
            "checkpoint support is not available yet.</p>")
        return True
    except Exception as exc:  # health: allow BLE001 -- a refused decision is card text, not a raised error
        logger.debug("web-designer direction decision failed", exc_info=True)
        await answer_callback(event)
        await _edit_card(
            adapter, source, event,
            "<h1>Web Designer</h1>"
            f"<p>Direction {_esc(name)} recorded, but the gate decision "
            f"was refused: {_esc(exc)}</p>")
        return True
    await answer_callback(event)
    fresh = _load_run(adapter, source, record.run_id) or record
    html_text, markup = _outcome_html(fresh, f"Direction {name} picked.")
    await _edit_card(adapter, source, event, html_text, markup)
    return True


async def _handle_option_pick(
    adapter: Any, event: Any, source: Any, record: Any, parts: list[str],
) -> bool:
    """``wd:pick:<run_id>:<kind>:<n>`` — record the chosen option as the
    stage's choice. Font/colour/theme are not checkpoint gates, so no
    gate decision follows: the agent reads the recorded choice when it
    completes the stage."""
    from .callback_answer import answer_callback
    from gateway.web_designer import store

    if len(parts) < 5:
        await answer_callback(event)
        return True
    kind = parts[3]
    try:
        index = int(parts[4])
    except ValueError:
        await answer_callback(event)
        return True
    label = _option_label(record, kind, index)
    stage_data = record.stage_data if isinstance(record.stage_data, dict) else {}
    entry = stage_data.get(kind)
    if not isinstance(entry, dict):
        entry = {}
        stage_data[kind] = entry
    entry["chosen"] = label if label is not None else index
    entry["option_index"] = index
    try:
        store.save_run(record)
    except Exception:  # health: allow BLE001 -- a failed save is card text, not a raised error
        logger.debug("web-designer option pick could not save", exc_info=True)
        await answer_callback(event)
        await _edit_card(
            adapter, source, event,
            "<h1>Web Designer</h1>"
            "<p>Choice not recorded — the run could not be saved.</p>")
        return True
    await answer_callback(event)
    shown = label if label is not None else str(index)
    html_text, markup = _outcome_html(
        record, f"{kind.replace('_', ' ')} option {shown} picked.")
    await _edit_card(adapter, source, event, html_text, markup)
    return True


async def handle_web_designer_callback(
    adapter: Any, event: Any, source: Any, data: str,
) -> bool:
    """Route one ``wd:`` callback. Returns False when it is not ours.

    Denied taps are answered silently (no toast that leaks run state).
    A recorded decision edits the card in place with the outcome.
    """
    if not isinstance(data, str) or not data.startswith("wd:"):
        return False
    from .callback_answer import answer_callback

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "badge":
        from .rich_ui import _BADGE_TOASTS

        toast = _BADGE_TOASTS.get(parts[2] if len(parts) > 2 else "")
        if toast is None:
            await answer_callback(event)
        else:
            await answer_callback(event, toast)
        return True
    if len(parts) < 3 or not parts[2]:
        await answer_callback(event)
        return True
    run_id = parts[2]
    record = _load_run(adapter, source, run_id)
    if record is None:
        await answer_callback(event)
        return True
    if not _is_actor_allowed(adapter, source, record):
        await answer_callback(event)
        return True
    if action == "direction":
        return await _handle_direction_pick(adapter, event, source, record, parts)
    if action == "pick":
        return await _handle_option_pick(adapter, event, source, record, parts)
    parser = _ACTION_PARSERS.get(action)
    if parser is None:
        await answer_callback(event)
        return True
    parsed = parser(parts)
    if parsed is None:
        await answer_callback(event)
        return True
    gate, decision, note = parsed
    try:
        _record_decision(record, gate=gate, decision=decision)
    except _CheckpointsUnavailable:
        await answer_callback(event)
        await _edit_card(
            adapter, source, event,
            "<h1>Web Designer</h1>"
            "<p>Decision not recorded — checkpoint support is not "
            "available yet.</p>")
        return True
    except Exception as exc:  # health: allow BLE001 -- a refused decision is card text, not a raised error
        logger.debug("web-designer decision failed for run %s", run_id, exc_info=True)
        await answer_callback(event)
        if decision == "approve_with_changes":
            # The module refuses the tap: the listed changes are
            # required and a tap cannot list them. The user sends the
            # changes as a message; the agent records them.
            await _edit_card(
                adapter, source, event,
                "<h1>Web Designer</h1>"
                "<p>Send the changes as a message — the tap alone "
                "cannot list them.</p>")
        else:
            await _edit_card(
                adapter, source, event,
                "<h1>Web Designer</h1>"
                f"<p>Decision not recorded: {_esc(exc)}</p>")
        return True
    await answer_callback(event)
    fresh = _load_run(adapter, source, run_id) or record
    html_text, markup = _outcome_html(fresh, note)
    await _edit_card(adapter, source, event, html_text, markup)
    return True
