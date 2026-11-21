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


async def send_status_card(adapter: Any, source: Any, *, reply_to: Any = None) -> Optional[str]:
    """Send the bare-/autonomous status card; the sent message id, or None.

    Minimal rich card: the green autonomous tag row when the mode is on
    (never when it is off — the pill means the mode is enabled), a status
    line (``Autonomous mode •`` + an enabled/disabled badge), one line on
    what the mode is and one on how it works, the last sessions with
    per-run token burn and a total, and rich in-body buttons — Enable
    (fresh) / Resume when off, Disable when on. The plain status text is
    the fallback when the card cannot be sent.
    """
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home
    from plugins.platforms.telegram.command_cards import lines_to_rich_html
    from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

    home = get_routing_process_hermes_home()
    active = autonomous_state.is_active(home)
    state = autonomous_state.load_state(home)
    current_run_id = state.get("current_run_id")
    runs = autonomous_state.recent_runs(home, limit=3)
    if active:
        # The current run failed or stopped under the live flag: the mode
        # stays on (the flag is the owner's switch), so Resume lives here
        # too — the owner must not have to Disable first to get the run
        # back. A killed run is never resumable, whatever its state; a
        # waiting run auto-resumes on the next message, so it needs no
        # button either.
        current = (autonomous_state.load_run(home, current_run_id)
                   if isinstance(current_run_id, str) and current_run_id else None)
        resumable = (current if current is not None
                     and str(current.get("state")) in {"stopped", "failed"}
                     and not current.get("killed") else None)
    else:
        resumable = autonomous_state.find_resumable_run(home)

    # The green pill means the mode is enabled, so it only renders while
    # the mode is on. When off, the card starts at the status line, whose
    # red disabled badge carries the state on its own.
    tag_row = (
        rich_button_row(
            [rich_button("autonomous", kind="disabled", style="success")])
        if active else "")
    badge = rich_button(
        "enabled" if active else "disabled", kind="callback",
        value="badge:enabled" if active else "badge:disabled",
        style="success" if active else "danger")
    status_line = f"<p><b>Autonomous mode</b> • {badge}</p>"
    lines = [t("gateway.autonomous.status_what"),
             t("gateway.autonomous.status_how"), ""]
    lines.append(f"**{t('gateway.autonomous.status_sessions')}**")
    total_tokens = 0
    if runs:
        for run in runs:
            tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
            total_tokens += tokens
            steps = int(run.get("steps") or 0)
            extra = ""
            if (isinstance(current_run_id, str) and current_run_id
                    and str(run.get("run_id")) == current_run_id
                    and str(run.get("last_tool") or "")):
                extra = t("gateway.autonomous.status_last_tool",
                          tool=str(run.get("last_tool")))
            if (resumable is not None
                    and str(run.get("run_id")) == str(resumable.get("run_id"))):
                # The Resume button re-opens this record; say so on its
                # line, or the button names no run at all.
                extra += t("gateway.autonomous.status_resumable")
            ago_text = _ago_text(str(run.get("last_step_at") or ""))
            ago = f" · {ago_text}" if ago_text else ""
            lines.append("- " + t(
                "gateway.autonomous.status_run",
                goal=str(run.get("goal") or "") or "(no goal)",
                state=_status_card_state_word(run),
                steps=steps,
                steps_word="step" if steps == 1 else "steps",
                tokens=f"{tokens:,}",
                extra=extra,
                ago=ago,
            ))
        lines.append("")
        lines.append(t("gateway.autonomous.status_total",
                       tokens=f"{total_tokens:,}", count=len(runs),
                       sessions_word="session" if len(runs) == 1 else "sessions"))
        if any(not str(run.get("goal") or "") for run in runs):
            # A goal only exists when it was typed after /autonomous
            # enable; the Enable button starts a run without one.
            lines.append(t("gateway.autonomous.status_no_goal_hint"))
    else:
        lines.append(t("gateway.autonomous.status_no_sessions"))
    head = "\n".join(part for part in (tag_row, status_line) if part)
    html = head + "\n" + lines_to_rich_html("", lines)
    if active:
        actions = [rich_button("Disable", kind="callback",
                               value="hm:autonomous:disable", style="danger")]
        if resumable is not None:
            actions.append(rich_button(
                "Resume", kind="callback",
                value=f"hm:autonomous:resume:{resumable['run_id']}", style="primary"))
        actions.append(rich_button("Limits", kind="callback",
                                   value="hm:autonomous:limits", style="primary"))
    elif resumable is not None:
        actions = [rich_button("Enable autonomous", kind="callback",
                               value="hm:autonomous:enable", style="primary"),
                   rich_button("Resume", kind="callback",
                               value=f"hm:autonomous:resume:{resumable['run_id']}",
                               style="primary"),
                   rich_button("Limits", kind="callback",
                               value="hm:autonomous:limits", style="primary")]
    else:
        actions = [rich_button("Enable autonomous", kind="callback",
                               value="hm:autonomous:enable", style="primary"),
                   rich_button("Limits", kind="callback",
                               value="hm:autonomous:limits", style="primary")]
    html += "\n" + rich_button_row(actions)
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
    "workeriter": ("delegation.max_iterations_per_worker", "Worker iterations", (50, 100, 250, 500), 1),
    "retries": ("delegation.rate_limit_max_retries", "Retries", (1, 2, 3), 1),
    "fallback": ("delegation.worker_model_fallback", "Model fallback", (), 0),
}
_LIMIT_PAGES = {
    "workers": ("workerbudget", "workeriter", "fallback"),
    "swarm": ("maxc", "permodel", "stagger", "runbudget", "retries"),
}


