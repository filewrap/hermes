"""Owner-bound Telegram UI for live agents and swarms.

Mirrors ``plugins/platforms/telegram/deep_research_ui.py``: token-based
callback data (``sw:<12-hex>:<action>``), cards sent and edited through the
raw MTProto rich path anchored to the topic root in threaded DMs, and a
launch/progress card driven by the shared ``TelegramProgressScheduler``.

TELEGRAM BOUNDARIES — CLI functions with no Telegram equivalent (see the
boundary map, section 5). Each is named here rather than silently dropped:
- Live log tailing (``cache/delegation/live/<id>/task-<n>.log``): no streaming
  file view exists in chat. A log file can be sent as a document after the
  run, never tailed. No ``tail`` action is offered below.
- Per-tool-call progress (every ``tool.started`` / ``subagent.tool`` event):
  Telegram flood-gates edits to one per 10–60 s per chat, so the card shows
  coarse worker state only. ``update_card`` coalesces through the scheduler.
- Free-text steering of a child (``delegate_task(action="steer")``,
  ``subagent.steer``): callback data is 64 bytes and carries no free text,
  and there is no per-child addressing in chat. No steer control exists.
- Per-worker cancel of one child of a batch delegation: the async-delegation
  store has no per-id interrupt, only a whole-session interrupt. Cancel
  therefore stops the delegation only when it is the only active one for its
  session; otherwise the card says so and points at ``/stop``.
- Worker PIDs and workspace paths (``on_kanban_worker_spawned`` payload):
  host paths are meaningless in chat and are never rendered.
- MoA per-reference streaming chunks and per-reference cancel: in-flight
  reference calls cannot be killed, and MoA has no registry to poll, so no
  MoA row ever appears in ``/swarm``. Only per-reference completion counts
  survive, on the parent turn's own progress card.
- Resume after a gateway restart (``reconnect()``): reports
  ``RECONNECT_UNAVAILABLE`` by design and terminal snapshots last one hour.
  Only durable Kanban swarms survive; an ephemeral swarm card says so when
  its process is gone.
- Spawn-pause kill-switch (``set_spawn_paused``): the TUI ``p`` key has no
  chat binding. No pause control exists.
- ``hermes kanban swarm --json`` machine output and ``--worker`` CLI parsing:
  no shell exists in chat. ``/swarm kanban`` renders the board state instead.
- Mid-run toolset changes: prompt caching forbids mutating the system prompt
  or toolsets mid-conversation, so no such control exists by design.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

_PENDING_TTL_SECONDS = 30 * 60
_LIST_CAP = 12
_RESULT_CHARS = 3000
_GOAL_CHARS = 200

CALLBACK_RE = re.compile(r"sw:([a-f0-9]{12}):([a-z_]+)(?::([a-z_]+))?")
_ACTIONS = ("view", "cancel", "retry", "result", "back", "refresh")


def callback_data(token: str, action: str) -> str:
    """One callback payload; raises when it would exceed the 64-byte limit."""
    data = f"sw:{token}:{action}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Swarm callback data exceeds 64 bytes")
    return data


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[: limit - 3] + "..." if len(text) > limit else text


def _status_label(status: str) -> str:
    return str(status or "unknown").replace("_", " ")


def _worker_lines(workers: list[dict[str, Any]]) -> list[str]:
    lines = []
    for worker in workers or []:
        label = str(worker.get("label") or "worker")
        status = _status_label(worker.get("status"))
        detail = _clip(worker.get("detail") or "", 120)
        row = f"- {label}: **{status}**"
        if detail:
            row += f" · {detail}"
        lines.append(row)
    return lines


def help_text() -> str:
    return (
        "# Agents and swarms\n\n"
        "Live agents, background delegations, and durable Kanban swarms for this chat.\n\n"
        "| Item | Details |\n"
        "|---|---|\n"
        "| **List** |\n"
        "| List | Send `/swarm` or `/swarm status`. |\n"
        "| Inspect | Send `/swarm detail <id>` or tap View on a row. |\n"
        "| Result | Send `/swarm result <id>` or tap Result on a finished row. |\n"
        "| Kanban | Send `/swarm kanban [root-id]` for a durable Kanban swarm with its blackboard. |\n"
        "| **Controls** |\n"
        "| Cancel | Tap Cancel on a running row, or send `/swarm cancel <id>`. Stops the whole delegation or swarm, not one child of a batch. |\n"
        "| Retry | Tap Retry on a blocked Kanban swarm, or send `/swarm retry <id>`. Re-queues its blocked worker cards. |\n"
        "| **Not available on Telegram** |\n"
        "| Live log tailing | No streaming file view exists in chat. |\n"
        "| Free-text steer | A child cannot be steered from chat; there is no per-child addressing. |\n"
        "| Per-tool progress | Cards update at most once every 10–60 seconds per chat, so only coarse worker state is shown. |\n"
        "| Resume after restart | Only durable Kanban swarms survive a gateway restart; ephemeral agents do not resume. |\n"
        "| Mixture-of-Agents | Parallel reference work has no listable registry; its progress shows on the turn that started it. |\n"
    )


def list_text(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "# Agents and swarms\n\nNo live agents, background delegations, or saved swarms in this chat."
    lines = ["# Agents and swarms", ""]
    for entry in entries[:_LIST_CAP]:
        goal = _clip(entry.get("goal") or "", 140)
        short_id = str(entry.get("durable_session_id") or entry.get("source_id") or "?")
        lines.append(
            f"- `{short_id}` · **{_status_label(entry.get('status'))}** · {entry.get('kind')}"
            + (f" · {goal}" if goal else "")
        )
        for row in _worker_lines(entry.get("workers") or [])[:4]:
            lines.append(f"  {row}")
    if len(entries) > _LIST_CAP:
        lines.append(f"Showing {_LIST_CAP} of {len(entries)}.")
    lines.append("")
    lines.append("Tap View on a row for detail, Cancel to stop it, or Result for a finished run.")
    return "\n".join(lines)


def detail_text(entry: dict[str, Any]) -> str:
    from plugins.platforms.telegram.mtproto_rich import rich_button
    lines = ["# Agent detail", ""]
    session_id = str(entry.get("durable_session_id") or "")
    if session_id:
        lines.append(rich_button(f"Session: {session_id}", kind="copy", value=session_id))
        lines.append("")
    lines.append(f"Kind: **{entry.get('kind')}**")
    lines.append(f"Status: **{_status_label(entry.get('status'))}**")
    if entry.get("goal"):
        lines.append(f"Goal: {_clip(entry.get('goal'), _GOAL_CHARS)}")
    if entry.get("source_id"):
        lines.append(f"Source: `{_clip(entry.get('source_id'), 80)}`")
    workers = entry.get("workers") or []
    if workers:
        lines.append("")
        lines.append("Workers:")
        lines.extend(_worker_lines(workers))
    if entry.get("result_summary"):
        lines.append("")
        lines.append(f"Result: {_clip(entry.get('result_summary'), 600)}")
    lines.append("")
    lines.append("Full results live in the Kanban card or the final message, not in this card.")
    return "\n".join(lines)


def result_text(entry: dict[str, Any]) -> str:
    summary = str(entry.get("result_summary") or "").strip()
    lines = ["# Agent result", ""]
    lines.append(f"Kind: **{entry.get('kind')}** · Status: **{_status_label(entry.get('status'))}**")
    if entry.get("goal"):
        lines.append(f"Goal: {_clip(entry.get('goal'), _GOAL_CHARS)}")
    lines.append("")
    if summary:
        lines.append(_clip(summary, _RESULT_CHARS))
    else:
        lines.append("No result text is stored for this run yet. Finished runs deliver their full result in the final message.")
    lines.append("")
    lines.append("Full results live in the Kanban card or the final message, not in this card.")
    return "\n".join(lines)


def kanban_detail_text(state: dict[str, Any], record: dict[str, Any] | None = None) -> str:
    lines = ["# Kanban swarm", ""]
    lines.append(f"Root: `{state.get('root_id')}`")
    if record and record.get("goal"):
        lines.append(f"Goal: {_clip(record.get('goal'), _GOAL_CHARS)}")
    workers = state.get("workers") or []
    if workers:
        lines.append("")
        lines.append("Workers:")
        lines.extend(_worker_lines(workers))
    for key in ("verifier", "synthesizer"):
        item = state.get(key) or {}
        if item:
            lines.append(f"- {key}: **{_status_label(item.get('status'))}** · {_clip(item.get('title') or '', 120)}")
    blackboard = state.get("blackboard") or {}
    if blackboard:
        lines.append("")
        lines.append("Blackboard:")
        for key in sorted(blackboard):
            value = blackboard[key]
            rendered = str(value)
            if isinstance(value, dict):
                rendered = f"{len(value)} entr{'y' if len(value) == 1 else 'ies'}"
            elif isinstance(value, list):
                rendered = f"{len(value)} item{'s' if len(value) != 1 else ''}"
            lines.append(f"- {key}: {_clip(rendered, 160)}")
    return "\n".join(lines)


def _entry_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": f"swarm:{record.get('session_id')}",
        "kind": str(record.get("kind") or "unknown"),
        "source_id": str(record.get("source_id") or ""),
        "status": str(record.get("status") or "unknown"),
        "goal": str(record.get("goal") or ""),
        "workers": list(record.get("workers") or []),
        "session_key": "",
        "board": record.get("board"),
        "durable_session_id": str(record.get("session_id")),
        "result_summary": str(record.get("result_summary") or "") or None,
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
    }


# ------------------------------------------------------------------ Kanban I/O

def read_kanban_swarm_state(conn: Any, root_id: str) -> dict[str, Any]:
    """Live Kanban swarm state: worker/verifier/synthesizer cards + blackboard."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_swarm import latest_blackboard

    def _row(task_id: str | None) -> dict[str, Any]:
        if not task_id:
            return {}
        task = kb.get_task(conn, task_id)
        if task is None:
            return {"task_id": task_id, "status": "unknown", "title": ""}
        return {"task_id": task_id, "status": str(task.status), "title": str(task.title or "")}

    root = kb.get_task(conn, root_id)
    if root is None:
        raise ValueError("Kanban swarm root not found")
    # Walk the whole task graph below the root (workers are children of the
    # root, the verifier is a child of the workers, the synthesizer a child
    # of the verifier) and classify each card by its title.
    workers: list[dict[str, Any]] = []
    verifier_id = synthesizer_id = None
    seen: set[str] = set()
    pending = list(kb.child_ids(conn, root_id)) if hasattr(kb, "child_ids") else []
    while pending:
        child_id = pending.pop()
        if child_id in seen:
            continue
        seen.add(child_id)
        child = kb.get_task(conn, child_id)
        if child is None:
            continue
        pending.extend(kb.child_ids(conn, child_id))
        title = str(child.title or "")
        if title.startswith("Verify"):
            verifier_id = child_id
        elif title.startswith("Synthesize"):
            synthesizer_id = child_id
        else:
            workers.append({
                "label": title or child_id,
                "status": str(child.status),
                "detail": str(child.assignee or ""),
                "task_id": child_id,
            })
    return {
        "root_id": str(root_id),
        "workers": workers,
        "verifier": _row(verifier_id),
        "synthesizer": _row(synthesizer_id),
        "blackboard": latest_blackboard(conn, root_id),
    }


