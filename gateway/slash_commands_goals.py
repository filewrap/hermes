"""Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review.
Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``."""

from __future__ import annotations

import logging
import re

from agent.i18n import t
from gateway.platforms.event import MessageEvent, MessageType

# Log-record parity with gateway/run.py and the origin module.
logger = logging.getLogger("gateway.run")


def _rich_lines(heading: str, *blocks: str) -> str:
    """``## <heading>`` over *blocks*, one blank line between blocks.

    Empty blocks are dropped, so absent fields never leave a placeholder.
    """
    parts = [f"## {heading}"]
    for block in blocks:
        block = (block or "").strip()
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def _labelled_rows(rows) -> str:
    """``- **Label:** value`` lines for ``(label, value)`` pairs that have a value."""
    return "\n".join(
        f"- **{label}:** {value}" for label, value in rows if value not in (None, "")
    )


def _strip_leading_icon(text: str) -> str:
    """Drop a leading status pictograph so parsers see the words."""
    return re.sub(r"^[^\w`\"'(/]+", "", text or "").strip()


_PROGRESS_RE = re.compile(r"^(\d+)/(\d+) (turns|runs|budget)$")
_COUNT_RE = re.compile(r"^(\d+) (subgoals?|gates?)$")
_TICKS_RE = re.compile(r"^(\d+) ticks?$")


def _meta_status_word(token: str) -> str:
    """The status word a token carries ("" when it carries none)."""
    if token in {"active", "paused", "done", "cleared"}:
        return token
    if token.startswith("parked"):
        return "active"  # a parked goal/loop is an active one waiting on something
    return ""


def _meta_token_row(token: str, *, interval_label: str):
    """``(label, value)`` for one status parenthetical token; None for status words."""
    if token in {"active", "paused", "done", "cleared"}:
        return None
    if token.startswith("parked"):
        return ("Waiting", token)
    if token.startswith("every ") or token == "self-paced":
        return (interval_label, token)
    if _PROGRESS_RE.match(token):
        return ("Progress", token)
    if token.startswith("next in "):
        return ("Next", token[len("next in "):])
    if token.startswith("fired "):
        return ("Fired", token[len("fired "):])
    if _TICKS_RE.match(token):
        return ("Fired", token)
    if token == "contract":
        return ("Contract", "set")
    if (count_match := _COUNT_RE.match(token)):
        return (count_match.group(2).capitalize(), count_match.group(1))
    if token.startswith("until: "):
        return ("Stops when", token[len("until: "):])
    if token == "wakeup running":
        return ("Next", token)
    return ("Detail", token)


def _parse_status_meta(meta: str, *, interval_label: str = "Interval"):
    """``(status, rows, reason)`` from a ``(active, 0/10 turns, ...)`` parenthetical.

    Tokens are comma-separated; a trailing `` — <reason>`` on any token is a
    pause/wait reason. Unknown tokens become detail rows, never dropped.
    """
    status, reason, rows = "", "", []
    for token in (tok.strip() for tok in (meta or "").split(",") if tok.strip()):
        if " — " in token:
            token, reason = token.split(" — ", 1)
            token, reason = token.strip(), reason.strip()
        status = _meta_status_word(token) or status
        if (row := _meta_token_row(token, interval_label=interval_label)) is not None:
            rows.append(row)
    return status, rows, reason


def _parse_goal_status_line(line: str):
    """``(status, goal, meta_rows, reason)`` for a ``Goal (...): <goal>`` line, else None."""
    match = re.match(
        r"Goal \((?P<meta>[^)]*)\): (?P<goal>[\s\S]*)$", _strip_leading_icon(line or "")
    )
    if not match:
        return None
    status, rows, reason = _parse_status_meta(match.group("meta"))
    return status, match.group("goal").strip(), rows, reason


def _goal_status_rows(parsed) -> str:
    """Labelled rows for a parsed goal status line: status, goal, then meta rows."""
    status, goal, rows, reason = parsed
    out = ([("Status", status)] if status else []) + [("Goal", goal)] + rows
    if reason:
        out.append(("Reason", reason))
    return _labelled_rows(out)