def _limit_display(short: str, value: Any) -> str:
    """Render a limit value the way the menu shows it."""
    if value is None:
        return "default"
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
        "workeriter": _get_max_iterations_per_worker(),
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

    Same rich language as the business cards: the title is a heading, each
    limit shows its current value in bold, each preset is a rich button
    labelled with just the value — the current one carries the selected
    (success) style, the rest are plain — and one italic line explains
    what the page controls. No keyboard buttons: the big grey blocks go.
    The callback payloads are unchanged (``hm:autonomous:set:*`` /
    ``hm:autonomous:models:*`` / ``hm:autonomous:limits:*``), so only the
    rendering changes, never the actions. Titles are plain headings, never
    pre-tagged ``<b>``: the body is direct HTML, not ``lines_to_rich_html``
    (which escapes raw tags into visible text).
    """
    from html import escape as _esc

    from agent.i18n import t
    from gateway import autonomous_state
    from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

    limits = _current_limits(home)
    models = limits["models"]
    legend = "<p><i>Green marks the current value — tap another to change it.</i></p>"

    def preset_row(short: str) -> str:
        _dotted, _label, presets, _minimum = _LIMIT_DEFS[short]
        current = limits[short]
        return rich_button_row([
            rich_button(
                _limit_display(short, preset), kind="callback",
                value=f"hm:autonomous:set:{short}:{preset}",
                style="success" if preset == current else "primary")
            for preset in presets])

    def fallback_row() -> str:
        current = bool(limits["fallback"])
        return rich_button_row([
            rich_button(
                "on" if target else "off", kind="callback",
                value=f"hm:autonomous:set:fallback:{'on' if target else 'off'}",
                style="success" if target == current else "primary")
            for target in (True, False)])

    def limit_block(short: str) -> list[str]:
        row = fallback_row() if short == "fallback" else preset_row(short)
        return [f"<p>{_esc(_LIMIT_DEFS[short][1])}: "
                f"<b>{_esc(_limit_display(short, limits[short]))}</b></p>", row]

    def back_row() -> str:
        return rich_button_row([rich_button(
            t("gateway.autonomous.limits_back"), kind="callback",
            value="hm:autonomous:limits:main", style="link")])

    def models_line() -> str:
        text = (", ".join(models) if models
                else t("gateway.autonomous.limits_inherit"))
        return f"<p>Worker models: <b>{_esc(text)}</b></p>"

    if page == "workers":
        parts = [f"<h1>{_esc(t('gateway.autonomous.limits_workers_title'))}</h1>",
                 f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p>",
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
                 f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p>"]
        for short in _LIMIT_PAGES["swarm"]:
            parts.extend(limit_block(short))
        parts.append(legend)
        parts.append(back_row())
        return "".join(parts)

    parts = [f"<h1>{_esc(t('gateway.autonomous.limits_title'))}</h1>",
             f"<p>{_esc(t('gateway.autonomous.limits_intro'))}</p>"]
    for short in _LIMIT_DEFS:
        parts.append(f"<p>{_esc(_LIMIT_DEFS[short][1])}: "
                     f"<b>{_esc(_limit_display(short, limits[short]))}</b></p>")
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
        await event.answer()
        return True
    if data == "hm:autonomous:limits":
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
    if text.startswith("hm:autonomous:resume:"):
        return await _handle_autonomous_choice(
            adapter, event, source, text[len("hm:autonomous:resume:"):], fresh=False)
    if text.startswith("hm:autonomous:fresh:"):
        return await _handle_autonomous_choice(
            adapter, event, source, text[len("hm:autonomous:fresh:"):], fresh=True)
    if (text == "hm:autonomous:limits"
            or text.startswith("hm:autonomous:limits:")
            or text.startswith("hm:autonomous:set:")
            or text.startswith("hm:autonomous:models:")):
        return await _handle_limits_menu_callback(adapter, event, source, text)
    return False


async def _handle_autonomous_disable(adapter: Any, event: Any, source: Any) -> bool:
    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await event.answer()
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await event.answer()
        return True
    try:
        if not checker(actor_id, source):
            await event.answer()
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies silently
        await event.answer()
        return True

    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    home = get_routing_process_hermes_home()
    pinned = autonomous_state.disable(home, stopped_by="autonomous_disable")
    await unpin_and_delete_pinned(adapter, pinned)
    clear_master_setting(home)
    await event.answer("Autonomous mode disabled.")
    return True


async def _handle_autonomous_enable(adapter: Any, event: Any, source: Any) -> bool:
    """Enable tap on the status card: fresh enable, no resume ask.

    Same gates as Disable (owner DM only, silent otherwise). The status card
    already offers Resume beside Enable, so this tap always starts a fresh
    run. The master setting flips on first — a failed write fails closed
    with a toast and starts nothing. The status card is deleted, the enable
    card is sent + pinned, and the run attaches to the tapper's DM session.
    """
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await event.answer()
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await event.answer()
        return True
    try:
        if not checker(actor_id, source):
            await event.answer()
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies silently
        await event.answer()
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

    Same gates as Disable (owner DM only, silent otherwise). The master
    setting flips on first — a failed write fails closed with a toast and
    starts nothing. The ask message is deleted, the enable card is sent +
    pinned, and the run record is the session source: the old record
    carries the owner's DM session key, so the resumed (or fresh) run
    attaches to the same session. A missing or no-longer-resumable record
    answers that the run is unavailable instead of starting anything.
    """
    from agent.i18n import t
    from gateway import autonomous_state
    from hermes_constants import get_routing_process_hermes_home

    actor_id = str(getattr(source, "user_id", "") or "")
    if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
            or str(getattr(source, "chat_id", "")) != actor_id
            or not actor_id):
        await event.answer()
        return True
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        await event.answer()
        return True
    try:
        if not checker(actor_id, source):
            await event.answer()
            return True
    except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies silently
        await event.answer()
        return True

    home = get_routing_process_hermes_home()
    record = autonomous_state.load_run(home, run_id)
    if record is None or str(record.get("state")) not in {"stopped", "failed", "needs_input"}:
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
