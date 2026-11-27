"""``hm:autonomous:*`` callbacks: the Disable button on the pinned enable message, and the
Resume / Start fresh buttons on the resume-ask card.

Owner-only + DM-only, silent on failure: a tapper who is not the owner in
the owner's DM gets a bare answer (the loading spinner dismissed, nothing
said) — the mode's existence never leaks to anyone else. Disabling clears
the live flag, stops the run (the record is kept as the resume source),
and unpins + deletes the pinned message; the callback answer is the
confirmation, since the button lives on the message being deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: DM chat types, same set as the admission-branch pattern in
#: ``plugins/platforms/telegram/telegram_controls.py``.
_AUTONOMOUS_DM_CHAT_TYPES = frozenset({"dm", "private", "direct"})


def clear_master_setting(home: Any) -> bool:
    """Clear the master ``platforms.telegram.autonomous_mode`` config key.

    Best-effort: a command-disable has already cleared the live flag and
    stopped the run by the time this runs, so a failed write must not
    turn the disable into an error — it is logged and the disable still
    stands. Returns whether the write landed. Both disable paths (the
    ``/autonomous disable`` handler and this callback) call it, so the
    settings row agrees with the live state after a command-disable.
    """
    try:
        from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

        TelegramPersonalSettingsUI._set_autonomous_mode(home, False)
    except Exception:
        logger.warning("Could not clear autonomous_mode setting on disable", exc_info=True)
        return False
    return True


async def unpin_pinned(adapter: Any, pinned: Optional[dict[str, Any]]) -> None:
    """Unpin a pinned message, best-effort; never raises.

    Unlike :func:`unpin_and_delete_pinned`, the message stays: Disable
    turns the pinned enable card into the disabled status card in place.
    """
    if not pinned:
        return
    chat_id = pinned.get("chat_id")
    message_id = pinned.get("pinned_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await adapter.pin_chat_message(chat_id=chat_id, message_id=message_id, unpin=True)
    except Exception:
        logger.debug("Autonomous unpin failed", exc_info=True)


async def unpin_and_delete_pinned(adapter: Any, pinned: Optional[dict[str, Any]]) -> None:
    """Unpin + delete a pinned message, best-effort; never raises."""
    if not pinned:
        return
    chat_id = pinned.get("chat_id")
    message_id = pinned.get("pinned_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await adapter.pin_chat_message(chat_id=chat_id, message_id=message_id, unpin=True)
    except Exception:
        logger.debug("Autonomous unpin failed", exc_info=True)
    try:
        await adapter.delete_message(str(chat_id), str(message_id))
    except Exception:
        logger.debug("Autonomous pinned-message delete failed", exc_info=True)


async def kill_autonomous_run(runner: Any, source: Any, *, via: str = "kill") -> dict[str, Any]:
    """Kill the autonomous run: coordinator + workers + queued + revival.

    The one stop (backbone §4). In order: the live async workers for the
    session are interrupted (which also supersedes their automatic
    resumes), the live in-turn children are counted, the coordinator turn
    is interrupted through the same ``_interrupt_and_clear_session``
    machinery /stop uses (its hard interrupt reaches the in-turn children
    and cancels queued batch futures, so no queued worker ever spawns),
    and only then the run record is ended through ``disable()`` — the
    shared flag-clear path, never a second one — and marked killed, so
    the run is never offered as Resume. The master setting clears so the
    settings row agrees with the live state.

    Honest limit (the AutoGen caveat): a tool call already executing runs
    to completion and its result is discarded — the kill guarantees no new
    work starts and no worker survives, it does not unwind a call already
    in flight.

    Best-effort, never raises; returns a summary dict (``killed`` False
    with a reason when there was nothing live to kill, or the caller was
    not the owner in their DM). The caller sends the one message from it.
    """
    try:
        from gateway import autonomous_state
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        if not autonomous_state.is_active(home):
            return {"killed": False, "reason": "no_live_run"}
        raw_platform = getattr(source, "platform", None)
        platform = str(getattr(raw_platform, "value", raw_platform)).lower()
        if platform != "telegram":
            return {"killed": False, "reason": "not_telegram"}
        actor_id = str(getattr(source, "user_id", "") or "")
        if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
                or str(getattr(source, "chat_id", "")) != actor_id
                or not actor_id):
            return {"killed": False, "reason": "not_owner_dm"}
        adapter = runner._delivery_adapter_for(source)
        checker = getattr(adapter, "_is_owner_user", None) if adapter is not None else None
        if not callable(checker) or not checker(actor_id, source):
            return {"killed": False, "reason": "not_owner"}

        state = autonomous_state.load_state(home)
        run_id = state.get("current_run_id")
        record = (autonomous_state.load_run(home, run_id)
                  if isinstance(run_id, str) and run_id else None)
        goal = str((record or {}).get("goal") or "")
        session_key = _session_key_for_source(source)

        # 1. Live async workers first: the count is captured here, and the
        # interrupt supersedes their automatic resumes, so a killed run is
        # never revived by the watcher.
        async_n = 0
        try:
            from tools.async_delegation import interrupt_for_session

            async_n = int(interrupt_for_session(
                session_key=session_key, reason="autonomous_kill") or 0)
        except Exception:
            logger.debug("Autonomous kill: async interrupt failed", exc_info=True)

        # 2. Live in-turn children, counted before the coordinator dies.
        sync_n = 0
        try:
            peek = getattr(runner, "_peek_session_state", None)
            session_state = peek(session_key) if callable(peek) else None
            agent = getattr(getattr(session_state, "turn", None), "agent", None)
            children = getattr(agent, "_active_children", None)
            if isinstance(children, list):
                sync_n = len(children)
        except Exception:
            logger.debug("Autonomous kill: child count failed", exc_info=True)

        # 3. The coordinator turn, through the /stop machinery (hard
        # interrupt + queued futures cancelled + async sweep for stragglers).
        try:
            from gateway.run import _INTERRUPT_REASON_STOP

            interrupt = getattr(runner, "_interrupt_and_clear_session", None)
            if callable(interrupt):
                await interrupt(session_key, source,
                                interrupt_reason=_INTERRUPT_REASON_STOP,
                                invalidation_reason="autonomous_kill")
        except Exception:
            logger.debug("Autonomous kill: coordinator interrupt failed", exc_info=True)

        # 4. The record, through disable() — the one flag-clear path —
        # then marked killed, so it is never a Resume candidate.
        try:
            pinned = autonomous_state.disable(home, stopped_by="kill")
            await unpin_and_delete_pinned(adapter, pinned)
        except Exception:
            logger.debug("Autonomous kill: disable failed", exc_info=True)
        if isinstance(run_id, str) and run_id:
            try:
                autonomous_state.mark_killed(home, run_id)
            except ValueError:
                logger.debug("Autonomous kill: run %s already gone", run_id)
        clear_master_setting(home)

        tokens = 0
        try:
            final = (autonomous_state.load_run(home, run_id)
                     if isinstance(run_id, str) and run_id else None)
            tokens = autonomous_state.run_token_total(final) if final is not None else 0
        except Exception:
            logger.debug("Autonomous kill: token total failed", exc_info=True)
        return {"killed": True, "run_id": run_id, "goal": goal,
                "workers_interrupted": async_n + sync_n,
                "async_interrupted": async_n, "sync_children": sync_n,
                "tokens": tokens, "via": via}
    except Exception:
        logger.debug("Autonomous kill failed", exc_info=True)
        return {"killed": False, "reason": "error"}


def kill_summary_text(summary: dict[str, Any]) -> str:
    """The one kill message from a :func:`kill_autonomous_run` summary."""
    from agent.i18n import t

    if not summary.get("killed"):
        return t("gateway.autonomous.kill_no_run")
    return t("gateway.autonomous.killed",
             workers=int(summary.get("workers_interrupted") or 0),
             tokens=int(summary.get("tokens") or 0))


async def stop_run_for_stop(runner: Any, source: Any, *, stopped_by: str = "stop_command") -> None:
    """End the autonomous run after /stop, best-effort; never raises.

    Only the owner's DM can end the run this way: any other platform, chat
    type, or actor is a silent no-op (the mode's existence never leaks).
    The run record is kept as ``stopped`` (the resume source), the live
    flag clears, the pin is unpinned + deleted, and the master setting
    clears so the settings row agrees with the live state — the same
    end-state as /autonomous disable. When the mode is already off there
    is nothing to end and the config is left untouched.
    """
    try:
        from gateway import autonomous_state
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        if not autonomous_state.is_active(home):
            return
        raw_platform = getattr(source, "platform", None)
        platform = str(getattr(raw_platform, "value", raw_platform)).lower()
        if platform != "telegram":
            return
        actor_id = str(getattr(source, "user_id", "") or "")
        if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
                or str(getattr(source, "chat_id", "")) != actor_id
                or not actor_id):
            return
        adapter = runner._delivery_adapter_for(source)
        checker = getattr(adapter, "_is_owner_user", None) if adapter is not None else None
        if not callable(checker) or not checker(actor_id, source):
            return
        pinned = autonomous_state.disable(home, stopped_by=stopped_by)
        await unpin_and_delete_pinned(adapter, pinned)
        clear_master_setting(home)
    except Exception:
        logger.debug("Autonomous /stop cleanup failed", exc_info=True)


def _current_badge(run: dict[str, Any]) -> str:
    """Small inline Current/Running badge for a run line or a summary.

    A tappable ``badge:<type>`` button — the tap answers its toast
    automatically. Small and inline after the label, never a big pill.
    """
    from plugins.platforms.telegram.mtproto_rich import rich_button

    if str(run.get("state") or "") == "running":
        return rich_button("running", kind="callback",
                           value="badge:running", style="success")
    return rich_button("current", kind="callback",
                       value="badge:current", style="primary")


def _format_run_tokens(tokens: int, budget_cap: int) -> str:
    """Compact used/budget tokens for a run line: ``450.0K / 1.0M``.

    Plain text, no brackets — the tokens read directly, with the budget
    beside them when one is set. A disabled budget (cap <= 0) reads
    used-only, ``450.0K``. The callers add the word "tokens" themselves
    (the status card and the overview line), so this returns just the
    numbers.
    """
    from gateway import autonomous_state

    used = autonomous_state.compact_tokens(tokens)
    if budget_cap <= 0:
        return used
    return f"{used} / {autonomous_state.compact_tokens(budget_cap)}"


def _format_model(value: Any) -> str:
    """Plain-text model for a table row: a ``{"model", "provider"}`` dict
    (the shape the run record stamps) reads ``"model · provider"``,
    never a Python dict repr. A bare string reads as-is."""
    if isinstance(value, dict):
        model = str(value.get("model") or "")
        provider = str(value.get("provider") or "")
        if model and provider:
            return f"{model} · {provider}"
        return model or provider or "—"
    text = str(value or "").strip()
    return text or "—"


def _state_badge(run: dict[str, Any]) -> str:
    """Small inline badge for a failed run's state, in the summary line.

    The same ``badge:<type>`` pattern as the Current/Running badges — a
    tappable button that answers its toast automatically, small and
    inline after the label, never a big pill. Only ``failed`` gets one:
    the other states keep their plain word. A table cell cannot carry a
    button, so the State row stays plain text.
    """
    from plugins.platforms.telegram.mtproto_rich import rich_button

    if str(run.get("state") or "") == "failed":
        return rich_button("failed", kind="callback",
                           value="badge:failed", style="danger")
    return ""


def _status_card_html(home: Any) -> str:
    """Rich HTML for the bare-/autonomous status card, state by state.

    Direct HTML (not ``lines_to_rich_html``) so the Current/Running badge
    can sit inline in the current run's line. Disabled: the big Enable
    button (worded so it starts fresh) on its own row, then Sessions,
    Limits, Resume Previous. Enabled: Disable + Sessions only — no
    Resume, no Limits, because limits cannot change mid-run and one
    ongoing run stays primary. Tokens read compact, without brackets.
    """
    from html import escape as _esc

    from agent.i18n import t
    from gateway import autonomous_state
    from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

    active = autonomous_state.is_active(home)
    state = autonomous_state.load_state(home)
    current_run_id = state.get("current_run_id")
    runs = autonomous_state.recent_runs(home, limit=3)
    budget_cap = autonomous_state.run_budget_cap(home)
    resumable = (None if active
                 else autonomous_state.find_resumable_run(home))

    parts: list[str] = []
    if active:
        # The green pill means the mode is enabled, so it only renders
        # while the mode is on. When off, the card starts at the status
        # line, whose red disabled badge carries the state on its own.
        parts.append(rich_button_row(
            [rich_button("autonomous", kind="disabled", style="success")]))
    badge = rich_button(
        "enabled" if active else "disabled", kind="callback",
        value="badge:enabled" if active else "badge:disabled",
        style="success" if active else "danger")
    parts.append(f"<p><b>Autonomous mode</b> • {badge}</p>")
    parts.append(f"<p>{t('gateway.autonomous.status_what')}</p>")
    parts.append(f"<p>{t('gateway.autonomous.status_how')}</p>")
    parts.append(f"<p><b>{t('gateway.autonomous.status_sessions')}</b></p>")
    total_tokens = 0
    if runs:
        for run in runs:
            tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
            total_tokens += tokens
            steps = int(run.get("steps") or 0)
            goal_html = _esc(str(run.get("goal") or "") or "(no goal)")
            if (isinstance(current_run_id, str) and current_run_id
                    and str(run.get("run_id")) == current_run_id):
                goal_html += " " + _current_badge(run)
            extra = ""
            if str(run.get("last_tool") or ""):
                extra = t("gateway.autonomous.status_last_tool",
                          tool=_esc(str(run.get("last_tool"))))
            if (resumable is not None
                    and str(run.get("run_id")) == str(resumable.get("run_id"))):
                # The Resume Previous button re-opens this record; say so
                # on its line, or the button names no run at all.
                extra += t("gateway.autonomous.status_resumable")
            ago_text = _ago_text(str(run.get("last_step_at") or ""))
            ago = f" · {ago_text}" if ago_text else ""
            parts.append("<p>- " + t(
                "gateway.autonomous.status_run",
                goal=goal_html,
                state=_status_card_state_word(run),
                steps=steps,
                steps_word="step" if steps == 1 else "steps",
                tokens=f"{_format_run_tokens(tokens, budget_cap)}",
                extra=extra,
                ago=ago,
            ) + "</p>")
        parts.append("<p>" + t(
            "gateway.autonomous.status_total",
            tokens=f"{autonomous_state.compact_tokens(total_tokens)}",
            count=len(runs),
            sessions_word="session" if len(runs) == 1 else "sessions") + "</p>")
        if any(not str(run.get("goal") or "") for run in runs):
            # A goal only exists when it was typed after /autonomous
            # enable; the Enable button starts a run without one.
            parts.append(f"<p>{t('gateway.autonomous.status_no_goal_hint')}</p>")
    else:
        parts.append(f"<p>{t('gateway.autonomous.status_no_sessions')}</p>")
    if active:
        parts.append(rich_button_row([rich_button(
            "Disable", kind="callback",
            value="hm:autonomous:disable", style="danger")]))
        parts.append(rich_button_row([rich_button(
            "Sessions", kind="callback",
            value="hm:autonomous:sessions", style="primary")]))
    else:
        parts.append(rich_button_row([rich_button(
            "Enable (starts fresh)", kind="callback",
            value="hm:autonomous:enable", style="success")]))
        second = [rich_button("Sessions", kind="callback",
                              value="hm:autonomous:sessions", style="primary"),
                  rich_button("Limits", kind="callback",
                              value="hm:autonomous:limits", style="primary")]
        if resumable is not None:
            second.append(rich_button(
                "Resume previous", kind="callback",
                value=f"hm:autonomous:resume:{resumable['run_id']}",
                style="primary"))
        parts.append(rich_button_row(second))
    return "\n".join(parts)


async def _edit_card_html(adapter: Any, chat_id: Any, message_id: Any,
                          html: str) -> bool:
    """Re-render a message in place with new Rich HTML; False when unavailable."""
    from .mtproto_rich import edit_request

    try:
        request = edit_request(adapter._peer_for(chat_id),
                               int(message_id or 0), html, rich_format="html")
        await adapter._mtproto_rich_request(request)
    except Exception:
        logger.debug("Autonomous card edit failed", exc_info=True)
        return False
    return True


async def _edit_status_card(adapter: Any, event: Any, source: Any) -> bool:
    """Re-render the tapped message in place as the status card."""
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    return await _edit_card_html(
        adapter, source.chat_id, getattr(event, "message_id", 0),
        _status_card_html(home))


async def edit_message_to_status_card(adapter: Any, chat_id: Any,
                                      message_id: Any, source: Any) -> bool:
    """Re-render a named message in place as the status card.

    The command path's edit: ``/autonomous disable`` turns the pinned
    enable card into the disabled status card instead of deleting it.
    """
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    return await _edit_card_html(
        adapter, chat_id, message_id, _status_card_html(home))


async def send_status_card(adapter: Any, source: Any, *, reply_to: Any = None) -> Optional[str]:
    """Send the bare-/autonomous status card; the sent message id, or None.

    One message carries the status, the last sessions, and the state
    buttons — Enable (starts fresh) / Sessions / Limits / Resume
    Previous when off, Disable / Sessions when on. The plain status text
    is the fallback when the card cannot be sent.
    """
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    html = _status_card_html(home)
    send_card = getattr(adapter, "send_command_card", None)
    if not callable(send_card):
        return None
    try:
        return await send_card(
            source=source, html=html, buttons=(), reply_to=reply_to)
    except Exception:
        logger.debug("Autonomous status card send failed", exc_info=True)
        return None


def _status_card_state_word(run: dict[str, Any]) -> str:
    """State word for a run line; a waiting run says so in plain words."""
    return {"stopped": "stopped", "failed": "failed",
            "needs_input": "waiting for input"}.get(
                str(run.get("state") or ""), str(run.get("state") or ""))


# --- Autonomous sessions page ----------------------------------------------
# One ``<details>`` collapsible per run, 10 per page, newest first — the
# same shape as the sessions listing in command_cards.py. Each run is
# identified by its goal (or last message), carries a table (turns,
# tokens, last tool, recent tools, agents, model, times), an overview
# line the Copy summary button copies, and a Resume button — only while
# the mode is off, because one ongoing run stays primary. The current
# run carries its small inline Current/Running badge in the summary.
_SESSIONS_PAGE_SIZE = 10
_SESSION_OVERVIEW_GOAL_CHARS = 120


def _session_overview(run: dict[str, Any], budget_cap: int = 0) -> str:
    """One-line overview of a run: the text the Copy summary button copies.

    The goal is trimmed so the whole line fits the copy button's
    256-character limit. ``budget_cap`` is the run's configured budget
    (0 = no budget shown); the page passes the cap it read once.
    """
    goal = " ".join(str(run.get("goal") or "(no goal)").split())
    goal = goal[:_SESSION_OVERVIEW_GOAL_CHARS]
    tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
    steps = int(run.get("steps") or 0)
    overview = (f"{goal} • {_status_card_state_word(run)} • {steps} turns • "
                f"{_format_run_tokens(tokens, budget_cap)} tokens")
    last_tool = str(run.get("last_tool") or "")
    if last_tool:
        overview += f" • last: {last_tool}"
    return overview[:256]


def _sessions_page_html(home: Any, page: int = 1) -> str:
    """Rich HTML for one sessions page; the body is direct HTML."""
    from html import escape as _esc

    from agent.i18n import t
    from gateway import autonomous_state
    from plugins.platforms.telegram.mtproto_rich import (
        rich_button, rich_button_row, rich_page_footer)

    active = autonomous_state.is_active(home)
    state = autonomous_state.load_state(home)
    current_run_id = state.get("current_run_id")
    runs = autonomous_state.all_runs(home)
    budget_cap = autonomous_state.run_budget_cap(home)
    total = len(runs)
    pages = max(1, (total + _SESSIONS_PAGE_SIZE - 1) // _SESSIONS_PAGE_SIZE)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * _SESSIONS_PAGE_SIZE
    page_runs = runs[start:start + _SESSIONS_PAGE_SIZE]

    parts = [f"<h1>{_esc(t('gateway.autonomous.sessions_title'))}</h1>"]
    if active:
        parts.append(f"<p><i>{_esc(t('gateway.autonomous.sessions_active_hint'))}</i></p>")
    if not runs:
        parts.append(f"<p>{_esc(t('gateway.autonomous.sessions_empty'))}</p>")
    for run in page_runs:
        run_id = str(run.get("run_id") or "")
        goal = " ".join(str(run.get("goal") or "(no goal)").split())
        tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
        steps = int(run.get("steps") or 0)
        ago_text = _ago_text(str(run.get("last_step_at") or ""))
        summary_html = _esc(goal[:80])
        state_badge = _state_badge(run)
        if state_badge:
            summary_html += f" — {state_badge} · "
        else:
            summary_html += (f" — {_status_card_state_word(run)} · ")
        summary_html += _format_run_tokens(tokens, budget_cap)
        if ago_text:
            summary_html += f" · {ago_text}"
        if run_id and run_id == str(current_run_id or ""):
            summary_html += " " + _current_badge(run)
        overview = _session_overview(run, budget_cap)
        large = steps >= 20 or tokens >= 1_000_000
        body = [f"<p><b>{_esc(overview)}</b></p>" if large
                else f"<p>{_esc(overview)}</p>"]
        agents = [_format_model(item) for item in (run.get("recent_agents") or [])
                  if str(item)]
        if not agents and run.get("model") is not None:
            agents = [_format_model(run.get("model"))]
        recent_tools = [str(item) for item in (run.get("recent_tools") or []) if str(item)]
        rows: list[tuple[str, str]] = [
            ("State", _status_card_state_word(run)),
            ("Turns", str(steps)),
            ("Tokens", _format_run_tokens(tokens, budget_cap)),
            ("Last tool", str(run.get("last_tool") or "—")),
            ("Recent tools", ", ".join(recent_tools) or "—"),
            ("Agents", ", ".join(agents) or "—"),
            ("Model", _format_model(run.get("model"))),
            ("Started", str(run.get("started_at") or "")[:16] or "—"),
            ("Last step", ago_text or "—"),
        ]
        if str(run.get("last_error") or ""):
            rows.append(("Error", str(run.get("last_error"))[:200]))
        table = "<table><tbody>" + "".join(
            f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
            for label, value in rows) + "</tbody></table>"
        body.append(table)
        # One row, three buttons: Copy summary, Resume, Delete. Resume
        # and Delete ride only while the mode is off — one ongoing run
        # stays primary, and a live run is never deletable.
        action_buttons = [rich_button(
            t("gateway.autonomous.sessions_copy_summary"), kind="copy",
            value=overview)]
        if not active and run_id:
            action_buttons.append(rich_button(
                "Resume", kind="callback",
                value=f"hm:autonomous:resume:{run_id}", style="primary"))
            action_buttons.append(rich_button(
                "Delete", kind="callback",
                value=f"hm:autonomous:delete:{run_id}", style="danger"))
        body.append(rich_button_row(action_buttons))
        parts.append(
            f"<details><summary>{summary_html}</summary>"
            + "\n".join(body) + "</details>")
    if pages > 1:
        parts.append("<hr>")
        parts.append(rich_page_footer(page, pages, total))
        pager: list[str] = []
        if page > 1:
            pager.append(rich_button("‹ Previous", kind="callback",
                                     value=f"hm:autonomous:sessions:{page - 1}",
                                     style="primary"))
        if page < pages:
            pager.append(rich_button("Next ›", kind="callback",
                                     value=f"hm:autonomous:sessions:{page + 1}",
                                     style="primary"))
        if pager:
            parts.append(rich_button_row(pager))
    if not active:
        parts.append(f"<p><i>{_esc(t('gateway.autonomous.sessions_resume_hint'))}</i></p>")
    parts.append(rich_button_row([rich_button(
        t("gateway.autonomous.sessions_back"), kind="callback",
        value="hm:autonomous:status", style="link")]))
    return "\n".join(parts)


async def _edit_sessions_page(adapter: Any, event: Any, source: Any,
                              page: int = 1) -> bool:
    """Re-render the tapped message in place as a sessions page."""
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    return await _edit_card_html(
        adapter, source.chat_id, getattr(event, "message_id", 0),
        _sessions_page_html(home, page))


async def _handle_autonomous_sessions(adapter: Any, event: Any, source: Any,
                                      data: str) -> bool:
    """Sessions tap / pager: edit the same message to the sessions page."""
    from agent.i18n import t

    if not _autonomous_callback_allowed(adapter, source):
        await _deny_loudly(event)
        return True
    page = 1
    if data.startswith("hm:autonomous:sessions:"):
        try:
            page = int(data[len("hm:autonomous:sessions:"):])
        except ValueError:
            return False
    if not await _edit_sessions_page(adapter, event, source, page):
        await event.answer(t("gateway.autonomous.limits_open_failed"))
    return True


async def _handle_autonomous_status(adapter: Any, event: Any, source: Any) -> bool:
    """Back tap: edit the same message back to the status card."""
    from agent.i18n import t

    if not _autonomous_callback_allowed(adapter, source):
        await _deny_loudly(event)
        return True
    if not await _edit_status_card(adapter, event, source):
        await event.answer(t("gateway.autonomous.limits_open_failed"))
    return True


# --- Autonomous limits menu (backbone §5) -----------------------------------
# Short key -> (dotted config key, label, presets, minimum). The fallback key
# is a toggle, not a preset list. Presets are the only values the menu writes;
# every write lands in the profile config.yaml and the next run enforces it
# (the getters below re-read the file on every call, so no restart is needed).
_LIMIT_DEFS = {
    "maxc": ("delegation.max_concurrent_children", "Max workers", (2, 4, 6, 10), 1),
    "permodel": ("delegation.max_concurrent_per_model", "Per-model cap", (1, 2, 4), 1),
    "stagger": ("delegation.spawn_stagger_seconds", "Spawn stagger", (0, 5, 10), 0),
    "runbudget": ("autonomous.max_tokens_per_run", "Run budget", (250000, 1000000, 5000000, 0), 0),
    "workerbudget": ("delegation.max_tokens_per_worker", "Worker budget", (100000, 250000, 1000000, 0), 0),
    "workeriter": ("delegation.max_iterations_per_worker", "Worker iterations", (50, 100, 250, 500, 1000, 2000, 0), 0),
    "retries": ("delegation.rate_limit_max_retries", "Retries", (1, 2, 3), 1),
    "fallback": ("delegation.worker_model_fallback", "Model fallback", (), 0),
}
#: One short hint line per limit, on the page the setting lives on — plain
#: words, one sentence, next to the setting it explains. The workeriter
#: "unlimited" preset is 0 (stored as 0; the readers treat 0 as unset).
_LIMIT_HINTS = {
    "maxc": "How many workers run at the same time.",
    "permodel": "How many workers may share one model at once.",
    "stagger": "Seconds to wait between starting each worker.",
    "runbudget": "Total tokens for the whole run; off means no cap.",
    "workerbudget": "Tokens one worker may use; off means no cap — only the run budget or the iteration cap stops it.",
    "workeriter": "Tool rounds one worker may run; unlimited means no cap in autonomous mode.",
    "retries": "How many times a worker retries a rate-limited call before giving up.",
    "fallback": "On: when a worker's model 429s or 5xxs, it retries on the next worker model instead of the same one.",
}
_LIMIT_PAGES = {
    "workers": ("workerbudget", "workeriter", "fallback"),
    "swarm": ("maxc", "permodel", "stagger", "runbudget", "retries"),
}


def _limit_display(short: str, value: Any) -> str:
    """Render a limit value the way the menu shows it."""
    if value is None:
        return "unlimited" if short == "workeriter" else "default"
    if short == "workeriter" and not value:
        return "unlimited"
    if short in {"runbudget", "workerbudget"}:
        if not value:
            return "off"
        if value >= 1_000_000:
            return f"{value // 1_000_000}M"
        if value >= 1000:
            return f"{value // 1000}k"
        return str(value)
    if short == "stagger":
        return f"{int(value)}s"
    if short == "fallback":
        return "on" if value else "off"
    return str(value)


def _current_limits(home: str | Path) -> dict:
    """Effective backbone limits, read through the same getters enforcement uses."""
    from gateway import autonomous_state
    from tools.delegate_tool_config import (
        _get_max_concurrent_children, _get_max_concurrent_per_model,
        _get_max_iterations_per_worker, _get_max_tokens_per_worker,
        _get_rate_limit_max_retries, _get_spawn_stagger_seconds,
        _get_worker_model_fallback, _get_worker_models)
    return {
        "maxc": _get_max_concurrent_children(),
        "permodel": _get_max_concurrent_per_model(),
        "stagger": _get_spawn_stagger_seconds(),
        "runbudget": autonomous_state._config_max_tokens_per_run(home),
        "workerbudget": _get_max_tokens_per_worker(),
        # Unset reads as unlimited (0): the menu's "off" preset for
        # worker iterations, and the effective cap when the key is absent.
        "workeriter": _get_max_iterations_per_worker() or 0,
        "retries": _get_rate_limit_max_retries(),
        "fallback": _get_worker_model_fallback(),
        "models": _get_worker_models(),
    }


def _set_backbone_config(home: str | Path, updates: dict[str, Any]) -> None:
    """Write dotted keys into the profile config.yaml, atomically.

    Same write path as :func:`_set_autonomous_mode`: refuse an unreadable
    file rather than replacing it with a partial dict, then write atomically
    so the config cache invalidates on the next read.
    """
    from hermes_cli.config import (
        atomic_config_write, require_readable_config_before_write)

    path = Path(home) / "config.yaml"
    cfg = require_readable_config_before_write(path)
    for dotted, value in updates.items():
        parts = dotted.split(".")
        node = cfg
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    atomic_config_write(path, cfg)


def _current_coordinator_model(home: str | Path) -> Optional[str]:
    """Model the coordinator actually runs: the most recent run's stamped model.

    The record's stamped model is a ``{"model", "provider"}`` dict (see
    ``turn_model_from_agent``); the plain id is what the menu shows. A bare
    string still reads, for records stamped before the dict shape.
    """
    from gateway import autonomous_state

    runs = autonomous_state.recent_runs(home, 1)
    if runs:
        model = (runs[0] or {}).get("model")
        if isinstance(model, dict):
            model = model.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _limits_menu_body(home: str | Path, page: str) -> str:
    """Rich HTML for one limits-menu page, buttons in-body.

    Same rich language as the business cards: the title is a heading, a
    table lists every limit with its current value in a block code, each
    preset is a rich button labelled with just the value — the current
    one first, checked (success) style, the rest uncolored link style,
    so the selection is unmistakable — and one italic line explains what
    the page controls. No keyboard buttons: the big grey blocks go. The
    callback payloads are unchanged (``hm:autonomous:set:*`` /
    ``hm:autonomous:models:*`` / ``hm:autonomous:limits:*``), so only the
    rendering changes, never the actions. Titles are plain headings,
    never pre-tagged ``<b>``: the body is direct HTML, not
    ``lines_to_rich_html`` (which escapes raw tags into visible text).
    """
    from html import escape as _esc

    from agent.i18n import t
    from gateway import autonomous_state
    from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

    limits = _current_limits(home)
    models = limits["models"]
    legend = "<p><i>✓ Green is the current value — tap another to change it.</i></p>"

    def preset_row(short: str) -> str:
        _dotted, _label, presets, _minimum = _LIMIT_DEFS[short]
        current = limits[short]
        ordered = [current] + [preset for preset in presets if preset != current]
        return rich_button_row([
            rich_button(
                f"✓ {_limit_display(short, preset)}" if preset == current
                else _limit_display(short, preset), kind="callback",
                value=f"hm:autonomous:set:{short}:{preset}",
                style="success" if preset == current else "link")
            for preset in ordered])

    def fallback_row() -> str:
        current = bool(limits["fallback"])
        return rich_button_row([
            rich_button(
                f"✓ {'on' if target else 'off'}" if target == current
                else ("on" if target else "off"), kind="callback",
                value=f"hm:autonomous:set:fallback:{'on' if target else 'off'}",
                style="success" if target == current else "link")
            for target in (current, not current)])

    def limits_table(shorts: list[str]) -> str:
        rows = "".join(
            f"<tr><td>{_esc(_LIMIT_DEFS[short][1])}</td>"
            f"<td><code>{_esc(_limit_display(short, limits[short]))}</code></td></tr>"
            for short in shorts)
        return ("<table><tbody><tr><th>Limit</th><th>Current</th></tr>"
                + rows + "</tbody></table>")

    def limit_block(short: str) -> list[str]:
        row = fallback_row() if short == "fallback" else preset_row(short)
        lines = [f"<p>{_esc(_LIMIT_DEFS[short][1])}: "
                 f"<code>{_esc(_limit_display(short, limits[short]))}</code></p>"]
        hint = _LIMIT_HINTS.get(short)
        if hint:
            lines.append(f"<p><i>{_esc(hint)}</i></p>")
        lines.append(row)
        return lines

    def back_row() -> str:
        return rich_button_row([rich_button(
            t("gateway.autonomous.limits_back"), kind="callback",
            value="hm:autonomous:limits:main", style="link")])

    def back_to_status_row() -> str:
        return rich_button_row([rich_button(
            t("gateway.autonomous.sessions_back"), kind="callback",
            value="hm:autonomous:status", style="link")])

    def models_line() -> str:
        text = (", ".join(models) if models
                else t("gateway.autonomous.limits_inherit"))
        return f"<p>Worker models: <b>{_esc(text)}</b></p>"

    if page == "workers":
        parts = [f"<h1>{_esc(t('gateway.autonomous.limits_workers_title'))}</h1>",
                 f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p>",
                 limits_table(list(_LIMIT_PAGES["workers"])),
                 models_line()]
        current = _current_coordinator_model(home)
        if current is not None and current not in models:
            parts.append(rich_button_row([rich_button(
                f"Add current model ({current})", kind="callback",
                value="hm:autonomous:models:add", style="primary")]))
        if models:
            model_buttons = [rich_button(
                "Clear worker models", kind="callback",
                value="hm:autonomous:models:clear", style="primary")]
            for index, model in enumerate(models):
                model_buttons.append(rich_button(
                    f"✕ {model}", kind="callback",
                    value=f"hm:autonomous:models:rm:{index}", style="primary"))
            for start in range(0, len(model_buttons), 4):
                parts.append(rich_button_row(model_buttons[start:start + 4]))
        for short in _LIMIT_PAGES["workers"]:
            parts.extend(limit_block(short))
        parts.append(legend)
        parts.append(back_row())
        return "".join(parts)

    if page == "swarm":
        parts = [f"<h1>{_esc(t('gateway.autonomous.limits_swarm_title'))}</h1>",
                 f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p>",
                 limits_table(list(_LIMIT_PAGES["swarm"]))]
        for short in _LIMIT_PAGES["swarm"]:
            parts.extend(limit_block(short))
        parts.append(legend)
        parts.append(back_row())
        return "".join(parts)

    parts = [f"<h1>{_esc(t('gateway.autonomous.limits_title'))}</h1>",
             f"<details><summary>About these limits</summary>"
             f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p></details>",
             limits_table(list(_LIMIT_DEFS))]
    parts.append(models_line())
    parts.append(legend)
    parts.append(rich_button_row([
        rich_button("Sub-agent limits", kind="callback",
                    value="hm:autonomous:limits:workers", style="primary"),
        rich_button("Swarm limits", kind="callback",
                    value="hm:autonomous:limits:swarm", style="primary")]))
    if autonomous_state.is_active(home):
        parts.append(rich_button_row([rich_button(
            "Kill run", kind="callback",
            value="hm:autonomous:limits:kill", style="danger")]))
    parts.append(back_to_status_row())
    return "".join(parts)


async def send_limits_menu(adapter: Any, source: Any, *,
                           page: str = "main", reply_to: Any = None) -> Optional[str]:
    """Send the limits menu as a new card; the buttons ride in-body, so no keyboard."""
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    html = _limits_menu_body(home, page)
    send_card = getattr(adapter, "send_command_card", None)
    if not callable(send_card):
        return None
    try:
        return await send_card(
            source=source, html=html, buttons=(), reply_to=reply_to)
    except Exception:
        logger.debug("Autonomous limits menu send failed", exc_info=True)
        return None


async def _edit_limits_menu(adapter: Any, event: Any, source: Any, page: str) -> bool:
    """Re-render the tapped limits card in place; False when the edit is unavailable."""
    from .mtproto_rich import edit_request
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    html = _limits_menu_body(home, page)
    request = edit_request(adapter._peer_for(source.chat_id),
                           getattr(event, "message_id", 0),
                           html, rich_format="html")
    try:
        await adapter._mtproto_rich_request(request)
    except Exception:
        logger.debug("Autonomous limits menu edit failed", exc_info=True)
        return False
    return True


def _limits_page_for(short: str) -> str:
    """Which menu page a limit lives on (the main page shows every value)."""
    for page, keys in _LIMIT_PAGES.items():
        if short in keys:
            return page
    return "main"


async def _deny_loudly(event: Any) -> None:
    """Answer a denied autonomous tap with the reason — never silently.

    A tap that cannot run says why in a toast; nothing dies quietly.
    """
    from agent.i18n import t

    await event.answer(t("gateway.autonomous.not_owner"))


def _autonomous_callback_allowed(adapter: Any, source: Any) -> bool:
    """Owner-DM gate shared by the autonomous menu callbacks (same as Disable)."""
    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        return False
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(actor_id, source))
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies silently
        return False


async def _handle_limits_menu_callback(adapter: Any, event: Any, source: Any,
                                      data: str) -> bool:
    """Owner-DM-only dispatcher for the limits menu and its set/model actions."""
    if not _autonomous_callback_allowed(adapter, source):
        await _deny_loudly(event)
        return True
    if data == "hm:autonomous:limits":
        # The Limits tap edits the current message in place; a new card
        # is only the fallback when the edit is unavailable.
        if not await _edit_limits_menu(adapter, event, source, "main"):
            await send_limits_menu(adapter, source)
        return True
    if data.startswith("hm:autonomous:limits:"):
        return await _handle_limits_page(adapter, event, source, data)
    if data.startswith("hm:autonomous:set:"):
        return await _handle_limits_set(adapter, event, source, data)
    if data.startswith("hm:autonomous:models:"):
        return await _handle_limits_models(adapter, event, source, data)
    return False


async def _handle_limits_page(adapter: Any, event: Any, source: Any,
                              data: str) -> bool:
    """Navigate the limits menu (page tap) or fire the Kill action."""
    from agent.i18n import t

    rest = data[len("hm:autonomous:limits:"):]
    if rest == "kill":
        await event.answer()
        await _redispatch_kill(adapter, event, source)
        return True
    if rest in ("main", "workers", "swarm"):
        if not await _edit_limits_menu(adapter, event, source, rest):
            await event.answer(t("gateway.autonomous.limits_open_failed"))
        return True
    return False


async def _handle_limits_set(adapter: Any, event: Any, source: Any,
                             data: str) -> bool:
    """Write one preset value from the menu, then re-render its page."""
    from agent.i18n import t
    from hermes_constants import get_routing_process_hermes_home

    rest = data[len("hm:autonomous:set:"):]
    parts = rest.split(":", 1)
    if len(parts) != 2 or parts[0] not in _LIMIT_DEFS:
        return False
    short, raw = parts
    dotted, label, _presets, minimum = _LIMIT_DEFS[short]
    if short == "fallback":
        if raw not in ("on", "off"):
            return False
        value: Any = raw == "on"
    else:
        try:
            value = int(raw)
        except ValueError:
            return False
        if value < minimum:
            await event.answer(
                t("gateway.autonomous.limits_invalid",
                  label=label, minimum=minimum))
            return True
    _set_backbone_config(get_routing_process_hermes_home(), {dotted: value})
    if not await _edit_limits_menu(adapter, event, source,
                                   _limits_page_for(short)):
        await event.answer(t("gateway.autonomous.limits_set",
                             label=label,
                             value=_limit_display(short, value)))
    return True


async def _handle_limits_models(adapter: Any, event: Any, source: Any,
                               data: str) -> bool:
    """Add / clear / remove one worker model, then re-render the workers page."""
    from agent.i18n import t
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    rest = data[len("hm:autonomous:models:"):]
    models = list(_current_limits(home)["models"])
    if rest == "add":
        current = _current_coordinator_model(home)
        if current is None:
            await event.answer(t("gateway.autonomous.limits_no_model"))
            return True
        if current not in models:
            models.append(current)
            _set_backbone_config(home, {"delegation.worker_models": models})
    elif rest == "clear":
        if models:
            _set_backbone_config(home, {"delegation.worker_models": []})
    elif rest.startswith("rm:"):
        try:
            index = int(rest[len("rm:"):])
        except ValueError:
            return False
        if 0 <= index < len(models):
            del models[index]
            _set_backbone_config(home, {"delegation.worker_models": models})
    else:
        return False
    if not await _edit_limits_menu(adapter, event, source, "workers"):
        await event.answer(t("gateway.autonomous.limits_set",
                             label="Worker models",
                             value=", ".join(models) or "inherit parent"))
    return True


async def _redispatch_kill(adapter: Any, event: Any, source: Any) -> None:
    """Re-dispatch ``/autonomous kill`` as an inbound message.

    Same pattern as the Resume button: the kill handler's own gates still
    decide (idle and mid-run paths), so a forged callback is no stronger
    than typing the command.
    """
    from gateway.platforms.event import MessageEvent, MessageType
    handler = getattr(adapter, "handle_message", None)
    if not callable(handler):
        return
    await handler(MessageEvent(
        text="/autonomous kill",
        message_type=MessageType.COMMAND,
        source=source,
        user_id=getattr(source, "user_id", None),
        user_name=getattr(source, "user_name", None),
        message_id=str(getattr(event, "message_id", None) or ""),
        metadata={"thread_id": source.thread_id}
        if getattr(source, "thread_id", None) else {},
    ))


def _ago_text(iso: str) -> str:
    """Compact relative time for a run's ``last_step_at`` ("5m ago", "3h ago").

    Empty when the timestamp is missing or unparsable — the run line then
    carries no time segment rather than a made-up one.
    """
    try:
        when = datetime.fromisoformat(str(iso or ""))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _session_key_for_source(source: Any) -> str:
    """Session key for a callback tap's source, via the single source of truth.

    Mirrors the runner's fallback in ``GatewayRunner._session_key_for_source``:
    the pinned identity's session-key profile wins, else the source's own
    profile. A DM tap has no thread to normalize, so no topic recovery
    applies here.
    """
    from gateway.session import build_session_key
    from gateway.session_identity import identity_of

    profile = None
    identity = identity_of(source)
    if identity is not None:
        profile = identity.session_key_profile
    elif getattr(source, "profile", None):
        profile = str(source.profile)
    return build_session_key(source, profile=profile)


async def send_enable_card(adapter: Any, source: Any, *, reply_to: Any = None) -> Optional[str]:
    """Send the enable confirmation card; the sent message id, or None.

    The card body is Rich HTML, so the green tag rides as an in-body
    button row atop it; the Disable button is an in-body rich button
    (``hm:autonomous:disable``) carried by the same send. Shared by the
    ``/autonomous enable`` handler and the resume-ask choice taps.
    """
    from agent.i18n import t
    from plugins.platforms.telegram.command_cards import lines_to_rich_html
    from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

    tag_row = rich_button_row(
        [rich_button("autonomous", kind="disabled", style="success")])
    html = (tag_row + "\n" + lines_to_rich_html(
        t("gateway.autonomous.enabled_title"), [t("gateway.autonomous.enabled_body")])
        + "\n" + rich_button_row([rich_button(
            "Disable", kind="callback",
            value="hm:autonomous:disable", style="danger")]))
    send_card = getattr(adapter, "send_command_card", None)
    if not callable(send_card):
        return None
    try:
        return await send_card(
            source=source, html=html, buttons=(), reply_to=reply_to)
    except Exception:
        logger.debug("Autonomous enable card send failed", exc_info=True)
        return None


async def handle_autonomous_callback(adapter: Any, event: Any, source: Any, data: str) -> bool:
    """Handle ``hm:autonomous:*``. True when the callback was handled."""
    text = str(data or "")
    if text == "hm:autonomous:enable":
        return await _handle_autonomous_enable(adapter, event, source)
    if text == "hm:autonomous:disable":
        return await _handle_autonomous_disable(adapter, event, source)
    if text == "hm:autonomous:status":
        return await _handle_autonomous_status(adapter, event, source)
    if text == "hm:autonomous:sessions" or text.startswith("hm:autonomous:sessions:"):
        return await _handle_autonomous_sessions(adapter, event, source, text)
    if text.startswith("hm:autonomous:resume:"):
        return await _handle_autonomous_choice(
            adapter, event, source, text[len("hm:autonomous:resume:"):], fresh=False)
    if text.startswith("hm:autonomous:delete:"):
        return await _handle_autonomous_delete(
            adapter, event, source, text[len("hm:autonomous:delete:"):])
    if text.startswith("hm:autonomous:fresh:"):
        return await _handle_autonomous_choice(
            adapter, event, source, text[len("hm:autonomous:fresh:"):], fresh=True)
    if (text == "hm:autonomous:limits"
            or text.startswith("hm:autonomous:limits:")
            or text.startswith("hm:autonomous:set:")
            or text.startswith("hm:autonomous:models:")):
        return await _handle_limits_menu_callback(adapter, event, source, text)
    return False


async def _handle_autonomous_delete(adapter: Any, event: Any, source: Any,
                                    run_id: str) -> bool:
    """Delete tap on a sessions-page run: remove the record, re-render
    the page in place, answer that it is gone. The live run is refused —
    the store raises and the tap answers why — and an unknown id just
    re-renders, so a double tap never errors."""
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    if not _autonomous_callback_allowed(adapter, source):
        await _deny_loudly(event)
        return True
    home = get_routing_process_hermes_home()
    try:
        autonomous_state.delete_run(home, run_id)
    except ValueError:
        await event.answer(t("gateway.autonomous.sessions_delete_live"))
        return True
    if not await _edit_sessions_page(adapter, event, source, 1):
        await event.answer(t("gateway.autonomous.limits_open_failed"))
        return True
    await event.answer(t("gateway.autonomous.sessions_deleted"))
    return True


async def _handle_autonomous_disable(adapter: Any, event: Any, source: Any) -> bool:
    """Disable tap: unpin, then edit the tapped message in place to the
    disabled status card — the badge on it shows the state changed, and
    the card carries the last run's overview. The pinned message is
    unpinned, never deleted. A denied tap answers its reason."""
    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await _deny_loudly(event)
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await _deny_loudly(event)
        return True
    try:
        if not checker(actor_id, source):
            await _deny_loudly(event)
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies with a toast
        await _deny_loudly(event)
        return True

    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    pinned = autonomous_state.disable(home, stopped_by="autonomous_disable")
    await unpin_pinned(adapter, pinned)
    clear_master_setting(home)
    if not await _edit_status_card(adapter, event, source):
        logger.debug("Autonomous disable card edit failed", exc_info=True)
    await event.answer("Autonomous mode disabled.")
    return True


async def _handle_autonomous_enable(adapter: Any, event: Any, source: Any) -> bool:
    """Enable tap on the status card: fresh enable, no resume ask.

    Same gates as Disable (owner DM only, answering its reason when
    denied). The status card already offers Resume Previous beside
    Enable, so this tap always starts a fresh run. The master setting
    flips on first — a failed write fails closed with a toast and starts
    nothing. The status card is deleted, the enable card is sent +
    pinned, and the run attaches to the tapper's DM session.
    """
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await _deny_loudly(event)
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await _deny_loudly(event)
        return True
    try:
        if not checker(actor_id, source):
            await _deny_loudly(event)
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies with a toast
        await _deny_loudly(event)
        return True

    home = get_routing_process_hermes_home()
    if autonomous_state.is_active(home):
        await event.answer(t("gateway.autonomous.status_already_on"))
        return True
    try:
        from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

        TelegramPersonalSettingsUI._set_autonomous_mode(home, True)
    except Exception:
        logger.warning("Could not persist autonomous_mode setting on enable tap", exc_info=True)
        await event.answer(t("gateway.autonomous.save_failed"))
        return True

    try:
        record = autonomous_state.create_run(home, _session_key_for_source(source), "")
        autonomous_state.start_run(home, record["run_id"])
    except Exception:
        logger.warning("Could not start the autonomous run from the enable tap", exc_info=True)
        await event.answer(t("gateway.autonomous.start_failed"))
        return True

    # The status card has served its purpose; delete it best-effort.
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        try:
            await adapter.delete_message(str(source.chat_id), str(message_id))
        except Exception:
            logger.debug("Autonomous status-card delete failed", exc_info=True)
    card_id = await send_enable_card(adapter, source)
    if card_id is not None:
        try:
            await adapter.pin_chat_message(chat_id=source.chat_id, message_id=card_id)
        except Exception:
            logger.debug("Autonomous enable pin failed", exc_info=True)
        autonomous_state.set_active(home, True, run_id=record["run_id"],
                                    pinned_message_id=card_id, chat_id=source.chat_id)
        await event.answer()
    else:
        await event.answer(t("gateway.autonomous.status_on"))
    return True


async def _handle_autonomous_choice(adapter: Any, event: Any, source: Any,
                                    run_id: str, *, fresh: bool) -> bool:
    """Resume-ask choice: Resume re-opens the old run, Start fresh starts a new one.

    Same gates as Disable (owner DM only, answering its reason when
    denied). The master setting flips on first — a failed write fails
    closed with a toast and starts nothing. The ask message is deleted,
    the enable card is sent + pinned, and the run record is the session
    source: the old record carries the owner's DM session key, so the
    resumed (or fresh) run attaches to the same session. A missing or
    no-longer-resumable record answers that the run is unavailable
    instead of starting anything. A ``done`` run re-opens as a
    continuation of its goal; a killed run never re-opens. While a run
    is actually running, no switch happens — one ongoing run stays
    primary.
    """
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await _deny_loudly(event)
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await _deny_loudly(event)
        return True
    try:
        if not checker(actor_id, source):
            await _deny_loudly(event)
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies with a toast
        await _deny_loudly(event)
        return True

    home = get_routing_process_hermes_home()
    state = autonomous_state.load_state(home)
    current_run_id = state.get("current_run_id")
    current = (autonomous_state.load_run(home, current_run_id)
               if isinstance(current_run_id, str) and current_run_id else None)
    if current is not None and str(current.get("state")) == "running":
        await event.answer(t("gateway.autonomous.resume_blocked_active"))
        return True
    record = autonomous_state.load_run(home, run_id)
    if record is None or str(record.get("state")) not in {"stopped", "failed", "needs_input", "done"}:
        await event.answer(t("gateway.autonomous.resume_unavailable"))
        return True
    if not str(record.get("session_key") or ""):
        # A record without a session cannot attach a run to the owner's DM.
        await event.answer(t("gateway.autonomous.start_failed"))
        return True
    try:
        from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

        TelegramPersonalSettingsUI._set_autonomous_mode(home, True)
    except Exception:
        logger.warning("Could not persist autonomous_mode setting on resume choice", exc_info=True)
        await event.answer(t("gateway.autonomous.save_failed"))
        return True

    try:
        if fresh:
            new_record = autonomous_state.create_run(
                home, str(record.get("session_key") or ""), str(record.get("goal") or ""))
            autonomous_state.start_run(home, new_record["run_id"])
        else:
            new_record = autonomous_state.reopen_run(home, run_id)
    except Exception:
        logger.warning("Could not start the autonomous run from the resume choice", exc_info=True)
        await event.answer(t("gateway.autonomous.start_failed"))
        return True

    # The ask message has served its purpose; delete it best-effort.
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        try:
            await adapter.delete_message(str(source.chat_id), str(message_id))
        except Exception:
            logger.debug("Autonomous resume-ask delete failed", exc_info=True)
    card_id = await send_enable_card(adapter, source)
    if card_id is not None:
        try:
            await adapter.pin_chat_message(chat_id=source.chat_id, message_id=card_id)
        except Exception:
            logger.debug("Autonomous resume pin failed", exc_info=True)
        autonomous_state.set_active(home, True, run_id=new_record["run_id"],
                                    pinned_message_id=card_id, chat_id=source.chat_id)
        await event.answer()
    else:
        await event.answer(t("gateway.autonomous.status_on"))
    return True