def cancel_kanban_cards(conn: Any, task_ids: list[str]) -> int:
    """Block ready/running Kanban cards so the dispatcher never respawns them.

    ``kind="needs_input"`` is the sticky block kind: ``recompute_ready``
    cannot promote a card blocked this way back into the queue, which is the
    closest the Kanban kernel has to a user cancel (there is no cancel API).
    Returns how many cards actually transitioned.
    """
    from hermes_cli import kanban_db as kb
    count = 0
    for task_id in task_ids:
        try:
            if kb.block_task(conn, task_id, kind="needs_input",
                             reason="Cancelled from Telegram"):
                count += 1
        except Exception:
            logger.debug("Could not block Kanban card %s", task_id, exc_info=True)
    return count


def retry_kanban_cards(conn: Any, task_ids: list[str]) -> int:
    """Unblock Kanban cards so the dispatcher can claim them again."""
    from hermes_cli import kanban_db as kb
    count = 0
    for task_id in task_ids:
        try:
            if kb.unblock_task(conn, task_id):
                count += 1
        except Exception:
            logger.debug("Could not unblock Kanban card %s", task_id, exc_info=True)
    return count


def _kanban_task_ids(entry: dict[str, Any], state: dict[str, Any] | None) -> list[str]:
    ids = [str(w.get("task_id")) for w in (entry.get("workers") or []) if w.get("task_id")]
    if state is not None:
        ids += [str(w.get("task_id")) for w in (state.get("workers") or []) if w.get("task_id")]
        for key in ("verifier", "synthesizer"):
            item = state.get(key) or {}
            if item.get("task_id"):
                ids.append(str(item["task_id"]))
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------------ cancel/retry

