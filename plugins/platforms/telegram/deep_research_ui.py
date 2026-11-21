"""Owner-bound Telegram UI for interactive deep-research sessions."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from types import SimpleNamespace
import uuid
from typing import Any

logger = logging.getLogger(__name__)
_PENDING_TTL_SECONDS = 30 * 60

# Pre-start configuration choices. Cycles are shown on the config card; the
# first value in each list is the default for a new session.
_LAUNCH_WATCH_GRACE_SECONDS = 60
_MAX_TURN_OPTIONS = (50, 100, 150, 300, 500)
_REPORT_FORMAT_OPTIONS = ("md", "pdf", "docx")
_REPORT_FORMAT_LABELS = {"md": "Markdown", "pdf": "PDF", "docx": "Word"}
_DEPTH_OPTIONS = ("standard", "deep", "exhaustive")
_DEPTH_LABELS = {"standard": "Standard", "deep": "Deep", "exhaustive": "Exhaustive"}


def _default_config() -> dict[str, Any]:
    return {"max_turns": 150, "report_format": "md", "depth": "deep", "custom_instructions": ""}


def _cycle(value: Any, options: tuple[Any, ...]) -> Any:
    try:
        return options[(options.index(value) + 1) % len(options)]
    except ValueError:
        return options[0]


def _parse_instructions_arg(args: str) -> tuple[str, str]:
    """Split ``--instructions ...`` off a research question.

    The flag takes the rest of the command; surrounding matching quotes are
    stripped. Everything before the flag is the question.
    """
    match = re.search(r"(?:^|\s)--instructions(?:\s+|$)", args)
    if not match:
        return args.strip(), ""
    query = args[:match.start()].strip()
    instructions = args[match.end():].strip()
    if len(instructions) >= 2 and instructions[0] == instructions[-1] and instructions[0] in "\"'":
        instructions = instructions[1:-1]
    return query, instructions.strip()


def created_topic_id_from_updates(response: Any, *, random_id: int | None = None,
                                 peer: Any = None) -> int | None:
    """Extract the forum root message ID from CreateForumTopic MTProto updates.

    ``UpdateShort`` wraps its underlying update, while ``Updates`` and
    ``UpdatesCombined`` expose an update sequence. A topic's root is the
    ``MessageActionTopicCreate`` service message ID, not an arbitrary numeric
    field or welcome-message ID. A matching ``UpdateMessageID`` is accepted
    only when it carries this exact request's random ID.
    """
    if response is None:
        return None
    pending = [response]
    seen: set[int] = set()
    mapped_id = None
    topic_message_id = None
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if type(item).__name__ == "UpdateMessageID" and random_id is not None:
            try:
                if int(getattr(item, "random_id", 0)) == int(random_id):
                    mapped_id = int(item.id)
            except (TypeError, ValueError):
                pass
        message = getattr(item, "message", None)
        if message is not None:
            action = getattr(message, "action", None)
            if type(action).__name__ == "MessageActionTopicCreate":
                if peer is None or _same_telegram_peer(getattr(message, "peer_id", None), peer):
                    try:
                        topic_message_id = int(message.id)
                    except (TypeError, ValueError):
                        pass
            pending.append(message)
        updates = getattr(item, "updates", None)
        if updates is not None:
            try:
                pending.extend(list(updates))
            except TypeError:
                pass
        nested = getattr(item, "update", None)
        if nested is not None:
            pending.append(nested)
    return topic_message_id or mapped_id


def _same_telegram_peer(actual: Any, expected: Any) -> bool:
    """Compare Telethon peers and numeric chat IDs without trusting display text."""
    if actual is None or expected is None:
        return False
    try:
        from telethon.utils import get_peer_id
        actual_id = int(get_peer_id(actual))
    except (TypeError, ValueError):
        try:
            actual_id = int(actual)
        except (TypeError, ValueError):
            return False
    try:
        expected_id = int(get_peer_id(expected))
    except (NameError, TypeError, ValueError):
        try:
            expected_id = int(expected)
        except (TypeError, ValueError):
            return False
    return actual_id == expected_id


class TelegramDeepResearchUI:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._pending: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._resume_actions: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for token, item in list(self._pending.items()):
            if now - item.get("created_at", now) > _PENDING_TTL_SECONDS:
                self._pending.pop(token, None)
        for token, item in list(self._resume_actions.items()):
            if now - item.get("created_at", now) > _PENDING_TTL_SECONDS:
                self._resume_actions.pop(token, None)

    @staticmethod
    def _help_text() -> str:
        # One table for the whole reference: each section is a single-cell
        # row that the blocks converter renders as a spanning header cell,
        # so every section keeps its own head without a separate table.
        # Italics use the double-underscore dialect: the blocks converter
        # parses inline Markdown with Telethon's parser, which leaves
        # single `_..._` / `*...*` markers literal.
        return (
            "## Deep research\n\n"
            "An autonomous, source-grounded investigation that checks evidence, keeps a private source ledger, and delivers a cited report.\n\n"
            "| Item | Details |\n"
            "|---|---|\n"
            "| **Start** |\n"
            "| Start | Send `/deepresearch <question>`. |\n"
            "| Configure | Review the question, then tap the buttons to change max turns, report format, and depth. |\n"
            "| Confirm | Tap **Start session**. Research runs in a dedicated topic created for it. |\n"
            "| **Configuration** |\n"
            "| Max turns | 50, 100, 150, 300, 500 (default: 150) |\n"
            "| Report format | Markdown, PDF, Word (default: Markdown) |\n"
            "| Depth | Standard, Deep, Exhaustive (default: Deep) |\n"
            "| Extra instructions | Add `--instructions \"...\"` after the question (default: none) |\n"
            "| **Session rules** |\n"
            "| Shell | Enabled only inside the private session folder (`/workspace`) |\n"
            "| Research tools and selected MCPs | Available |\n"
            "| Approval prompts | Disabled for the session's scoped work; the session file boundary and the ban on consequential external actions still apply |\n"
            "| Compaction | Allowed |\n"
            "| Telegram topic | Required; research never falls back to the parent chat |\n"
            "| Restart / resume | State is durable; a run that stops (restart, error, dropped admission) pauses with its checkpoint saved and can be resumed in its original topic |\n"
            "| **Research workflow** |\n"
            "| Scope | Define the question, subquestions, boundaries, and assumptions. |\n"
            "| Sources | Search broadly and prioritize credible primary and authoritative sources. |\n"
            "| Verify | Read sources, cross-check material claims against independent evidence, and investigate meaningful disagreements. |\n"
            "| Report | Cite key claims with exact URLs; distinguish findings from inference and state evidence gaps, limitations, and uncertainty. |\n"
            "| **Controls** |\n"
            "| `/deepresearch status` | List sessions. |\n"
            "| `/deepresearch resume [session-id]` | Resume the newest paused/interrupted session here, or the specified one, in its original topic. |\n"
            "| `/deepresearch resume --accept-current [session-id]` | Resume after accepting that the on-disk notes/report are newer than their checkpoint snapshots (owner reconcile; identity, topic, tasks, and evidence checks still apply). |\n"
            "| `/deepresearch stop` | Stop the run (or use `/stop` in its topic). |\n\n"
            "Depth sets how far the investigation goes: Standard answers the question thoroughly in one full pass; Deep runs several search/read/verify rounds and writes a comprehensive report; Exhaustive keeps going until new searches stop surfacing new material evidence. Reports are written to be thorough — a deep session is expected to produce a large report, not a short summary. No size limit is enforced.\n\n"
            "__If a clarification is non-essential, Hermes states an assumption and continues.__"
        )

    @staticmethod
    def _config_text(query: str, config: dict[str, Any]) -> str:
        # Render the query in a code block so user-provided Markdown cannot forge headings/buttons.
        safe_query = query.replace("```", "'''")
        instructions = str(config.get("custom_instructions") or "").strip()
        # Dash markers (not `•`) so the blocks converter builds a native
        # list; a `•` line is plain paragraph text and its line breaks are
        # folded into one flowing paragraph. The instructions fence stays
        # on its own lines so it is not glued to the list item.
        lines = [
            "# Configure deep research",
            "",
            "**Question**",
            "```",
            safe_query,
            "```",
            "",
            "**Settings** — tap a button to change a value, then start.",
            "",
            f"- Max turns: **{config.get('max_turns')}** agent turns maximum",
            f"- Report format: **{_REPORT_FORMAT_LABELS.get(config.get('report_format'), 'Markdown')}**",
            f"- Depth: **{_DEPTH_LABELS.get(config.get('depth'), 'Deep')}**",
        ]
        if instructions:
            lines.append("- Extra instructions:")
            lines.extend(["```", instructions.replace("```", "'''"), "```"])
        else:
            lines.append("- Extra instructions: None")
        lines.extend([
            "",
            "Starting creates a private session folder and a dedicated Telegram topic; the research runs in that topic only. "
            "Approval prompts are disabled for the session's scoped work.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _config_markup(token: str, config: dict[str, Any]) -> Any:
        from plugins.platforms.telegram.mtproto_rich import inline_markup

        return inline_markup([
            [(f"Turns: {config.get('max_turns')}", f"dr:{token}:cfg:max_turns", None),
             (f"Format: {_REPORT_FORMAT_LABELS.get(config.get('report_format'), 'Markdown')}", f"dr:{token}:cfg:report_format", None),
             (f"Depth: {_DEPTH_LABELS.get(config.get('depth'), 'Deep')}", f"dr:{token}:cfg:depth", None)],
            [("Start session", f"dr:{token}:start", "primary"),
             ("Cancel", f"dr:{token}:cancel", None)],
        ])

    async def handle_command(self, event: Any, source: Any, arguments: str, *, reply_to: Any = None) -> bool:
        """Handle `/deepresearch`; return False only for unrecognized subcommands."""
        self._prune()
        args = str(arguments or "").strip()
        if not args:
            await self._send_card(source, self._help_text(), reply_to=reply_to)
            return True
        if not str(source.user_id or "").isdigit():
            await self.adapter.send(source.chat_id, "Telegram could not identify your account, so I can't create a private research session.",
                                    reply_to=reply_to, metadata={"thread_id": source.thread_id} if source.thread_id else None)
            return True
        action, _, rest = args.partition(" ")
        if action.lower() == "status":
            session_id = rest.strip().lower()
            if session_id and not re.fullmatch(r"[a-f0-9]{12}", session_id):
                await self._send_card(source, "Use `/deepresearch status [12-character-session-id]` to inspect saved research.", reply_to=reply_to)
                return True
            await self._show_sessions(source, reply_to=reply_to, session_id=session_id or None)
            return True
        if action.lower() == "resume":
            parts = rest.strip().split()
            accept_current = "--accept-current" in parts
            ids = [part for part in parts if part != "--accept-current"]
            session_id = ids[0].strip().lower() if ids else ""
            if session_id and not re.fullmatch(r"[a-f0-9]{12}", session_id):
                await self.adapter.send(
                    source.chat_id,
                    "Use `/deepresearch resume <12-character-session-id>` for a paused or interrupted session.",
                    reply_to=reply_to, metadata={"thread_id": source.thread_id} if source.thread_id else None,
                )
                return True
            if len(ids) > 1:
                await self.adapter.send(
                    source.chat_id,
                    "Use `/deepresearch resume [session-id]` with at most one session id.",
                    reply_to=reply_to, metadata={"thread_id": source.thread_id} if source.thread_id else None,
                )
                return True
            trigger_message_id = getattr(event, "message_id", None) or reply_to
            await self._queue_resume(source, session_id or None, trigger_message_id=trigger_message_id,
                                     reply_to=reply_to, accept_artifact_drift=accept_current)
            return True
        if action.lower() == "stop" and not rest.strip():
            from gateway.platforms.event import MessageEvent, MessageType
            stop_source = source
            try:
                from gateway.deep_research_sessions import list_sessions
                active = [item for item in list_sessions(self.adapter._settings_ui()._home(source), source.user_id)
                          if item.get("status") in {"starting", "running"}
                          and str(item.get("chat_id")) == str(source.chat_id)]
                if not source.thread_id and len(active) == 1 and active[0].get("thread_id"):
                    stop_source = dataclasses.replace(source, thread_id=str(active[0]["thread_id"]))
                elif not source.thread_id and len(active) > 1:
                    await self.adapter.send(
                        source.chat_id,
                        "More than one research session is active in this chat. Use `/stop` inside the topic you want to interrupt.",
                        reply_to=reply_to,
                    )
                    return True
            except Exception:
                logger.debug("Could not resolve active deep-research topic for /stop", exc_info=True)
            stop_event = MessageEvent(
                text="/stop", message_type=MessageType.COMMAND, user_id=source.user_id,
                user_name=source.user_name, source=stop_source, message_id=str(reply_to or "") or None,
                metadata={"thread_id": stop_source.thread_id} if stop_source.thread_id else {},
            )
            await self.adapter.handle_message(stop_event)
            return True
        if len(args) > 4000:
            await self.adapter.send(source.chat_id, "Research questions must be 4,000 characters or fewer.",
                                    reply_to=reply_to, metadata={"thread_id": source.thread_id} if source.thread_id else None)
            return True
        query, instructions = _parse_instructions_arg(args)
        if not query:
            await self._send_card(source, self._help_text(), reply_to=reply_to)
            return True
        config = _default_config()
        config["custom_instructions"] = instructions[:2000]
        token = uuid.uuid4().hex[:12]
        message_id = await self._send_card(
            source, self._config_text(query, config), self._config_markup(token, config), reply_to=reply_to)
        if not message_id:
            return True
        self._pending[token] = {
            "created_at": time.monotonic(),
            "source": source,
            "user_id": str(source.user_id),
            "chat_id": str(source.chat_id),
            "message_id": str(message_id),
            "trigger_message_id": str(getattr(event, "message_id", None) or reply_to or "") or None,
            "query": query,
            "config": config,
            "event": event,
            "started": False,
        }
        return True

    async def handle_callback(self, event: Any, data: str) -> None:
        self._prune()
        match = re.fullmatch(r"dr:([a-f0-9]{12}):([a-z_]+)(?::([a-z_]+))?", data)
        if not match:
            await event.answer("This research action is invalid or expired.", alert=True)
            return
        token, action, field = match.groups()
        if action in {"resume", "reconcile"}:
            pending = self._resume_actions.get(token)
            source = await self.adapter._source(event)
            if (not pending or str(source.user_id or "") != pending["user_id"]
                    or str(source.chat_id) != pending["chat_id"]
                    or str(getattr(event, "message_id", "")) != pending.get("message_id")
                    or not self.adapter._authorized(source)):
                await event.answer("This resume action is not available for this account or has expired.", alert=True)
                return
            self._resume_actions.pop(token, None)
            await event.answer("Resuming deep research…")
            await self.adapter.send(source.chat_id, "Checking saved state before resuming deep research…",
                                    reply_to=str(event.message_id), metadata=self._topic_metadata(source) if source.thread_id else None)
            task = asyncio.create_task(
                self._resume_session(pending["source"], pending["session_id"],
                                     trigger_message_id=pending.get("message_id"),
                                     accept_artifact_drift=bool(pending.get("accept_artifact_drift"))),
                name=f"telegram-deep-research-resume-{pending['session_id']}",
            )
            self._tasks[token] = task
            task.add_done_callback(lambda _task, key=token: self._tasks.pop(key, None))
            return
        if action == "cfg":
            pending = self._pending.get(token)
            source = await self.adapter._source(event)
            if (not pending or str(source.user_id or "") != pending["user_id"]
                    or str(source.chat_id) != pending["chat_id"]
                    or str(getattr(event, "message_id", "")) != pending["message_id"]
                    or not self.adapter._authorized(source)):
                await event.answer("This action is not available for this account or has expired.", alert=True)
                return
            config = pending.get("config") or _default_config()
            pending["config"] = config
            if field == "max_turns":
                config["max_turns"] = _cycle(config.get("max_turns"), _MAX_TURN_OPTIONS)
                await event.answer(f"Max turns: {config['max_turns']}")
            elif field == "report_format":
                config["report_format"] = _cycle(config.get("report_format"), _REPORT_FORMAT_OPTIONS)
                await event.answer(f"Report format: {_REPORT_FORMAT_LABELS[config['report_format']]}")
            elif field == "depth":
                config["depth"] = _cycle(config.get("depth"), _DEPTH_OPTIONS)
                await event.answer(f"Depth: {_DEPTH_LABELS[config['depth']]}")
            else:
                await event.answer("Unknown setting.", alert=True)
                return
            await self._edit_card(source, pending["message_id"],
                                  self._config_text(pending["query"], config),
                                  self._config_markup(token, config))
            return
        pending = self._pending.get(token)
        source = await self.adapter._source(event)
        if (not pending or str(source.user_id or "") != pending["user_id"]
                or str(source.chat_id) != pending["chat_id"]
                or str(getattr(event, "message_id", "")) != pending["message_id"]
                or not self.adapter._authorized(source)):
            await event.answer("This action is not available for this account or has expired.", alert=True)
            return
        if action == "cancel":
            self._pending.pop(token, None)
            await event.answer("Cancelled")
            await self._edit_card(source, pending["message_id"], "# Deep research cancelled\n\nNo session was started.", None)
            await self.adapter.send(source.chat_id, "Deep research cancelled. No session was started.",
                                    reply_to=str(event.message_id), metadata={"thread_id": source.thread_id} if source.thread_id else None)
            return
        if pending.get("started"):
            await event.answer("This session has already started.", alert=True)
            return
        pending["started"] = True
        await event.answer("Starting deep research…")
        await self.adapter.send(source.chat_id, "Starting deep research: creating and checking its dedicated topic…",
                                reply_to=str(event.message_id), metadata=self._topic_metadata(source) if source.thread_id else None)
        self._pending.pop(token, None)
        task = asyncio.create_task(self._start_session(pending), name=f"telegram-deep-research-{token}")
        self._tasks[token] = task
        task.add_done_callback(lambda _task, key=token: self._tasks.pop(key, None))

    @staticmethod
    async def _convert_report(session_dir: Any, report_path: Any, report_format: str) -> Any:
        """Best-effort pandoc conversion of the verified report.md.

        Returns the converted file path, or None when conversion is
        unavailable or fails. Never raises: delivery falls back to Markdown.
        """
        import shutil
        import subprocess
        from pathlib import Path

        if shutil.which("pandoc") is None:
            return None
        target = Path(session_dir) / f"report.{report_format}"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["pandoc", str(report_path), "-o", str(target)],
                capture_output=True, timeout=300,
            )
        except Exception:
            logger.warning("DeepResearch report conversion failed", exc_info=True)
            return None
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            return None
        return target

    async def _complete_session_after_turn(
        self, *, home: Any, user_id: str, session_id: str, source: Any,
        run_event: Any, session_key: str, reply_to: str | None,
    ) -> None:
        """Attach only a validated report in the original topic after a successful agent turn."""
        from gateway.deep_research_sessions import (
            load_checkpoint, load_session, resolve_session_dir, save_checkpoint,
            update_session, validate_research_artifacts,
        )

        completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        current = load_session(home, user_id, session_id)
        if (str(getattr(source, "chat_id", "")) != str(current.get("chat_id"))
                or str(getattr(source, "user_id", "")) != str(current.get("user_id"))
                or (current.get("thread_id") and str(getattr(source, "thread_id", "")) != str(current["thread_id"]))):
            self._persist_pause(
                home, user_id, session_id, reason="topic_identity_mismatch",
                last_error="Final delivery destination does not match the saved research owner/chat/topic; no report was sent.",
                final_response_status="failed",
            )
            await self._send_session_notice(
                source, current,
                f"Research session `{session_id}` is paused because its delivery destination did not match saved state. Use `/deepresearch status {session_id}` to review it. No research content was sent.",
                reply_to=current.get("latest_reply_message_id") or current.get("thread_id"),
                event=run_event,
            )
            return
        guard = getattr(self.adapter, "_active_sessions", {}).get(session_key)
        if guard is not None and callable(getattr(guard, "is_set", None)) and guard.is_set():
            error = "Research was interrupted before completion; the saved work can be resumed."
            self._persist_pause(home, user_id, session_id, reason="interrupted", last_error=error,
                                topic_id=current.get("thread_id"), final_response_status="interrupted")
            update_session(home, user_id, session_id, status="interrupted", completed_at=completed_at,
                           final_response_status="interrupted")
            save_checkpoint(home, user_id, session_id, status="interrupted", last_error=error,
                            latest_reply_message_id=current.get("latest_reply_message_id"))
            return
        if getattr(run_event, "_gateway_accepted", None) is False:
            return
        if current.get("status") in {"paused", "interrupted"}:
            if current.get("failure_class") == "gateway_not_accepted":
                return
            if isinstance(getattr(run_event, "metadata", None), dict) and run_event.metadata.get("deep_research_terminal_pause_published"):
                # This turn's final response already carried the pause into the
                # topic (finish_search_progress published it); sending the
                # notice here would deliver the same pause twice.
                return
            response_status = "interrupted" if current.get("status") == "interrupted" else "failed"
            if current.get("final_response_status") != response_status:
                update_session(home, user_id, session_id, final_response_status=response_status)
            issue = str(current.get("last_error") or "Partial work is saved.")
            delivery_error = str(getattr(run_event, "metadata", {}).get("telegram_task_delivery_failure") or "")
            if delivery_error:
                issue += f" Topic delivery also failed ({delivery_error})."
            status_label = "interrupted" if current.get("status") == "interrupted" else "paused"
            text = (
                f"Deep research is {status_label}. {issue} "
                f"Resume after resolving the issue with `/deepresearch resume {session_id}`."
            )
            notify = await self._send_session_notice(
                source, current, text,
                reply_to=current.get("latest_reply_message_id") or getattr(source, "thread_id", None),
                event=run_event,
            )
            if not getattr(notify, "success", False):
                await self._send_session_notice(
                    source, current,
                    f"Deep research session `{session_id}` is {status_label}; use `/deepresearch status` in this chat to resume. No research content was sent outside the topic.",
                    reply_to=current.get("thread_id"),
                )
            return
        if current.get("status") in {"failed", "cancelled"}:
            return
        delivery_error = str(getattr(run_event, "metadata", {}).get("telegram_task_delivery_failure") or "")
        if delivery_error:
            message = (
                f"A research message could not be confirmed in its original Telegram topic ({delivery_error}). "
                "The session is paused; no research content was sent to the parent chat."
            )
            self._persist_pause(home, user_id, session_id, reason="topic_delivery_exhausted",
                                last_error=message, topic_id=current.get("thread_id"),
                                final_response_status="failed")
            notify = await self._send_session_notice(
                source, current,
                f"Deep research is paused. {message} Resume after fixing topic delivery with `/deepresearch resume {session_id}`.",
                reply_to=current.get("latest_reply_message_id") or getattr(source, "thread_id", None),
                event=run_event,
            )
            if not getattr(notify, "success", False):
                await self._send_session_notice(
                    source, current,
                    f"Deep research session `{session_id}` is paused after a topic-delivery failure. Use `/deepresearch status` to resume. No research content was sent outside the topic.",
                    reply_to=current.get("thread_id"),
                )
            return
        if not getattr(source, "thread_id", None):
            error = "The original research topic is unavailable; strict topic-only delivery was refused."
            self._persist_pause(home, user_id, session_id, reason="topic_unavailable", last_error=error,
                                final_response_status="failed")
            return

        try:
            session_dir = resolve_session_dir(home, user_id, session_id)
            candidate = session_dir / "report.md"
            report_path = candidate.resolve(strict=True)
            if (not report_path.is_relative_to(session_dir) or not report_path.is_file()
                    or report_path.stat().st_size == 0):
                raise FileNotFoundError("report.md is not a non-empty regular file in the private session folder")
            checkpoint = load_checkpoint(home, user_id, session_id)
            valid, evidence_issue = validate_research_artifacts(
                session_dir, checkpoint.get("research_evidence"),
            )
            artifacts_valid = valid
            todos = checkpoint.get("todos")
            if valid and (not isinstance(todos, list) or not todos or any(
                    item.get("status") not in {"completed", "cancelled"} for item in todos)):
                valid = False
                evidence_issue = "The task ledger is missing or still has unfinished tasks; the report is saved, not research complete."
            if not valid:
                message = (f"The saved work did not pass source-evidence checks: {evidence_issue} "
                           "Partial work is saved and can be resumed.")
                self._persist_pause(home, user_id, session_id,
                                    reason="unfinished_tasks" if artifacts_valid else "insufficient_research_evidence",
                                    last_error=message, topic_id=current.get("thread_id"),
                                    artifact_status="ready" if artifacts_valid else "invalid", final_response_status="failed")
                notify = await self._send_session_notice(
                    source, current,
                    f"Deep research is paused. {message} Resume with `/deepresearch resume {session_id}`.",
                    reply_to=(current.get("latest_reply_message_id") or getattr(source, "thread_id", None)),
                    event=run_event,
                )
                if not getattr(notify, "success", False):
                    await self._send_session_notice(
                        source, current,
                        f"Deep research session `{session_id}` is paused because its report lacks verified source evidence. Use `/deepresearch status` to resume. No report was sent outside the topic.",
                        reply_to=current.get("thread_id"),
                    )
                return
        except Exception as exc:
            error = f"The research turn did not create a usable report.md ({type(exc).__name__}). Partial work is saved."
            self._persist_pause(home, user_id, session_id, reason="report_not_written", last_error=error,
                                topic_id=current.get("thread_id"), artifact_status="invalid",
                                final_response_status="failed")
            result = await self._send_session_notice(
                source, current,
                f"Deep research is paused because report.md is missing or invalid. {error} Resume with `/deepresearch resume {session_id}`.",
                reply_to=(current.get("latest_reply_message_id") or getattr(source, "thread_id", None)),
                event=run_event,
            )
            if not getattr(result, "success", False):
                await self._send_session_notice(
                    source, current,
                    f"Deep research session `{session_id}` is paused; its report is not yet available. No research content was sent outside the topic.",
                    reply_to=current.get("thread_id"),
                )
            return

        update_session(home, user_id, session_id, artifact_status="ready", final_response_status="pending")
        metadata = self._topic_metadata(source, run_event)
        research = run_event.metadata.get("deep_research", {}) if isinstance(getattr(run_event, "metadata", None), dict) else {}
        anchor = (research.get("latest_reply_message_id") if isinstance(research, dict) else None) or current.get("latest_reply_message_id") or source.thread_id
        # Deliver the configured format. report.md is always the verified
        # source of truth; pdf/docx are conversions of it, never a second
        # authoring pass. A failed conversion falls back to Markdown with a
        # notice, rather than pausing a verified report.
        report_format = str(current.get("report_format") or research.get("report_format") or "md").strip().lower()
        deliver_path = report_path
        conversion_fallback = False
        if report_format in {"pdf", "docx"}:
            converted = await self._convert_report(session_dir, report_path, report_format)
            if converted is not None:
                deliver_path = converted
            else:
                conversion_fallback = True
        sender = getattr(self.adapter, "send_document", None)
        try:
            result = await sender(
                source.chat_id, str(deliver_path), caption="Deep research report",
                reply_to=str(anchor), metadata=metadata,
            ) if callable(sender) else None
        except asyncio.CancelledError:
            error = "Telegram report delivery was interrupted; the private report remains saved and can be resumed."
            self._persist_pause(home, user_id, session_id, reason="report_delivery_interrupted",
                                last_error=error, topic_id=current.get("thread_id"),
                                artifact_status="ready", final_response_status="interrupted")
            raise
        except Exception as exc:
            logger.warning("DeepResearch report upload failed (%s)", type(exc).__name__)
            result = None

        if not getattr(result, "success", False) or not getattr(result, "message_id", None):
            error = str(getattr(result, "error", None) or "Telegram did not confirm the topic document message ID")
            message = f"The report is saved privately, but Telegram could not confirm its delivery in the research topic ({error})."
            self._persist_pause(home, user_id, session_id, reason="report_delivery_failed",
                                last_error=message, topic_id=current.get("thread_id"),
                                artifact_status="ready", final_response_status="failed")
            notify = await self._send_session_notice(
                source, current,
                f"Deep research is paused. {message} No text or Bot API fallback was used. Resume with `/deepresearch resume {session_id}`.",
                reply_to=str(anchor), event=run_event,
            )
            if not getattr(notify, "success", False):
                await self._send_session_notice(
                    source, current,
                    f"Deep research session `{session_id}` is paused because the report could not be delivered in its topic. The report remains private; no research content was sent outside the topic.",
                    reply_to=current.get("thread_id"),
                )
            return

        update_session(
            home, user_id, session_id, status="completed", completed_at=completed_at,
            latest_reply_message_id=str(result.message_id), last_error="",
            artifact_status="ready", final_response_status="delivered",
        )
        save_checkpoint(
            home, user_id, session_id, status="completed", last_error="",
            latest_reply_message_id=str(result.message_id),
        )
        if conversion_fallback:
            await self._send_session_notice(
                source, current,
                f"The requested {report_format.upper()} file could not be generated on this host, "
                "so the verified Markdown report was delivered instead. The Markdown report is complete; "
                "convert it with pandoc if a different format is needed.",
                reply_to=str(result.message_id), event=run_event,
            )

    def _reconcile_stale_running(self, source: Any, home: Any) -> None:
        """Mark starting/running sessions with no live gateway guard as interrupted.

        A gateway restart, a swallowed admission, or a crashed turn leaves a
        session stuck in starting/running forever; claiming only accepts
        paused/interrupted, so without this such sessions can never resume.
        A session with a live guard is untouched.
        """
        from gateway.deep_research_sessions import list_sessions, load_session, save_checkpoint, update_session
        try:
            candidates = list_sessions(home, source.user_id)
        except Exception:
            logger.warning("Could not list Telegram deep-research sessions for reconciliation", exc_info=True)
            return
        session_key_fn = getattr(self.adapter, "_event_session_key", None)
        active_sessions = getattr(self.adapter, "_active_sessions", {})
        for item in candidates:
            if item.get("status") not in {"starting", "running"}:
                continue
            if str(item.get("chat_id")) != str(source.chat_id):
                continue
            is_active = False
            if callable(session_key_fn):
                try:
                    event = SimpleNamespace(source=self._source_for_record(source, item))
                    key = str(session_key_fn(event))
                    guard = active_sessions.get(key)
                    is_active = guard is not None and not getattr(guard, "is_set", lambda: False)()
                except Exception:
                    logger.debug("Could not check active DeepResearch task while reconciling", exc_info=True)
            if not is_active:
                error = "The previous gateway run stopped before completion; its checkpoint is available to resume."
                try:
                    current = load_session(home, source.user_id, item["session_id"])
                    update_session(home, source.user_id, item["session_id"], status="interrupted",
                                   failure_class="gateway_interrupted", last_error=error)
                    # Refresh the artifact snapshots from disk as well as the
                    # status: the agent's writes after the last checkpoint are
                    # legitimate work, and without this validate_resume_state
                    # refuses the resume on stale snapshots forever. Every
                    # other validation check stays in force.
                    save_checkpoint(home, source.user_id, item["session_id"], status="interrupted",
                                    last_error=error,
                                    latest_reply_message_id=current.get("latest_reply_message_id"))
                except Exception:
                    logger.warning("Could not mark stale DeepResearch session interrupted", exc_info=True)

    async def _show_sessions(self, source: Any, *, reply_to: Any = None, session_id: str | None = None) -> None:
        from gateway.deep_research_sessions import list_sessions, load_session
        from plugins.platforms.telegram.mtproto_rich import inline_markup
        try:
            home = self.adapter._settings_ui()._home(source)
        except Exception:
            logger.warning("Could not list Telegram deep-research sessions", exc_info=True)
            home = None
            sessions = []
        if home is not None:
            self._reconcile_stale_running(source, home)
            try:
                candidates = ([load_session(home, source.user_id, session_id)] if session_id
                              else list_sessions(home, source.user_id))
                sessions = [item for item in candidates
                            if str(item.get("chat_id")) == str(source.chat_id)]
            except Exception:
                logger.warning("Could not list Telegram deep-research sessions", exc_info=True)
                sessions = []
        pending_actions = []
        if not sessions:
            text = "# Deep research sessions\n\nNo sessions found for your Telegram account in this Telegram chat."
            markup = None
        else:
            lines = ["# Deep research sessions", ""]
            resume_rows = []
            for item in sessions:
                query = str(item.get("query", "")).replace("\n", " ")
                if len(query) > 140:
                    query = query[:137] + "…"
                artifact_status = str(item.get("artifact_status") or "not checked").replace("_", " ")
                response_status = str(item.get("final_response_status") or "not recorded").replace("_", " ")
                lines.append(
                    f"• `{item.get('session_id')}` · **{item.get('status', 'unknown')}** · {query}\n"
                    f"  Artifact: {artifact_status} · Final delivery: {response_status}"
                )
                if item.get("status") in {"paused", "interrupted"}:
                    issue = str(item.get("last_error") or "Partial work is saved.").replace("\n", " ")[:240]
                    lines.append(f"  Last issue: {issue}")
                    token = uuid.uuid4().hex[:12]
                    resume_rows.append([(f"Resume {item.get('session_id')}", f"dr:{token}:resume", "primary")])
                    pending_actions.append((token, item))
            text = "\n".join(lines)
            markup = inline_markup(resume_rows) if resume_rows else None
        message_id = await self._send_card(source, text, markup, reply_to=reply_to)
        if message_id:
            for token, item in pending_actions:
                self._resume_actions[token] = {
                    "created_at": time.monotonic(), "source": source,
                    "session_id": str(item["session_id"]), "user_id": str(source.user_id),
                    "chat_id": str(source.chat_id), "message_id": str(message_id),
                }

    async def _send_card(self, source: Any, text: str, markup: Any = None, *, reply_to: Any = None) -> str | None:
        from plugins.platforms.telegram.mtproto_rich import send_request
        if self.adapter._client is None:
            logger.warning("Could not send Telegram deep-research Rich Message while disconnected")
            return None
        try:
            thread_id = getattr(source, "thread_id", None)
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(source.chat_id), text,
                reply_to=int(reply_to) if reply_to is not None else (int(thread_id) if thread_id else None),
                top_msg_id=int(thread_id) if thread_id else None,
                reply_markup=markup,
            ))
            return self.adapter._sent_message_id(response)
        except Exception:
            logger.warning("Could not send Telegram deep-research card", exc_info=True)
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
            logger.warning("Could not edit Telegram deep-research card", exc_info=True)

    async def _start_session(self, pending: dict[str, Any]) -> None:
        from gateway.deep_research_sessions import create_session

        source = pending["source"]
        user_id = str(pending["user_id"])
        home = self.adapter._settings_ui()._home(source)
        trigger_id = (pending.get("trigger_message_id")
                      or (getattr(pending.get("event"), "message_id", None) if pending.get("event") else None)
                      or pending.get("message_id"))
        config = pending.get("config") or _default_config()
        record = create_session(
            home, user_id, pending["query"], chat_id=source.chat_id,
            chat_type=getattr(source, "chat_type", "unknown"),
            thread_id=None, trigger_message_id=trigger_id,
            max_turns=config.get("max_turns"), report_format=config.get("report_format"),
            depth=config.get("depth"), custom_instructions=config.get("custom_instructions", ""),
        )
        await self._launch_session(
            source, record, trigger_message_id=trigger_id,
            confirmation_card_id=pending.get("message_id"), resumed=False,
        )

    async def _queue_resume(self, source: Any, session_id: str | None, *, trigger_message_id: Any, reply_to: Any,
                            accept_artifact_drift: bool = False) -> None:
        from gateway.deep_research_sessions import list_sessions, load_session

        try:
            home = self.adapter._settings_ui()._home(source)
            # A restart can leave a session stuck in starting/running with no
            # live run; reconcile first so it becomes resumable instead of
            # reporting "cannot be resumed" for a dead run.
            self._reconcile_stale_running(source, home)
            if session_id:
                record = load_session(home, source.user_id, session_id)
            else:
                candidates = [item for item in list_sessions(home, source.user_id)
                              if str(item.get("chat_id")) == str(source.chat_id)
                              and item.get("status") in {"paused", "interrupted"}]
                record = candidates[0] if candidates else None
        except Exception:
            await self.adapter.send(source.chat_id, "That research session was not found for this Telegram account.",
                                    reply_to=reply_to)
            return
        if not record:
            await self.adapter.send(source.chat_id,
                                    "No paused or interrupted research session is available in this Telegram chat.",
                                    reply_to=reply_to)
            return
        session_id = str(record["session_id"])
        if str(record.get("chat_id")) != str(source.chat_id):
            await self.adapter.send(source.chat_id, "This session can only be resumed in the Telegram chat where it was created.",
                                    reply_to=reply_to)
            return
        if record.get("status") not in {"paused", "interrupted"}:
            await self.adapter.send(source.chat_id, f"Session `{session_id}` is `{record.get('status', 'unknown')}` and cannot be resumed.",
                                    reply_to=reply_to)
            return
        token = uuid.uuid4().hex[:12]
        task = asyncio.create_task(
            self._resume_session(source, session_id, trigger_message_id=trigger_message_id,
                                 accept_artifact_drift=accept_artifact_drift),
            name=f"telegram-deep-research-resume-{session_id}",
        )
        self._tasks[token] = task
        task.add_done_callback(lambda _task, key=token: self._tasks.pop(key, None))

    async def _resume_session(self, source: Any, session_id: str, *, trigger_message_id: Any = None,
                              accept_artifact_drift: bool = False) -> None:
        from gateway.deep_research_sessions import claim_session_for_resume, load_session, validate_resume_state

        try:
            home = self.adapter._settings_ui()._home(source)
            record = load_session(home, source.user_id, session_id)
        except Exception:
            await self.adapter.send(source.chat_id, "That research session was not found for this Telegram account.",
                                    reply_to=trigger_message_id)
            return
        if str(record.get("chat_id")) != str(source.chat_id):
            await self.adapter.send(source.chat_id, "This session can only be resumed in its original Telegram chat.",
                                    reply_to=trigger_message_id)
            return
        if record.get("status") not in {"paused", "interrupted"}:
            await self.adapter.send(source.chat_id, f"Session `{session_id}` is `{record.get('status', 'unknown')}` and cannot be resumed.",
                                    reply_to=trigger_message_id)
            return
        session_source = self._source_for_record(source, record)
        session_key_fn = getattr(self.adapter, "_event_session_key", None)
        active = getattr(self.adapter, "_active_sessions", {})
        if callable(session_key_fn):
            probe = SimpleNamespace(source=session_source)
            try:
                key = str(session_key_fn(probe))
                guard = active.get(key)
                if guard is not None and not getattr(guard, "is_set", lambda: False)():
                    await self._send_session_notice(
                        source, record, f"Session `{session_id}` is already active in its topic.",
                        reply_to=record.get("latest_reply_message_id") or record.get("thread_id"),
                    )
                    return
            except Exception:
                logger.debug("Could not verify active DeepResearch session before resume", exc_info=True)
        try:
            resume_checkpoint = validate_resume_state(
                home, source.user_id, session_id, accept_artifact_drift=accept_artifact_drift)
        except Exception as exc:
            # Never claim, rewrite, reset, or repair disputed saved work automatically.
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            notice = (
                f"Resume blocked for session `{session_id}`: {detail}. "
                "No saved files were changed. Please review this discrepancy and tell me how to reconcile it before continuing."
            )
            # Offer the explicit owner reconcile: accept the current on-disk
            # notes/report as the new snapshots and resume. Identity, topic,
            # task-ledger, and evidence discrepancies still block, and the
            # button is only armed for this account, chat, and card.
            from plugins.platforms.telegram.mtproto_rich import inline_markup
            reconcile_token = uuid.uuid4().hex[:12]
            reconcile_markup = inline_markup([[
                ("Accept current files & resume", f"dr:{reconcile_token}:reconcile", "primary"),
            ]])
            card_id = await self._send_card(
                session_source,
                notice + "\n\nTap below to accept the current on-disk files and resume, or review the files first. "
                f"You can also run `/deepresearch resume --accept-current {session_id}`.",
                reconcile_markup,
                reply_to=record.get("thread_id"),
            )
            if card_id:
                self._resume_actions[reconcile_token] = {
                    "created_at": time.monotonic(), "source": session_source,
                    "session_id": str(session_id), "user_id": str(source.user_id),
                    "chat_id": str(source.chat_id), "message_id": str(card_id),
                    "accept_artifact_drift": True,
                }
                return
            result = await self._send_session_notice(
                source, record, notice,
                reply_to=record.get("latest_reply_message_id") or record.get("thread_id"),
            )
            if not getattr(result, "success", False):
                await self._send_session_notice(
                    source, record,
                    f"Session `{session_id}` cannot resume until its saved-state discrepancy is reviewed in the original topic. No saved files were changed.",
                    reply_to=record.get("thread_id"),
                )
            return
        try:
            claimed = claim_session_for_resume(home, source.user_id, session_id,
                                               accept_artifact_drift=accept_artifact_drift)
        except Exception:
            logger.warning("Could not claim DeepResearch session for resume", exc_info=True)
            claimed = None
        if claimed is None:
            await self._send_session_notice(
                source, record,
                f"Session `{session_id}` is already resuming or is no longer paused/interrupted.",
                reply_to=record.get("latest_reply_message_id") or record.get("thread_id"),
            )
            return
        try:
            await self._launch_session(
                source, claimed, trigger_message_id=trigger_message_id,
                confirmation_card_id=None, resumed=True, resume_checkpoint=resume_checkpoint,
            )
        except Exception as exc:
            logger.exception("Unexpected DeepResearch resume initialization failure")
            current = load_session(home, source.user_id, session_id)
            if current.get("status") == "starting":
                reason = f"Resume initialization failed ({type(exc).__name__}); the saved work remains available."
                self._persist_pause(home, str(source.user_id), session_id,
                                    reason="resume_start_failed", last_error=reason,
                                    topic_id=current.get("thread_id"))
                await self._send_session_notice(
                    source, current,
                    f"Deep research is paused. {reason} Retry with `/deepresearch resume {session_id}`.",
                    reply_to=current.get("latest_reply_message_id") or current.get("thread_id"),
                )

    @staticmethod
    def _source_for_record(source: Any, record: dict[str, Any]) -> Any:
        try:
            return dataclasses.replace(
                source,
                chat_id=str(record.get("chat_id") or source.chat_id),
                chat_type=str(record.get("chat_type") or getattr(source, "chat_type", "unknown")),
                thread_id=str(record.get("thread_id")) if record.get("thread_id") else None,
            )
        except Exception:
            return SimpleNamespace(
                platform=getattr(source, "platform", "telegram"),
                chat_id=str(record.get("chat_id") or source.chat_id),
                user_id=str(record.get("user_id") or source.user_id),
                user_name=getattr(source, "user_name", None),
                chat_type=str(record.get("chat_type") or getattr(source, "chat_type", "unknown")),
                thread_id=str(record.get("thread_id")) if record.get("thread_id") else None,
                profile=getattr(source, "profile", None),
            )

    @staticmethod
    def _topic_metadata(source: Any, event: Any = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "telegram_strict_thread": True,
            "telegram_send_in_topic": True,
        }
        if event is None or not isinstance(getattr(event, "metadata", None), dict):
            return metadata
        event_metadata = event.metadata
        research = event_metadata.get("deep_research")
        if not isinstance(research, dict):
            return metadata
        metadata.update({
            "deep_research": research,
            "deep_research_session_id": str(research.get("session_id") or ""),
            "deep_research_user_id": str(getattr(source, "user_id", "") or ""),
            "deep_research_content": True,
            "telegram_task_reply_chain": True,
            "telegram_task_event_metadata": event_metadata,
            "telegram_task_source": source,
        })
        return metadata

    async def _send_session_notice(
        self, source: Any, record: dict[str, Any], text: str, *,
        reply_to: Any = None, event: Any = None,
    ) -> Any:
        """Send lifecycle status only to the destination saved for this research session."""
        topic_source = self._source_for_record(source, record)
        if not getattr(topic_source, "thread_id", None):
            logger.warning("Refusing DeepResearch notice without its saved Telegram topic")
            return None
        return await self.adapter.send(
            topic_source.chat_id, text, reply_to=reply_to,
            metadata=self._topic_metadata(topic_source, event),
        )

    def _persist_pause(self, home: Any, user_id: str, session_id: str, *, reason: str,
                       last_error: str, topic_id: Any = None, artifact_status: str | None = None,
                       final_response_status: str | None = None) -> None:
        from gateway.deep_research_sessions import load_session, save_checkpoint, update_session

        current = load_session(home, user_id, session_id)
        changes = {
            "status": "paused", "failure_class": reason, "last_error": last_error,
            "thread_id": str(topic_id) if topic_id else current.get("thread_id"),
        }
        if artifact_status is not None:
            changes["artifact_status"] = artifact_status
        if final_response_status is not None:
            changes["final_response_status"] = final_response_status
        update_session(home, user_id, session_id, **changes)
        save_checkpoint(
            home, user_id, session_id, status="paused", phase=current.get("phase") or "research",
            last_error=last_error, latest_reply_message_id=current.get("latest_reply_message_id"),
        )

    async def _pause_before_start(self, *, source: Any, home: Any, session_id: str,
                                  trigger_message_id: Any, reason: str, message: str,
                                  topic_source: Any = None, confirmation_card_id: Any = None) -> None:
        self._persist_pause(home, str(source.user_id), session_id, reason=reason,
                            last_error=message, topic_id=getattr(topic_source, "thread_id", None))
        if confirmation_card_id and topic_source is None:
            await self._edit_card(
                source, str(confirmation_card_id),
                f"# Deep research paused\n\n{message}\n\nSession `{session_id}` can be resumed with `/deepresearch resume {session_id}`.",
                None,
            )
        if topic_source is not None:
            await self.adapter.send(
                topic_source.chat_id,
                f"Deep research is paused before starting. {message} No research content was sent outside this topic.",
                reply_to=topic_source.thread_id,
                metadata=self._topic_metadata(topic_source),
            )
            # A failed topic send must not publish the failure in the invocation
            # chat or another topic. Keep its durable paused state for recovery.
            return
        original_metadata = {"thread_id": source.thread_id} if getattr(source, "thread_id", None) else None
        await self.adapter.send(
            source.chat_id,
            f"Deep research session `{session_id}` is paused. {message} Resume with `/deepresearch resume {session_id}`.",
            reply_to=trigger_message_id, metadata=original_metadata,
        )

    @staticmethod
    def _resume_prompt(record: dict[str, Any], checkpoint: dict[str, Any]) -> str:
        import json
        remaining = [item for item in checkpoint.get("todos", [])
                     if item.get("status") in {"pending", "in_progress"}]
        snapshot = json.dumps(checkpoint, ensure_ascii=False)
        return (
            f"Resume the existing DeepResearch session {record['session_id']} for this original question: {record['query']}\n\n"
            "Continue only from this durable state; do not restart from scratch, reset tasks, or overwrite useful findings. "
            "The full checkpoint below contains the source ledger, draft/report, task statuses and measured durations, "
            "successful evidence, recorded tool outcomes, previous issues and delivery state. "
            "First inspect checkpoint.json, research_notes.md and report.md in the private session folder. "
            "If anything disagrees or required history is missing, pause and ask the owner; never guess. "
            "Inspect the previous failure and continue the unfinished verification, not a new investigation. "
            "If the saved tasks are all completed but source/report checks still failed, preserve those historical statuses "
            "and add only the concrete repair and re-verification tasks needed for that failure. Mark the relevant new task "
            "in_progress before doing its tool work; do not reset completed tasks or invent activity merely to animate the card. "
            "Do not say research is complete while tasks remain pending/in_progress or validation is incomplete. "
            "A report saved/ready is not research complete or delivered. Only Telegram's delivery receipt confirms delivery.\n"
            f"Remaining work (JSON): {json.dumps(remaining, ensure_ascii=False)}\n"
            f"Actual saved checkpoint (JSON): {snapshot}\n"
        )

    async def _launch_session(self, source: Any, record: dict[str, Any], *,
                              trigger_message_id: Any, confirmation_card_id: Any,
                              resumed: bool, resume_checkpoint: dict[str, Any] | None = None) -> None:
        from gateway.deep_research_sessions import (
            load_checkpoint, load_session, save_checkpoint, update_checkpoint_metadata, update_session,
        )
        from gateway.platforms.event import MessageEvent, MessageType

        user_id = str(record["user_id"])
        home = self.adapter._settings_ui()._home(source)
        session_id = str(record["session_id"])
        topic_id = record.get("thread_id")
        topic_source = None
        if not topic_id:
            topic_id = await self.adapter._create_topic_from_message(
                source, self._topic_title(record["query"]), reply_to=trigger_message_id,
                welcome_text="✅ Research topic created. The investigation will start here.",
                random_id=record.get("topic_create_random_id"),
            )
            if not topic_id:
                detail = str(getattr(self.adapter, "_topic_creation_error", "") or (
                    "Enable Telegram topics and grant the bot permission to create topics and send messages."
                ))
                await self._pause_before_start(
                    source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                    reason="topic_creation_failed", message=detail, confirmation_card_id=confirmation_card_id,
                )
                return
            topic_id = str(topic_id)
            update_session(home, user_id, session_id, thread_id=topic_id)
        topic_source = self._source_for_record(source, {**record, "thread_id": str(topic_id)})
        welcome_id = getattr(self.adapter, "_topic_welcome_message_id", lambda *_: None)(source.chat_id, topic_id)
        current = load_session(home, user_id, session_id)
        latest_anchor = current.get("latest_reply_message_id")
        if not latest_anchor:
            latest_anchor = welcome_id
        if not latest_anchor:
            welcome_result = await self.adapter.send(
                topic_source.chat_id, "✅ Research session is ready. The investigation will start here.",
                reply_to=str(topic_id), metadata=self._topic_metadata(topic_source),
            )
            if getattr(welcome_result, "success", False) and getattr(welcome_result, "message_id", None):
                welcome_id = str(welcome_result.message_id)
                latest_anchor = welcome_id
            else:
                detail = str(getattr(self.adapter, "_topic_creation_error", "") or
                             getattr(welcome_result, "error", "The bot could not send a welcome message in the new topic."))
                await self._pause_before_start(
                    source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                    reason="topic_send_permission", message=detail, topic_source=topic_source,
                    confirmation_card_id=confirmation_card_id,
                )
                return
        if not latest_anchor:
            await self._pause_before_start(
                source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                reason="topic_anchor_unavailable", message="Telegram did not provide a usable topic message ID.",
                topic_source=topic_source, confirmation_card_id=confirmation_card_id,
            )
            return

        update_session(home, user_id, session_id, thread_id=str(topic_id), latest_reply_message_id=str(latest_anchor))
        update_checkpoint_metadata(home, user_id, session_id, latest_reply_message_id=str(latest_anchor))
        query = str(current.get("query") or record.get("query") or "")
        if trigger_message_id:
            ack = ("Deep research is resuming in its original Telegram topic." if resumed
                   else "Deep research is starting in its dedicated Telegram topic.")
            ack_result = await self.adapter.send(
                topic_source.chat_id, f"{ack} Session `{session_id}`.",
                reply_to=latest_anchor, metadata=self._topic_metadata(topic_source),
            )
            if ack_result is not None and not getattr(ack_result, "success", False):
                # The topic exists but rejected the first message: every later
                # send would fail the same way. Pause loudly now, on the card
                # the user is looking at, instead of running a silent session.
                error = (
                    f"The research topic rejected the first message "
                    f"({getattr(ack_result, 'error', 'delivery failed')}); "
                    "the bot may lack permission to send in the topic."
                )
                self._persist_pause(home, user_id, session_id, reason="topic_send_permission",
                                    last_error=error, topic_id=topic_id)
                if confirmation_card_id:
                    await self._edit_card(
                        source, str(confirmation_card_id),
                        f"# Deep research paused\n\n{error}\n\nSession `{session_id}` can be resumed with `/deepresearch resume {session_id}`.",
                        None,
                    )
                await self.adapter.send(
                    topic_source.chat_id,
                    f"Deep research is paused before starting. {error} No research content was sent outside this topic.",
                    reply_to=str(topic_id), metadata=self._topic_metadata(topic_source),
                )
                return
        if confirmation_card_id:
            format_label = _REPORT_FORMAT_LABELS.get(str(current.get("report_format") or "md"), "Markdown")
            depth_label = _DEPTH_LABELS.get(str(current.get("depth") or "deep"), "Deep")
            await self._edit_card(
                source, str(confirmation_card_id),
                f"# Deep research started\n\nThe investigation is running in its dedicated Telegram topic.\n\nSession `{session_id}` · {current.get('max_turns', 150)} turns max · {format_label} report · {depth_label} depth.",
                None,
            )

        stored_checkpoint = (resume_checkpoint if resume_checkpoint is not None
                             else load_checkpoint(home, user_id, session_id)) if resumed else {
            "phase": "starting", "todos": [], "last_error": "",
        }
        # Pass all durable evidence/artifacts/history to the runner, not only todos.
        checkpoint = dict(stored_checkpoint)
        research_metadata = {
            "deep_research": {
                "session_id": session_id,
                "max_turns": int(current.get("max_turns", 150)),
                "report_format": str(current.get("report_format") or "md"),
                "depth": str(current.get("depth") or "deep"),
                "shell_access": True, "compaction_allowed": True,
                "tools_allowed": True, "mcps_allowed": True,
                "custom_instructions": str(current.get("custom_instructions") or ""),
                "latest_reply_message_id": str(latest_anchor),
                "topic_welcome_message_id": str(welcome_id or latest_anchor),
                "checkpoint": checkpoint,
                "progress_message_id": current.get("progress_message_id"),
                "resumed": bool(resumed),
            },
            "thread_id": str(topic_id),
            "telegram_task_reply_chain": True,
            "telegram_task_reply_to_message_id": str(latest_anchor),
        }
        run_text = self._resume_prompt(current, checkpoint) if resumed else f"Conduct deep research on: {query}"
        run_event = MessageEvent(
            text=run_text, message_type=MessageType.TEXT, user_id=user_id,
            user_name=source.user_name, source=topic_source,
            message_id=str(latest_anchor), metadata=research_metadata,
        )
        session_key_fn = getattr(self.adapter, "_event_session_key", None)
        register_callback = getattr(self.adapter, "register_post_delivery_callback", None)
        if not callable(session_key_fn) or not callable(register_callback):
            await self._pause_before_start(
                source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                reason="gateway_delivery_tracking_unavailable",
                message="The gateway cannot track completion and attach the report safely.",
                topic_source=topic_source, confirmation_card_id=confirmation_card_id,
            )
            return
        session_key = str(session_key_fn(run_event))
        if not session_key:
            await self._pause_before_start(
                source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                reason="gateway_session_key_unavailable", message="The gateway returned an empty session key.",
                topic_source=topic_source, confirmation_card_id=confirmation_card_id,
            )
            return
        run_event.metadata["gateway_session_key"] = session_key

        async def _after_delivery() -> None:
            await self._complete_session_after_turn(
                home=home, user_id=user_id, session_id=session_id,
                source=topic_source, run_event=run_event, session_key=session_key,
                reply_to=None,
            )

        register_callback(session_key, _after_delivery)
        update_session(home, user_id, session_id, status="running", failure_class=None, last_error="")
        save_checkpoint(home, user_id, session_id, status="running", last_error="",
                        latest_reply_message_id=str(latest_anchor))
        memory_token = None
        if getattr(topic_source, "chat_type", None) == "dm":
            try:
                from gateway.telegram_personal_settings import (
                    load_personal_settings, reset_personal_memory_context,
                    set_personal_memory_context,
                )
                personal = load_personal_settings(home, user_id)
                memory_token = set_personal_memory_context(
                    home, user_id,
                    memory_enabled=personal.get("memory_enabled", True),
                    self_learning_enabled=personal.get("self_learning_enabled", True),
                )
            except Exception:
                logger.warning("Could not establish private memory scope for deep research", exc_info=True)
                await self._pause_before_start(
                    source=source, home=home, session_id=session_id, trigger_message_id=trigger_message_id,
                    reason="private_scope_unavailable", message="Private session scope could not be established safely.",
                    topic_source=topic_source, confirmation_card_id=confirmation_card_id,
                )
                return
        try:
            await self.adapter.handle_message(run_event)
            if getattr(run_event, "_gateway_accepted", None) is False:
                error = "The gateway did not admit the research turn; no research content was delivered."
                self._persist_pause(home, user_id, session_id, reason="gateway_not_accepted",
                                    last_error=error, topic_id=topic_id)
                await self.adapter.send(
                    topic_source.chat_id,
                    f"Deep research is paused. {error} Resume with `/deepresearch resume {session_id}`.",
                    reply_to=latest_anchor, metadata=self._topic_metadata(topic_source, run_event),
                )
        except asyncio.CancelledError:
            self._persist_pause(home, user_id, session_id, reason="interrupted",
                                last_error="Research startup was interrupted; the session can be resumed.", topic_id=topic_id)
            update_session(home, user_id, session_id, status="interrupted")
            save_checkpoint(home, user_id, session_id, status="interrupted",
                            last_error="Research startup was interrupted; the session can be resumed.",
                            latest_reply_message_id=str(latest_anchor))
            raise
        except Exception as exc:
            logger.exception("Telegram deep-research session failed to enter the agent loop")
            error = f"Research startup failed ({type(exc).__name__}); partial work is saved and can be resumed."
            self._persist_pause(home, user_id, session_id, reason="agent_start_failed",
                                last_error=error, topic_id=topic_id)
            await self.adapter.send(
                topic_source.chat_id,
                f"Deep research is paused. {error} Resume with `/deepresearch resume {session_id}`.",
                reply_to=latest_anchor, metadata=self._topic_metadata(topic_source, run_event),
            )
        finally:
            if memory_token is not None:
                try:
                    reset_personal_memory_context(memory_token)
                except Exception:
                    logger.debug("Could not reset private DeepResearch memory scope", exc_info=True)
        # Admission watchdog: handle_message returns as soon as the background
        # task spawns, so a turn refused inside session resolution would
        # otherwise leave this session stuck in running with an empty topic
        # and no pause, no progress card, and no way to resume it.
        watch_key = f"watch:{session_id}"
        watchdog = asyncio.create_task(
            self._watch_launch(source=source, record=record, run_event=run_event,
                               session_key=session_key, confirmation_card_id=confirmation_card_id),
            name=f"telegram-deep-research-watch-{session_id}",
        )
        self._tasks[watch_key] = watchdog
        watchdog.add_done_callback(lambda _task, key=watch_key: self._tasks.pop(key, None))

    async def _watch_launch(self, *, source: Any, record: dict[str, Any], run_event: Any,
                            session_key: str, confirmation_card_id: Any) -> None:
        """Pause loudly when a launched research turn never actually started.

        After a grace period, a session still marked starting/running with no
        live gateway guard and no progress card has been swallowed (admission
        refusal, crash before the first tool call, or a restart that dropped
        the background task). Pausing makes it resumable and tells the owner
        exactly why nothing appeared in the topic.
        """
        from gateway.deep_research_sessions import load_session

        await asyncio.sleep(_LAUNCH_WATCH_GRACE_SECONDS)
        try:
            home = self.adapter._settings_ui()._home(source)
            session_id = str(record["session_id"])
            current = load_session(home, str(source.user_id), session_id)
            if current.get("status") not in {"starting", "running"}:
                return
            guard = getattr(self.adapter, "_active_sessions", {}).get(session_key)
            guard_live = guard is not None and not getattr(guard, "is_set", lambda: False)()
            if guard_live or current.get("progress_message_id"):
                return
            reason = str(getattr(run_event, "_deep_research_route_error", "") or "").strip()
            if not reason and getattr(run_event, "_gateway_accepted", None) is False:
                reason = "The gateway did not admit the research turn."
            if not reason:
                reason = ("The research turn never started: no gateway task is live for its topic "
                          "and no progress card was created.")
            self._persist_pause(home, str(source.user_id), session_id,
                                reason="launch_never_started", last_error=reason,
                                topic_id=current.get("thread_id"))
            topic_source = self._source_for_record(source, current)
            await self.adapter.send(
                topic_source.chat_id,
                f"Deep research is paused. {reason} No research content was sent. "
                f"Resume with `/deepresearch resume {session_id}`.",
                reply_to=current.get("thread_id"), metadata=self._topic_metadata(topic_source),
            )
            if confirmation_card_id:
                await self._edit_card(
                    source, str(confirmation_card_id),
                    f"# Deep research paused\n\n{reason}\n\nSession `{session_id}` can be resumed with `/deepresearch resume {session_id}`.",
                    None,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("DeepResearch launch watchdog failed", exc_info=True)

    @staticmethod
    def _topic_title(query: str) -> str:
        compact = " ".join(str(query).split())
        title = "Research: " + compact[:100]
        while len(title.encode("utf-8")) > 128:
            title = title[:-1]
        return title
