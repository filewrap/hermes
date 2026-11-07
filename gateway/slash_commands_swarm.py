"""Gateway ``/swarm`` command: list, inspect, cancel, retry, and view results
of live agents and swarms.

Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``. The
handler resolves by name via ``_command_handler_table``; ``swarm`` is listed
in ``_PLAIN_COMMANDS`` (``gateway/run_busy.py``) with
``busy_policy="dispatch"``, so it dispatches inline while an agent runs. Both
message guards are bypassed that way: guard 1 routes every resolvable
command inline (``should_bypass_active_session``), and guard 2 takes the
plain-handler route for a ``dispatch`` policy. A cancel issued while the
parent turn is blocked on approval therefore reaches the runner instead of
sitting in the pending queue.

Telegram delivery goes through ``TelegramSwarmUI`` (rich cards with token
callbacks); every other platform gets the same snapshot as plain text,
truncated like ``/kanban``. Returning ``None`` after the UI sends means the
card was already delivered (the ``/model`` dashboard precedent).
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.run")


def _plain_text_snapshot(entries: list[dict[str, Any]]) -> str:
    """The ``/swarm`` list for platforms without the Telegram card UI.

    A headed list: one ``- `` row per agent/swarm, worker rows nested beneath
    it, and an italic page footer. (Telegram uses the rich ``TelegramSwarmUI``
    cards instead, so this text never reaches a Telegram card.)
    """
    if not entries:
        return (
            "## Agents and swarms\n\n"
            "No live agents, background delegations, or saved swarms in this chat.\n\n"
            "Agents started with delegate_task appear here while they run; durable "
            "Kanban swarms stay listed after the gateway restarts."
        )
    lines = ["## Agents and swarms", ""]
    for entry in entries[:12]:
        goal = " ".join(str(entry.get("goal") or "").split())
        if len(goal) > 140:
            goal = goal[:137] + "..."
        short_id = str(entry.get("durable_session_id") or entry.get("source_id") or "?")
        status = str(entry.get("status") or "unknown").replace("_", " ")
        row = f"- `{short_id}` · {status} · {entry.get('kind')}"
        if goal:
            row += f" · {goal}"
        lines.append(row)
        for worker in (entry.get("workers") or [])[:4]:
            detail = " ".join(str(worker.get("detail") or "").split())
            worker_row = f"  - {worker.get('label')}: {str(worker.get('status') or '').replace('_', ' ')}"
            if detail:
                worker_row += f" · {detail[:120]}"
            lines.append(worker_row)
    lines.append("")
    if len(entries) > 12:
        lines.append(f"Showing 12 of {len(entries)}.")
        lines.append("")
    lines.append(f"__Page 1 of 1 • total {len(entries)}__")
    return "\n".join(lines)


class GatewaySwarmCommandsMixin:
    """Gateway ``/swarm`` command handler."""

    async def _handle_swarm_command(self, event: MessageEvent) -> str | None:
        """Handle /swarm — Telegram cards, or plain text elsewhere."""
        from gateway.swarm_sessions import collect_swarm_entries

        source = event.source
        args = (event.get_command_args() or "").strip()
        try:
            home = self._resolve_profile_home_for_source(source)
        except Exception:
            logger.warning("Could not resolve profile home for /swarm", exc_info=True)
            home = None
        platform = str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower()
        if platform == "telegram":
            adapter = self._delivery_adapter_for(source)
            if adapter is None:
                return ("## Swarm\n\n"
                        "Swarm controls are unavailable: no Telegram adapter is connected.")
            from plugins.platforms.telegram.swarm_ui import TelegramSwarmUI
            ui = TelegramSwarmUI.for_adapter(adapter)
            await ui.handle_command(event, source, args, reply_to=event.message_id)
            return None
        if home is None:
            return ("## Swarm\n\n"
                    "Swarm status is unavailable: the profile home could not be resolved.")
        try:
            entries = collect_swarm_entries(home, source.user_id, source.chat_id)
        except Exception:
            logger.warning("Could not collect swarm entries for /swarm", exc_info=True)
            entries = []
        if args:
            # Non-Telegram surfaces have no card UI; only the list subcommand
            # is meaningful there, and it ignores its arguments.
            action = args.split(None, 1)[0].lower()
            if action not in {"", "status", "list"}:
                return ("## Swarm\n\n"
                        "On this platform /swarm only lists live agents and swarms. "
                        "Cancel, retry, and detail controls live on Telegram.")
        output = _plain_text_snapshot(entries)
        if len(output) > 3800:
            output = output[:3800] + "\n[truncated]"
        return output