def cancel_swarm_entry(entry: dict[str, Any], *, home: Any = None,
                       user_id: str | int | None = None) -> tuple[bool, str]:
    """Stop one swarm entry. Returns ``(stopped, message)``.

    Delegation cancel is session-scoped: the async-delegation store has no
    per-id interrupt, so a delegation is stopped only when it is the only
    active one for its session. With several active, the caller is told to
    use ``/stop`` for the whole session instead of silently stopping siblings.
    """
    kind = str(entry.get("kind") or "")
    if kind == "delegation":
        from tools.async_delegation import interrupt_for_session, list_async_delegations
        session_key = str(entry.get("session_key") or "")
        source_id = str(entry.get("source_id") or "")
        try:
            active = [d for d in list_async_delegations()
                      if str(d.get("status")) in {"running", "stalling"}
                      and str(d.get("session_key") or "") == session_key]
        except Exception:
            return False, "Could not read live delegations; nothing was stopped."
        if not any(str(d.get("delegation_id")) == source_id for d in active):
            return False, "That delegation is no longer running; nothing was stopped."
        if len(active) > 1:
            return False, (
                f"{len(active)} delegations are active in this session, so one cannot be "
                "stopped on its own. Use /stop to end the whole session, or wait for the others."
            )
        try:
            stopped = interrupt_for_session(session_key=session_key, reason="swarm_cancel")
        except Exception:
            return False, "Could not stop that delegation; nothing was stopped."
        if stopped:
            return True, "Delegation stopped."
        return False, "That delegation is no longer running; nothing was stopped."
    if kind == "lifecycle":
        from tools.delegate_tool_registry import interrupt_subagent
        try:
            stopped = interrupt_subagent(str(entry.get("source_id") or ""))
        except Exception:
            return False, "Could not stop that agent; nothing was stopped."
        return (True, "Agent stopped.") if stopped else (False, "That agent is no longer running; nothing was stopped.")
    if kind == "kanban":
        from hermes_cli import kanban_db_connect as kbc
        try:
            conn = kbc.connect(board=str(entry.get("board") or "") or None)
        except Exception:
            return False, "Could not open the Kanban board; nothing was stopped."
        try:
            state = read_kanban_swarm_state(conn, str(entry.get("source_id") or ""))
            blocked = cancel_kanban_cards(conn, _kanban_task_ids(entry, state))
        except Exception:
            return False, "Could not read that Kanban swarm; nothing was stopped."
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if entry.get("durable_session_id") and home is not None and user_id is not None:
            try:
                from gateway.swarm_sessions import update_session
                update_session(home, user_id, str(entry["durable_session_id"]), status="cancelled")
            except Exception:
                logger.debug("Could not mark cancelled Kanban swarm record", exc_info=True)
        if blocked:
            return True, f"Kanban swarm stopped: {blocked} card{'s' if blocked != 1 else ''} blocked."
        return False, "That Kanban swarm has no running cards to stop."
    if kind == "moa":
        return False, "Mixture-of-Agents reference calls cannot be stopped once started."
    return False, "That entry cannot be stopped from here."