def _rich_goal_status_view(lines: list[str], parsed) -> str:
    """Status/show view: status rows, then a contract section, then trailing notes."""
    blocks = [_goal_status_rows(parsed)]
    rest = [line for line in lines[1:] if line.strip()]
    index = 0
    while index < len(rest):
        line = rest[index]
        if line in {"Completion contract:", "Drafted completion contract:"}:
            block = [ln for ln in rest[index + 1:] if ln.startswith("- ")]
            if block:
                blocks.append("## Completion contract\n\n" + "\n".join(block))
                index += 1 + len(block)
                continue
            index += 1
            continue
        if line.startswith("- "):
            block = [ln for ln in rest[index:] if ln.startswith("- ")]
            blocks.append("## Completion contract\n\n" + "\n".join(block))
            index += len(block)
            continue
        blocks.append(line)
        index += 1
    return _rich_lines("Status", *blocks)


def _rich_goal_set(lines: list[str]) -> str:
    """``## Goal set`` from a set/draft reply: headline rows, contract, notes."""
    match = re.match(
        r"Goal set \((\d+)-turn budget\): (.*)$", _strip_leading_icon(lines[0])
    )
    rows = [("Goal", match.group(2).strip()), ("Budget", f"{match.group(1)} turns")]
    sections: list[str] = []
    paragraphs: list[str] = []
    index = 1
    while index < len(lines):
        line = lines[index].strip()
        if line == "(replaced the previous goal)":
            nxt = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if nxt.startswith("was: "):
                rows.append(("Previous goal", nxt[len("was: "):]))
                index += 2
                continue
            index += 1
            continue
        if line in {"Completion contract:", "Drafted completion contract:"}:
            block = [ln for ln in lines[index + 1:] if ln.startswith("- ")]
            if block:
                sections.append("## Completion contract\n\n" + "\n".join(block))
                index += 1 + len(block)
                continue
            index += 1
            continue
        if line.startswith("- "):
            block = [ln for ln in lines[index:] if ln.startswith("- ")]
            sections.append("## Completion contract\n\n" + "\n".join(block))
            index += len(block)
            continue
        if line:
            paragraphs.append(line)
        index += 1
    return _rich_lines("Goal set", _labelled_rows(rows), *sections, *paragraphs)


def _rich_goal_gate(text: str) -> str | None:
    """Rich view for a /goal gate reply, else None."""
    head = _strip_leading_icon(text.split("\n", 1)[0])
    match = re.match(
        r"Gate added: \$ (?P<command>.+?) \((?P<retries>\d+) retries, "
        r"(?P<timeout>\d+)s timeout\)\. (?P<tail>.*)$",
        head,
    )
    if match:
        rows = [
            ("Command", f"`$ {match.group('command')}`"),
            ("Retries", match.group("retries")),
            ("Timeout", f"{match.group('timeout')}s"),
        ]
        return _rich_lines("Gate added", _labelled_rows(rows), match.group("tail"))
    match = re.match(r"Gate removed: \$ (?P<command>.+)$", head)
    if match:
        return _rich_lines(
            "Gate removed", _labelled_rows([("Command", f"`$ {match.group('command')}`")])
        )
    match = re.match(r"Cleared (?P<count>\d+) gates?\.$", head)
    if match:
        return _rich_lines(
            "Gates cleared",
            _labelled_rows([("Cleared", f"{match.group('count')} gates")]),
        )
    if head.startswith("- ") or head.startswith("(no quality gates") or head.startswith("(no active goal"):
        return _rich_lines("Quality gates", text)
    return None