def retry_swarm_entry(entry: dict[str, Any], *, home: Any = None,
                      user_id: str | int | None = None) -> tuple[bool, str]:
    """Retry one swarm entry. Only blocked Kanban cards can be re-queued.

    A finished delegation or lifecycle child cannot be re-run from chat: the
    stores keep no re-runnable handle, so the caller starts a new run.
    """
    kind = str(entry.get("kind") or "")
    if kind == "kanban":
        from hermes_cli import kanban_db_connect as kbc
        try:
            conn = kbc.connect(board=str(entry.get("board") or "") or None)
        except Exception:
            return False, "Could not open the Kanban board; nothing was retried."
        try:
            state = read_kanban_swarm_state(conn, str(entry.get("source_id") or ""))
            unblocked = retry_kanban_cards(conn, _kanban_task_ids(entry, state))
        except Exception:
            return False, "Could not read that Kanban swarm; nothing was retried."
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if entry.get("durable_session_id") and home is not None and user_id is not None:
            try:
                from gateway.swarm_sessions import update_session
                update_session(home, user_id, str(entry["durable_session_id"]), status="running")
            except Exception:
                logger.debug("Could not mark retried Kanban swarm record", exc_info=True)
        if unblocked:
            return True, f"Kanban swarm retried: {unblocked} card{'s' if unblocked != 1 else ''} re-queued."
        return False, "That Kanban swarm has no blocked cards to retry."
    if kind in {"delegation", "lifecycle"}:
        return False, "A finished run cannot be retried from chat; start a new one."
    if kind == "moa":
        return False, "Mixture-of-Agents runs cannot be retried from chat."
    return False, "That entry cannot be retried from here."