def _rich_goal_text(text: str) -> str:
    """Reformat one /goal reply as card lines.

    Shapes the shared engine returns are matched first; anything unknown keeps
    its text under a ``## Goal`` heading (``## Usage`` for usage lines). Only the
    card text is reformatted — the plain-text fallback keeps the original.
    """
    text = (text or "").strip()
    if not text:
        return "## Goal"
    lines = text.split("\n")
    head = _strip_leading_icon(lines[0])
    if head.startswith("No active goal"):
        return _rich_lines("No active goal", head[len("No active goal."):].strip())
    if re.match(r"Goal set \(\d+-turn budget\): ", head):
        return _rich_goal_set(lines)
    if head.startswith("Goal paused: "):
        return _rich_lines(
            "Goal paused",
            _labelled_rows([("Goal", head[len("Goal paused: "):])]),
            *lines[1:],
        )
    if head.startswith("Goal resumed: "):
        return _rich_lines(
            "Goal resumed",
            _labelled_rows([("Goal", head[len("Goal resumed: "):])]),
            *lines[1:],
        )
    if head == "Goal cleared.":
        return "## Goal cleared"
    match = re.match(r"Goal parked on (?P<target>.+?)\. Loop pauses until it exits\.$", head)
    if match:
        return _rich_lines(
            "Goal parked", _labelled_rows([("Parked on", match.group("target"))])
        )
    if head.startswith("Wait barrier cleared"):
        return "## Wait barrier cleared"
    if (gated := _rich_goal_gate(text)) is not None:
        return gated
    if (parsed := _parse_goal_status_line(lines[0])) is not None:
        return _rich_goal_status_view(lines, parsed)
    if head.startswith("Usage:"):
        return _rich_lines("Usage", text)
    return _rich_lines("Goal", text)


def _rich_heartbeat_text(text: str) -> str:
    """Reformat one /heartbeat reply as card lines (card text only)."""
    text = (text or "").strip()
    if not text:
        return "## Heartbeat"
    lines = text.split("\n")
    head = _strip_leading_icon(lines[0])
    match = re.match(r"Heartbeat \((?P<meta>[^)]*)\): (?P<prompt>[\s\S]*)$", head)
    if match:
        status, rows, reason = _parse_status_meta(match.group("meta"))
        # HeartbeatManager only names the status when it is not active.
        status = status or "active"
        out = ([("Status", status)] if status else []) + [
            ("Prompt", match.group("prompt").strip())
        ] + rows
        if reason:
            out.append(("Reason", reason))
        return _rich_lines("Status", _labelled_rows(out))
    match = re.match(
        r"Heartbeat set \(every (?P<interval>[^)]+)\): (?P<prompt>[\s\S]*)$", head
    )
    if match:
        rows = [
            ("Interval", f"every {match.group('interval')}"),
            ("Prompt", match.group("prompt").strip()),
        ]
        return _rich_lines("Heartbeat set", _labelled_rows(rows), *lines[1:])
    match = re.match(r"Heartbeat paused: (?P<prompt>.*)$", head)
    if match:
        return _rich_lines(
            "Heartbeat paused", _labelled_rows([("Prompt", match.group("prompt"))])
        )
    match = re.match(
        r"Heartbeat resumed \(every (?P<interval>[^)]+)\): (?P<prompt>.*)$", head
    )
    if match:
        rows = [
            ("Interval", f"every {match.group('interval')}"),
            ("Prompt", match.group("prompt")),
        ]
        return _rich_lines("Heartbeat resumed", _labelled_rows(rows))
    if head == "Heartbeat cleared.":
        return "## Heartbeat cleared"
    if head.startswith("No heartbeat."):
        return _rich_lines("No heartbeat", head[len("No heartbeat."):].strip())
    if head.startswith("Usage:"):
        return _rich_lines("Usage", text)
    return _rich_lines("Heartbeat", text)


def _rich_subgoal_text(text: str) -> str:
    """Reformat one /subgoal reply as card lines (card text only)."""
    text = (text or "").strip()
    if not text:
        return "## Subgoals"
    lines = text.split("\n")
    head = _strip_leading_icon(lines[0])
    if head.startswith("No active goal"):
        return _rich_lines("No active goal", head[len("No active goal."):].strip())
    if (parsed := _parse_goal_status_line(lines[0])) is not None:
        blocks = [_goal_status_rows(parsed)]
        rest = [line for line in lines[1:] if line.strip()]
        if rest:
            blocks.append("\n".join(rest))
        return _rich_lines("Subgoals", *blocks)
    match = re.match(r"(Added|Removed) subgoal (\d+): (.*)$", head)
    if match:
        verb, index, subgoal = match.groups()
        return _rich_lines(
            f"Subgoal {verb.lower()}",
            _labelled_rows([(f"Subgoal {index}", subgoal)]),
        )
    match = re.match(r"Cleared (\d+) subgoals?\.$", head)
    if match:
        return _rich_lines(
            "Subgoals cleared",
            _labelled_rows([("Cleared", f"{match.group(1)} subgoals")]),
        )
    if head.startswith("Usage:"):
        return _rich_lines("Usage", text)
    return _rich_lines("Subgoal", text)


def _code_loop_commands(line: str) -> str:
    """Wrap ``/loop <verb>`` mentions in code spans, expanding bare verbs in control lists."""
    line = re.sub(r"(?<!`)(/loop(?: [a-z-]+)?)(?!`)", r"`\1`", line)
    parts = line.split(" · ")
    if len(parts) > 1 and "`/loop " in parts[0]:
        expanded = [parts[0]]
        for part in parts[1:]:
            stripped = part.strip()
            suffix = "." if stripped.endswith(".") else ""
            word = stripped.rstrip(".")
            expanded.append(f"`/loop {word}`{suffix}" if re.fullmatch(r"[a-z-]+", word) else part)
        return " · ".join(expanded)
    return line


def _rich_loop_set(lines: list[str], match, note: str) -> str:
    """``## Loop set`` from a loop-create reply: cadence rows, notes, controls."""
    rows = [("Cadence", match.group("cadence")), ("Prompt", match.group("prompt").strip())]
    paragraphs: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("(") and line.endswith(")"):
            sentence = line[1:-1].strip()
            sentence = sentence[:1].upper() + sentence[1:]
            if not sentence.endswith("."):
                sentence += "."
            paragraphs.append(sentence)
            continue
        if line.startswith("Self-paced: "):
            rows.append(("Pacing", "self-paced — " + line[len("Self-paced: "):]))
            continue
        if line.startswith("Runs ") and line.endswith(", then stops."):
            rows.append(("Runs", line[len("Runs "):].rstrip(".")))
            continue
        if line.startswith("Stops when: "):
            rows.append(("Stops when", line[len("Stops when: "):]))
            continue
        if line.startswith("Backstop budget: "):
            rows.append(("Backstop budget", line[len("Backstop budget: "):]))
            continue
        if line.startswith("First wakeup "):
            first, sep, controls = line.partition(". Controls: ")
            rows.append(("First wakeup", first[len("First wakeup "):]))
            if sep:
                paragraphs.append("Controls: " + _code_loop_commands(controls))
            continue
        paragraphs.append(line)
    blocks = [_labelled_rows(rows), *paragraphs]
    if note:
        blocks.append(note)
    return _rich_lines("Loop set", *blocks)


def _rich_loop_help(lines: list[str], note: str) -> str:
    """``## Loop usage`` from LOOP_HELP: usage line, examples, controls."""
    blocks = [lines[0].strip()]
    examples: list[str] = []
    paragraphs: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("/loop "):
            command, sep, desc = line.partition(" — ")
            item = f"- `{command.strip()}`" + (f" — {desc}" if sep else "")
            examples.append(item)
        elif line.startswith("Controls:"):
            paragraphs.append("Controls: " + _code_loop_commands(line[len("Controls:"):].strip()))
        else:
            paragraphs.append(line)
    if examples:
        blocks.append("\n".join(examples))
    blocks.extend(paragraphs)
    if note:
        blocks.append(note)
    return _rich_lines("Loop usage", *blocks)


def _rich_loop_text(text: str) -> str:
    """Reformat one /loop reply as card lines (card text only)."""
    text = (text or "").strip()
    if not text:
        return "## Loop"
    note = ""
    if "\nNote: " in text:
        text, note = text.split("\nNote: ", 1)
        note = "Note: " + note.strip()
    lines = text.split("\n")
    head = _strip_leading_icon(lines[0])
    match = re.match(r"Loop set \((?P<cadence>[^)]*)\): (?P<prompt>.*)$", head)
    if match:
        return _rich_loop_set(lines, match, note)
    match = re.match(r"Loop \((?P<meta>[^)]*)\): (?P<prompt>[\s\S]*)$", head)
    if match:
        status, rows, reason = _parse_status_meta(match.group("meta"), interval_label="Cadence")
        out = ([("Status", status)] if status else []) + [
            ("Prompt", match.group("prompt").strip())
        ] + rows
        if reason:
            out.append(("Reason", reason))
        blocks = [_labelled_rows(out)]
        if note:
            blocks.append(note)
        return _rich_lines("Status", *blocks)
    match = re.match(r"Loop paused: (?P<prompt>.*)$", head)
    if match:
        blocks = [_labelled_rows([("Prompt", match.group("prompt"))])]
        blocks.extend(_code_loop_commands(ln) for ln in lines[1:] if ln.strip())
        if note:
            blocks.append(note)
        return _rich_lines("Loop paused", *blocks)
    match = re.match(r"Loop resumed \((?P<cadence>[^)]*)\): (?P<prompt>.*)$", head)
    if match:
        rows = [("Cadence", match.group("cadence")), ("Prompt", match.group("prompt"))]
        blocks = [_labelled_rows(rows)]
        if note:
            blocks.append(note)
        return _rich_lines("Loop resumed", *blocks)
    if head == "Loop stopped.":
        return "## Loop stopped"
    if head.startswith("Usage:"):
        return _rich_loop_help(lines, note)
    blocks = [text]
    if note:
        blocks.append(note)
    return _rich_lines("Loop", *blocks)