class TelegramSwarmUI:
    """Telegram cards and callbacks for ``/swarm``. Lazily owned per adapter."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._actions: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def for_adapter(cls, adapter: Any) -> TelegramSwarmUI:
        """Return the adapter's shared UI instance, attaching one if needed.

        The adapter's own ``_swarm_ui()`` accessor (wired by the Phase 1
        callback-routing change) is the permanent home for this; attaching
        here keeps the gateway command path working without editing the
        adapter file.
        """
        ui = getattr(adapter, "_telegram_swarm_ui", None)
        if ui is None:
            ui = cls(adapter)
            adapter._telegram_swarm_ui = ui
        return ui

    def _prune(self) -> None:
        now = time.monotonic()
        for token, item in list(self._actions.items()):
            if now - item.get("created_at", now) > _PENDING_TTL_SECONDS:
                self._actions.pop(token, None)

    def _home(self, source: Any) -> str:
        return str(self.adapter._settings_ui()._home(source))

    # ------------------------------------------------------------------ cards

    @staticmethod
    def _topic_metadata(source: Any) -> dict[str, Any]:
        thread_id = getattr(source, "thread_id", None)
        if not thread_id:
            return {}
        return {
            "thread_id": str(thread_id),
            "telegram_strict_thread": True,
            "telegram_send_in_topic": True,
            "telegram_progress_card": True,
            "swarm_status_card": True,
            "rich_format": "markdown",
        }

    async def _send_card(self, source: Any, text: str, markup: Any = None, *, reply_to: Any = None) -> str | None:
        from plugins.platforms.telegram.mtproto_rich import send_request
        if self.adapter._client is None:
            logger.warning("Could not send Telegram swarm card while disconnected")
            return None
        try:
            thread_id = getattr(source, "thread_id", None)
            # Anchor to the topic root in threaded DMs: the strict-topic edit
            # gate only verifies a card replying directly to the root, so a
            # card anchored to the command message would be uneditable later.
            anchor = int(thread_id) if thread_id else None
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(source.chat_id), text,
                reply_to=int(reply_to) if reply_to is not None and not thread_id else anchor,
                top_msg_id=anchor,
                reply_markup=markup,
            ))
            return self.adapter._sent_message_id(response)
        except Exception:
            logger.warning("Could not send Telegram swarm card", exc_info=True)
            return None

    async def _edit_card(self, source: Any, message_id: str, text: str, markup: Any) -> None:
        from plugins.platforms.telegram.mtproto_rich import edit_request
        if self.adapter._client is None:
            return
        try:
            await self.adapter._mtproto_rich_request(edit_request(
                self.adapter._peer_for(source.chat_id), int(message_id), text, reply_markup=markup,
            ))
        except Exception:
            logger.warning("Could not edit Telegram swarm card", exc_info=True)

    def _list_markup(self, entries: list[dict[str, Any]], message_id: str,
                     source: Any) -> Any:
        from plugins.platforms.telegram.mtproto_rich import inline_markup
        rows = []
        for entry in entries[:_LIST_CAP]:
            token = uuid.uuid4().hex[:12]
            label = _clip(entry.get("goal") or entry.get("source_id") or "agent", 28)
            rows.append([(f"View: {label}", callback_data(token, "view"), "primary")])
            self._actions[token] = {
                "created_at": time.monotonic(), "source": source,
                "user_id": str(source.user_id), "chat_id": str(source.chat_id),
                "message_id": str(message_id), "view": "list",
                "entry": entry, "previous": None,
            }
        token = uuid.uuid4().hex[:12]
        rows.append([("Refresh", callback_data(token, "refresh"), None)])
        self._actions[token] = {
            "created_at": time.monotonic(), "source": source,
            "user_id": str(source.user_id), "chat_id": str(source.chat_id),
            "message_id": str(message_id), "view": "list", "entry": None, "previous": None,
        }
        return inline_markup(rows)

    def _detail_markup(self, entry: dict[str, Any], message_id: str, source: Any,
                       *, previous: str | None) -> Any:
        from plugins.platforms.telegram.mtproto_rich import inline_markup
        rows = []
        status = str(entry.get("status") or "")
        if status in {"running", "starting", "stalling", "stalled"}:
            token = uuid.uuid4().hex[:12]
            rows.append([("Cancel", callback_data(token, "cancel"), "primary")])
            self._actions[token] = {
                "created_at": time.monotonic(), "source": source,
                "user_id": str(source.user_id), "chat_id": str(source.chat_id),
                "message_id": str(message_id), "view": "detail",
                "entry": entry, "previous": previous,
            }
        if entry.get("kind") == "kanban" and status in {"cancelled", "failed", "paused", "blocked"}:
            token = uuid.uuid4().hex[:12]
            rows.append([("Retry", callback_data(token, "retry"), "primary")])
            self._actions[token] = {
                "created_at": time.monotonic(), "source": source,
                "user_id": str(source.user_id), "chat_id": str(source.chat_id),
                "message_id": str(message_id), "view": "detail",
                "entry": entry, "previous": previous,
            }
        if status in {"completed", "failed", "cancelled", "interrupted"}:
            token = uuid.uuid4().hex[:12]
            rows.append([("Result", callback_data(token, "result"), None)])
            self._actions[token] = {
                "created_at": time.monotonic(), "source": source,
                "user_id": str(source.user_id), "chat_id": str(source.chat_id),
                "message_id": str(message_id), "view": "detail",
                "entry": entry, "previous": previous,
            }
        token = uuid.uuid4().hex[:12]
        back_row = [("Back", callback_data(token, "back"), None),
                    ("Refresh", callback_data(token, "refresh"), None)]
        rows.append(back_row)
        self._actions[token] = {
            "created_at": time.monotonic(), "source": source,
            "user_id": str(source.user_id), "chat_id": str(source.chat_id),
            "message_id": str(message_id), "view": "detail",
            "entry": entry, "previous": previous,
        }
        return inline_markup(rows)

    def _result_markup(self, entry: dict[str, Any], message_id: str, source: Any) -> Any:
        from plugins.platforms.telegram.mtproto_rich import inline_markup
        token = uuid.uuid4().hex[:12]
        markup = inline_markup([[("Back", callback_data(token, "back"), None)]])
        self._actions[token] = {
            "created_at": time.monotonic(), "source": source,
            "user_id": str(source.user_id), "chat_id": str(source.chat_id),
            "message_id": str(message_id), "view": "result",
            "entry": entry, "previous": "detail",
        }
        return markup

    # ------------------------------------------------------------------ lookup

    def _collect(self, source: Any) -> list[dict[str, Any]]:
        from gateway.swarm_sessions import collect_swarm_entries
        try:
            home = self._home(source)
            return collect_swarm_entries(home, source.user_id, source.chat_id)
        except Exception:
            logger.warning("Could not list Telegram swarm entries", exc_info=True)
            return []

    @staticmethod
    def _resolve(entries: list[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
        wanted = str(identifier or "").strip().lower()
        if not wanted:
            return None
        for entry in entries:
            if str(entry.get("durable_session_id") or "").lower() == wanted:
                return entry
        for entry in entries:
            if str(entry.get("source_id") or "").lower() == wanted:
                return entry
        for entry in entries:
            if str(entry.get("entry_id") or "").lower() == wanted:
                return entry
        return None

    async def _show_list(self, source: Any, *, reply_to: Any = None) -> None:
        entries = self._collect(source)
        message_id = await self._send_card(source, list_text(entries), None, reply_to=reply_to)
        if message_id and entries:
            markup = self._list_markup(entries, message_id, source)
            await self._edit_card(source, message_id, list_text(entries), markup)

    async def _show_detail(self, source: Any, entry: dict[str, Any], *, reply_to: Any = None,
                           previous: str | None = "list") -> None:
        message_id = await self._send_card(source, detail_text(entry), None, reply_to=reply_to)
        if message_id:
            markup = self._detail_markup(entry, message_id, source, previous=previous)
            await self._edit_card(source, message_id, detail_text(entry), markup)

    async def _show_result(self, source: Any, entry: dict[str, Any], *, reply_to: Any = None) -> None:
        message_id = await self._send_card(source, result_text(entry), None, reply_to=reply_to)
        if message_id:
            markup = self._result_markup(entry, message_id, source)
            await self._edit_card(source, message_id, result_text(entry), markup)

    async def _show_kanban(self, source: Any, root_id: str | None, *, reply_to: Any = None) -> None:
        from gateway.swarm_sessions import load_session
        from hermes_cli import kanban_db_connect as kbc
        entries = self._collect(source)
        record = None
        if root_id:
            target = self._resolve(entries, root_id)
            if target is not None and target.get("durable_session_id"):
                try:
                    record = load_session(self._home(source), source.user_id, target["durable_session_id"])
                except Exception:
                    record = None
            root = str((target or {}).get("source_id") or root_id)
            board = str((target or {}).get("board") or "") or None
        else:
            kanban_entries = [e for e in entries if e.get("kind") == "kanban"]
            if not kanban_entries:
                await self._send_card(source, "# Kanban swarm\n\nNo durable Kanban swarm in this chat.", None,
                                      reply_to=reply_to)
                return
            root = str(kanban_entries[0].get("source_id"))
            board = str(kanban_entries[0].get("board") or "") or None
            if kanban_entries[0].get("durable_session_id"):
                try:
                    record = load_session(self._home(source), source.user_id,
                                          kanban_entries[0]["durable_session_id"])
                except Exception:
                    record = None
        try:
            conn = kbc.connect(board=board)
        except Exception:
            await self._send_card(source, "# Kanban swarm\n\nCould not open the Kanban board.", None,
                                  reply_to=reply_to)
            return
        try:
            state = read_kanban_swarm_state(conn, root)
        except Exception:
            await self._send_card(source, f"# Kanban swarm\n\nNo Kanban swarm `{_clip(root, 80)}` was found.", None,
                                  reply_to=reply_to)
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass
        entry = _entry_from_record(record) if record else {
            "entry_id": f"swarm:kanban:{root}", "kind": "kanban", "source_id": root,
            "status": "running", "goal": "", "workers": state["workers"],
            "session_key": "", "board": board, "durable_session_id": None,
            "result_summary": None, "created_at": "", "updated_at": "",
        }
        message_id = await self._send_card(source, kanban_detail_text(state, record), None, reply_to=reply_to)
        if message_id:
            markup = self._detail_markup(entry, message_id, source, previous="list")
            await self._edit_card(source, message_id, kanban_detail_text(state, record), markup)

    # ------------------------------------------------------------------ command

    async def handle_command(self, event: Any, source: Any, arguments: str, *, reply_to: Any = None) -> bool:
        """Handle ``/swarm``; return False only for unrecognized subcommands."""
        self._prune()
        args = str(arguments or "").strip()
        if not args:
            await self._show_list(source, reply_to=reply_to)
            return True
        if not str(source.user_id or "").isdigit():
            await self.adapter.send(source.chat_id, "Telegram could not identify your account, so I can't list your agents.",
                                    reply_to=reply_to, metadata={"thread_id": source.thread_id} if source.thread_id else None)
            return True
        action, _, rest = args.partition(" ")
        action = action.lower()
        if action == "help":
            await self._send_card(source, help_text(), None, reply_to=reply_to)
            return True
        if action == "status":
            session_id = rest.strip().lower()
            if session_id and not re.fullmatch(r"[a-f0-9]{12}", session_id):
                await self._send_card(source, "Use `/swarm status [12-character-session-id]` to inspect one saved swarm.", None, reply_to=reply_to)
                return True
            entries = self._collect(source)
            if session_id:
                entry = self._resolve(entries, session_id)
                if entry is None:
                    await self._send_card(source, f"No swarm `{session_id}` was found for this account in this chat.", None, reply_to=reply_to)
                    return True
                await self._show_detail(source, entry, reply_to=reply_to)
                return True
            await self._show_list(source, reply_to=reply_to)
            return True
        if action in {"detail", "cancel", "retry", "result"}:
            identifier = rest.strip()
            if not identifier:
                await self._send_card(source, f"Use `/swarm {action} <id>` with a session or delegation id from `/swarm status`.", None, reply_to=reply_to)
                return True
            entries = self._collect(source)
            entry = self._resolve(entries, identifier)
            if entry is None:
                await self._send_card(source, f"No agent or swarm `{_clip(identifier, 80)}` was found for this account in this chat.", None, reply_to=reply_to)
                return True
            if action == "detail":
                await self._show_detail(source, entry, reply_to=reply_to)
                return True
            if action == "result":
                await self._show_result(source, entry, reply_to=reply_to)
                return True
            home = self._home(source)
            if action == "cancel":
                _ok, message = cancel_swarm_entry(entry, home=home, user_id=source.user_id)
            else:
                _ok, message = retry_swarm_entry(entry, home=home, user_id=source.user_id)
            fresh = self._resolve(self._collect(source), identifier) or entry
            await self._show_detail(source, fresh, reply_to=reply_to)
            await self.adapter.send(source.chat_id, message,
                                    reply_to=reply_to, metadata=self._topic_metadata(source) if source.thread_id else None)
            return True
        if action == "kanban":
            await self._show_kanban(source, rest.strip() or None, reply_to=reply_to)
            return True
        return False

    # ------------------------------------------------------------------ callbacks

    async def handle_callback(self, event: Any, data: str) -> None:
        self._prune()
        match = CALLBACK_RE.fullmatch(str(data or ""))
        if not match:
            await event.answer("This agent action is invalid or expired.", alert=True)
            return
        token, action = match.group(1), match.group(2)
        pending = self._actions.get(token)
        source = await self.adapter._source(event)
        if pending is None and self.adapter._authorized(source):
            # Observer-issued token (sw:<12-hex>:cancel): the observer and this
            # UI share the sw: namespace but keep separate registries. Fall
            # back to the observer before rejecting as expired.
            from gateway.swarm_observer import get_active_observer

            observer = get_active_observer()
            resolved = (
                observer.resolve_callback(data, user_id=source.user_id, chat_id=source.chat_id)
                if observer is not None
                else None
            )
            if resolved is not None:
                action, card = resolved
                if action == "cancel":
                    _ok, note = observer.request_cancel(card)
                    await event.answer(note)
                    await observer.refresh_card(card)
                    return
        if (not pending or str(source.user_id or "") != pending["user_id"]
                or str(source.chat_id) != pending["chat_id"]
                or str(getattr(event, "message_id", "")) != pending.get("message_id")
                or not self.adapter._authorized(source)):
            await event.answer("This action is not available for this account or has expired.", alert=True)
            return
        message_id = str(pending["message_id"])
        entries = self._collect(source)
        stored = pending.get("entry") or {}
        entry = None
        if stored.get("durable_session_id"):
            entry = self._resolve(entries, str(stored["durable_session_id"]))
        if entry is None and stored.get("source_id"):
            entry = self._resolve(entries, str(stored["source_id"]))
        if action == "refresh":
            view = str(pending.get("view") or "list")
            if view == "detail" and entry is not None:
                await self._edit_card(source, message_id, detail_text(entry),
                                      self._detail_markup(entry, message_id, source, previous=pending.get("previous")))
            elif view == "result" and entry is not None:
                await self._edit_card(source, message_id, result_text(entry),
                                      self._result_markup(entry, message_id, source))
            else:
                await self._edit_card(source, message_id, list_text(entries),
                                      self._list_markup(entries, message_id, source))
            await event.answer("Refreshed")
            return
        if action == "back":
            previous = str(pending.get("previous") or "list")
            if previous == "detail" and entry is not None:
                await self._edit_card(source, message_id, detail_text(entry),
                                      self._detail_markup(entry, message_id, source, previous="list"))
            else:
                await self._edit_card(source, message_id, list_text(entries),
                                      self._list_markup(entries, message_id, source))
            await event.answer("Back")
            return
        if entry is None:
            await event.answer("That agent or swarm is no longer listed; refresh the list.", alert=True)
            return
        if action == "view":
            await event.answer("Opening detail")
            await self._edit_card(source, message_id, detail_text(entry),
                                  self._detail_markup(entry, message_id, source, previous="list"))
            return
        if action == "result":
            await event.answer("Opening result")
            await self._edit_card(source, message_id, result_text(entry),
                                  self._result_markup(entry, message_id, source))
            return
        if action == "cancel":
            home = self._home(source)
            _ok, message = cancel_swarm_entry(entry, home=home, user_id=source.user_id)
            await event.answer(message)
            fresh = self._resolve(self._collect(source), str(entry.get("durable_session_id") or entry.get("source_id") or "")) or entry
            await self._edit_card(source, message_id, detail_text(fresh),
                                  self._detail_markup(fresh, message_id, source, previous=pending.get("previous")))
            return
        if action == "retry":
            home = self._home(source)
            _ok, message = retry_swarm_entry(entry, home=home, user_id=source.user_id)
            await event.answer(message)
            fresh = self._resolve(self._collect(source), str(entry.get("durable_session_id") or entry.get("source_id") or "")) or entry
            await self._edit_card(source, message_id, detail_text(fresh),
                                  self._detail_markup(fresh, message_id, source, previous=pending.get("previous")))
            return
        await event.answer("Unknown action.", alert=True)

    # ------------------------------------------------------------------ progress card

    def _card_metadata(self, source: Any) -> dict[str, Any]:
        return self._topic_metadata(source)

    def _card_key(self, source: Any, session_key: str, session_id: str) -> str:
        return f"{source.chat_id}:{session_key}:swarm:{session_id}"

    async def begin_card(self, source: Any, record: dict[str, Any], *, session_key: str) -> str | None:
        """Register a flood-gated status card for one swarm and persist its message ID.

        Used by the observer (Builder A) when a swarm starts. The scheduler
        owns all later edits; see :meth:`update_card`.
        """
        from gateway.swarm_sessions import update_session
        from gateway.telegram_progress_scheduler import scheduler_for
        home = self._home(source)
        entry = _entry_from_record(record)
        scheduler = scheduler_for(self.adapter, source.chat_id)
        key = self._card_key(source, session_key, str(record["session_id"]))
        thread_id = getattr(source, "thread_id", None)

        async def send_start(text: str) -> Any:
            return await self.adapter.send(
                source.chat_id, text,
                reply_to=str(thread_id) if thread_id else None,
                metadata=self._card_metadata(source),
            )

        async def edit(message_id: str, text: str) -> Any:
            return await self.adapter.edit_message(
                source.chat_id, message_id, text, metadata=self._card_metadata(source))

        async def on_started(message_id: str) -> None:
            try:
                update_session(home, source.user_id, str(record["session_id"]),
                               progress_message_id=str(message_id))
            except Exception:
                logger.debug("Could not persist swarm progress message ID", exc_info=True)

        return await scheduler.begin(key, "swarm", detail_text(entry),
                                     send_start=send_start, edit=edit, on_started=on_started)

    async def update_card(self, source: Any, record: dict[str, Any], text: str, *,
                          session_key: str, terminal: bool = False) -> None:
        """Queue a coalesced card update; a no-op when no card was begun."""
        from gateway.telegram_progress_scheduler import scheduler_for
        scheduler = scheduler_for(self.adapter, source.chat_id)
        scheduler.update(self._card_key(source, session_key, str(record["session_id"])),
                         text, terminal=terminal)