def _rich_refine_text(text: str) -> str:
    """Reformat one /refine reply as card lines (card text only)."""
    text = (text or "").strip()
    if not text:
        return "## Refine"
    match = re.match(
        r"^.*?Reviewing this conversation in the background"
        r"(?: \(focus: (?P<focus>.+?)\))? — (?P<rest>[\s\S]*)$",
        text,
    )
    if match:
        blocks = [
            f"Reviewing this conversation in the background — {match.group('rest').strip()}"
        ]
        if match.group("focus"):
            blocks.append(_labelled_rows([("Focus", match.group("focus"))]))
        return _rich_lines("Refine started", *blocks)
    return _rich_lines("Refine", text)


def _rich_review_text(text: str) -> str:
    """Reformat one /review reply as card lines (card text only)."""
    text = (text or "").strip()
    if not text:
        return "## Review"
    if text.startswith("Review started."):
        return _rich_lines("Review started", text)
    match = re.match(
        r"^Review completed synchronously(?: on (?P<model>\S+))?"
        r"(?: \(focus: (?P<focus>.+?)\))? — results:\n(?P<results>[\s\S]*)$",
        text,
    )
    if match:
        rows = []
        if match.group("model"):
            rows.append(("Model", f"`{match.group('model')}`"))
        if match.group("focus"):
            rows.append(("Focus", match.group("focus")))
        blocks = []
        if rows:
            blocks.append(_labelled_rows(rows))
        blocks.append("## Results\n\n```\n" + match.group("results").strip() + "\n```")
        return _rich_lines("Review completed", *blocks)
    return _rich_lines("Review", text)


def _quiet_bool(fn) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def _mgr_call(prefix: str, fn, *args, errors=(RuntimeError, ValueError)):
    """``(result, None)`` from ``fn(*args)``, or ``(None, "<prefix>: <exc>")`` on a manager error."""
    try:
        return fn(*args), None
    except errors as exc:
        return None, f"{prefix}: {exc}"


class GatewayGoalCommandsMixin:
    """Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review."""

    async def _handle_goal_command(self, event: MessageEvent) -> str | None:

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Goal", text=_rich_goal_text(text)):
                return None
            return text
        from hermes_cli.goal_command import dispatch_goal_command
        from hermes_cli.goals import last_user_message_from_db

        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return await _reply(t("gateway.goal.unavailable"))

        def authorize_gate():
            # A local session's owner is its admin: slash.exec admitted this command only after
            # authorize_local_source and the session:control capability (T7). Messaging stays
            # explicit-admin-only.
            from gateway.session_local import authorize_local_source
            if event.source is not None and authorize_local_source(self, event.source) is True:
                return None
            if not self._resume_caller_is_admin(event.source):
                return t("gateway.goal.gate_add_admin_only")
            return None

        def dispatch():
            return dispatch_goal_command(
                mgr, event.get_command_args() or "", authorize_gate=authorize_gate,
                render=lambda key, default, **values: t(key, **values),
                last_user_message=last_user_message_from_db(getattr(mgr, "session_id", None)),
            )

        # Drafting resolves profile-scoped credentials. Keep ContextVars across the
        # executor hop; manager I/O must also stay off the messaging event loop.
        result = await self._run_in_executor_with_context(dispatch)
        if result.clear_pending:
            self._clear_goal_continuations(event, result.clear_pending)
        if result.prompt:
            self._enqueue_goal_turn(event, result.prompt, label="command enqueue", kickoff=result.kickoff)
        return await _reply(result.output)

    def _clear_goal_continuations(self, event: MessageEvent, verb: str) -> None:
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if adapter and quick_key:
                self._clear_goal_pending_continuations(quick_key, adapter)
        except Exception as exc:
            logger.debug("goal %s: pending continuation cleanup failed: %s", verb, exc)

    def _enqueue_goal_turn(
        self, event: MessageEvent, text: str, *, label: str, kickoff: bool
    ) -> None:
        """Enqueue *text* as the next turn through the adapter FIFO (the post-turn judge's path).

        A kickoff keeps the triggering message id / channel prompt / bound skills; a resume
        continuation carries none. Best-effort: failures only logged. A local ``slash.exec`` returns
        the turn to the client as its ``send`` directive instead (the client submits it with its own
        input id).
        """
        from gateway.session_command_sink import active_sink
        if (sink := active_sink()) is not None:
            sink.prompt = text
            if not kickoff:
                sink.display = "/goal resume"
            return
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if text and adapter and quick_key:
                turn = MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id if kickoff else None,
                    channel_prompt=event.channel_prompt if kickoff else None,
                    auto_skill=event.auto_skill if kickoff else None,
                    inherit_channel_pin=not kickoff,
                )
                self._enqueue_fifo(quick_key, turn, adapter)
        except Exception as exc:
            logger.debug("goal %s failed: %s", label, exc)

    async def _handle_heartbeat_command(self, event: MessageEvent) -> str | None:
        """Handle /heartbeat (mirror of the CLI handler): the session's one recurring re-entry
        prompt. The gateway-wide poller injects due heartbeats through the adapter FIFO as
        ordinary user turns, so alternation and caching hold."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Heartbeat", text=_rich_heartbeat_text(text)):
                return None
            return text
        from hermes_cli.heartbeat import parse_interval, format_interval, MIN_INTERVAL_SECONDS
        args = (event.get_command_args() or "").strip()
        lower = args.lower()
        mgr, _session_entry = await self._get_heartbeat_manager_for_event(event)
        if mgr is None:
            return await _reply(t("gateway.heartbeat.unavailable"))
        quick_key = self._session_key_for_source(event.source) if event.source else None

        def _watch():
            if quick_key and event.source is not None:
                self._register_heartbeat_watch(quick_key, event.source, mgr.session_id)

        if not args or lower == "status":
            return await _reply(mgr.status_line())
        if lower == "pause":
            state = mgr.pause()
            return await _reply(t("gateway.heartbeat.paused", prompt=state.prompt) if state else t("gateway.heartbeat.none_set"))
        if lower == "resume":
            state = mgr.resume()
            if state is None:
                return await _reply(t("gateway.heartbeat.no_resume"))
            _watch()
            return await _reply(t("gateway.heartbeat.resumed", interval=format_interval(state.interval_seconds), prompt=state.prompt))
        if lower in {"clear", "stop", "off"}:
            had = mgr.clear()
            if quick_key:
                self._unregister_heartbeat_watch(quick_key)
            return await _reply(t("gateway.heartbeat.cleared") if had else t("gateway.heartbeat.none_set"))

        # Set: `/heartbeat every 10m <prompt>` (also accepts `10m <prompt>`).
        tokens = args.split(None, 2)
        interval, prompt = None, ""
        if tokens[0].lower() == "every" and len(tokens) >= 2:
            interval = parse_interval(f"every {tokens[1]}")
            prompt = tokens[2] if len(tokens) > 2 else ""
        else:
            interval = parse_interval(tokens[0])
            prompt = args[len(tokens[0]):].strip() if interval and interval > 0 else ""
        if interval is None:
            return await _reply(t("gateway.heartbeat.usage"))
        if interval < 0:
            return await _reply(t("gateway.heartbeat.interval_too_small", min_seconds=MIN_INTERVAL_SECONDS))
        if not prompt.strip():
            return await _reply(t("gateway.heartbeat.prompt_required"))
        state, err = _mgr_call(t("gateway.heartbeat.invalid_prefix"), mgr.set, prompt, interval, errors=(ValueError,))
        if err:
            return await _reply(err)
        _watch()
        return await _reply(t("gateway.heartbeat.set", interval=format_interval(state.interval_seconds), prompt=state.prompt))

    def _idle_cached_agent_or_error(self, event: MessageEvent, verb: str):
        """``(session_key, cached_agent, None)`` for /refine and /review, or ``(_, _, error_text)``:
        both need a cached agent from a completed turn and refuse while a run is in flight.
        ``verb`` is the command name (``refine`` / ``review``) and selects ``gateway.<verb>.*`` copy."""
        quick_key = self._session_key_for_source(event.source) if event.source else None
        if not quick_key:
            return None, None, t(f"gateway.{verb}.unavailable")
        if quick_key in self._running_agents:
            return quick_key, None, t("gateway.shared.agent_running_retry_later", command=verb)
        agent = self._cached_agent_for(quick_key)
        if agent is None:
            return quick_key, None, t(f"gateway.{verb}.nothing_yet")
        return quick_key, agent, None

    async def _handle_refine_command(self, event: MessageEvent) -> str | None:
        """Handle /refine — run the memory/skill review fork on demand, in a daemon thread against a
        snapshot of the cached AIAgent's conversation (live session and prompt cache untouched)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Refine", text=_rich_refine_text(text)):
                return None
            return text
        args = (event.get_command_args() or "").strip()
        quick_key, agent, error = self._idle_cached_agent_or_error(event, "refine")
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        session_id = None
        if not snapshot and quick_key and quick_key not in self._running_agents:
            # A local session's conversation is its stored transcript, not the cached agent: /moa
            # (and its settle) drop that agent, so /refine reads the session itself (T7).
            from gateway.session_refine import refine_local_session
            local = await refine_local_session(self, event.source, args)
            if local is not None:
                return await _reply(local)
            # A released cached agent (idle TTL, pressure, /moa), or one holding no messages, is not
            # an empty conversation: refine the stored transcript.
            from gateway.session_cold_review import stored_transcript
            from gateway.session_transcript import TranscriptReadError
            from gateway.slash_commands_status import history_unreadable
            try:
                session_id, snapshot = await stored_transcript(self, quick_key)
            except TranscriptReadError:
                return await _reply(history_unreadable())
            error = None if snapshot else error
        if error:
            return await _reply(error)
        if not snapshot:
            return await _reply(t("gateway.refine.empty"))
        try:
            if agent is None:
                from gateway.session_cold_review import start_cold_refine
                await start_cold_refine(self, event.source, quick_key, session_id, snapshot, args)
            else:
                agent._spawn_background_review(
                    messages_snapshot=snapshot, review_memory=True,
                    review_skills="skill_manage" in getattr(agent, "valid_tool_names", set()), focus=args or None,
                    explicit=True,
                )
        except Exception as exc:
            return await _reply(t("gateway.refine.start_failed", error=exc))
        tail = t("gateway.refine.focus_suffix", focus=args) if args else ""
        return await _reply(t("gateway.refine.started", focus_suffix=tail))

    async def _handle_review_command(self, event: MessageEvent) -> str | None:
        """Handle /review — spawn an independent reviewer subagent. The approval session-key
        contextvar is only bound during agent turns, so bind it explicitly here or the completion
        event carries no gateway route and never re-enters this chat."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Review", text=_rich_review_text(text)):
                return None
            return text
        args = (event.get_command_args() or "").strip()
        quick_key, agent, error = self._idle_cached_agent_or_error(event, "review")
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        cold_session_id = None
        if agent is None and quick_key and quick_key not in self._running_agents:
            # The cached agent was evicted (idle TTL, pressure, /moa): review the stored transcript.
            from gateway.session_cold_review import cold_review_history
            from gateway.session_transcript import TranscriptReadError
            from gateway.slash_commands_status import history_unreadable
            try:
                cold_session_id, snapshot = await cold_review_history(self, event.source)
            except TranscriptReadError:
                return await _reply(history_unreadable())
            error = None
        if error:
            return await _reply(error)
        from tools.approval_context import reset_current_session_key, set_current_session_key

        def _dispatch():
            from agent.review_engine import start_review
            from gateway.session_cold_review import cold_review_parent
            token = set_current_session_key(quick_key)
            try:
                if cold_session_id is None:
                    return start_review(agent, snapshot, args)
                with cold_review_parent(self, event.source, quick_key, cold_session_id) as parent:
                    return start_review(parent, snapshot, args)
            finally:
                reset_current_session_key(token)

        try:
            # _run_in_executor_with_context, not a bare hop: the reviewer subagent is spawned from
            # the worker and inherits its context; a bare hop would run it under the launch home.
            result = await self._run_in_executor_with_context(_dispatch)
        except ValueError as exc:
            return await _reply(str(exc))
        except Exception as exc:
            return await _reply(t("gateway.review.start_failed", error=exc))
        from agent.review_engine import format_dispatch_note
        return await _reply(format_dispatch_note(result, args))

    async def _handle_subgoal_command(self, event: MessageEvent) -> str | None:
        """Handle /subgoal (mirror of the CLI handler): extra criteria appended to the active goal
        mid-loop. They modify state read at the next turn boundary, so this is safe while the
        agent is running."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Subgoal", text=_rich_subgoal_text(text)):
                return None
            return text
        args = (event.get_command_args() or "").strip()
        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return await _reply(t("gateway.goal.unavailable"))
        if not mgr.has_goal():
            return await _reply(t("gateway.subgoal.no_goal"))
        if not args:
            return await _reply(f"{mgr.status_line()}\n{mgr.render_subgoals()}")
        tokens = args.split(None, 1)
        verb = tokens[0].lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""
        if verb == "remove":
            if not rest:
                return await _reply(t("gateway.subgoal.usage_remove"))
            try:
                idx = int(rest.split()[0])
            except ValueError:
                return await _reply(t("gateway.subgoal.remove_not_int"))
            removed, err = _mgr_call(
                "/subgoal remove", mgr.remove_subgoal, idx, errors=(IndexError, RuntimeError)
            )
            return await _reply(err or t("gateway.subgoal.removed", index=idx, text=removed))
        if verb == "clear":
            prev, err = _mgr_call("/subgoal clear", mgr.clear_subgoals, errors=(RuntimeError,))
            if err:
                return await _reply(err)
            if not prev:
                return await _reply(t("gateway.subgoal.none_to_clear"))
            return await _reply(t("gateway.subgoal.cleared_one" if prev == 1 else "gateway.subgoal.cleared_other", count=prev))
        text, err = _mgr_call("/subgoal", mgr.add_subgoal, args)
        if err:
            return await _reply(err)
        idx = len(mgr.state.subgoals) if mgr.state else 0
        return await _reply(t("gateway.subgoal.added", index=idx, text=text))

    async def _handle_loop_command(self, event: MessageEvent) -> str | None:
        """Handle /loop — recurring in-session wakeups, via ``dispatch_loop_command`` (CLI mirror)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Loop", text=_rich_loop_text(text)):
                return None
            return text
        try:
            from hermes_cli.loops import LoopManager, dispatch_loop_command, goal_blocks_loop_tick
        except Exception as exc:
            logger.debug("loops module unavailable: %s", exc)
            return await _reply(t("gateway.loop.unavailable"))

        # Warm the SessionDB cache off-loop: a cold cache drops the first /loop write while the
        # reply claims the loop was set (same class as the /goal false-ack fix).
        await self._warm_goals_session_db("loop manager")
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception:
            session_entry = None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return await _reply(t("gateway.loop.no_session"))
        mgr = LoopManager(session_id=sid)

        # New loops capture the event's routing so the idle loop-wakeup watcher can inject ticks
        # here after a restart; best-effort, empty fields dropped. ``profile`` pins the wakeup to this
        # session's own bot under multiplex (the watcher must never fire it through the default bot).
        route: dict = {}
        try:
            src = event.source
            if src is not None:
                platform = getattr(src, "platform", "")
                route = {"platform": platform.value if hasattr(platform, "value") else str(platform or "")}
                for key in ("chat_id", "chat_type", "thread_id", "user_id", "user_name", "profile"):
                    route[key] = str(getattr(src, key, "") or "")
                route = {k: v for k, v in route.items() if v}
        except Exception:
            route = {}
        result = dispatch_loop_command(mgr, (event.get_command_args() or "").strip(), route=route)
        output = result.get("output") or ""
        if result.get("created") and _quiet_bool(lambda: goal_blocks_loop_tick(mgr.session_id)):
            output += t("gateway.loop.goal_defers_note")
        return await _reply(output)
