"""Shared rich-card builders for converted Telegram slash commands (Phase 5).

Pure functions only: Markdown-ish handler lines become Telegram rich HTML, and
navigation buttons become regular inline-keyboard buttons whose ``hm:run:/...``
payloads re-dispatch through the adapter's normal inbound path. Values the user
may want to reuse (session id, home path, user id) travel as in-body copy
buttons; links travel as in-body URL buttons, never as bare hyperlinks.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

# Read-only commands a card button may re-dispatch. Argument-free only: the
# payload carries no user text, so a forged callback cannot smuggle a prompt or
# a mutating command past this list.
RUN_COMMAND_ALLOWLIST: frozenset[str] = frozenset({
    "status", "context", "last", "usage", "agents", "insights", "sessions",
    "model", "providers", "models", "mcp", "plugins", "skills", "help",
    "settings", "version", "profile", "whoami", "commands", "topup",
    "deepresearch", "goal", "subgoal", "heartbeat", "loop", "kanban",
    "memory", "bundles", "curator", "suggestions", "blueprint", "queue",
    "steer", "bg", "btw", "egress", "moa", "codex-runtime", "personality",
    "busy", "autoprune", "platform", "rollback", "diff",
})

NAV_LABELS: dict[str, str] = {
    "status": "Status", "context": "Context", "last": "Last", "usage": "Usage",
    "agents": "Agents", "insights": "Insights", "sessions": "Sessions",
    "model": "Model", "providers": "Providers", "models": "Models",
    "mcp": "MCP", "plugins": "Plugins", "skills": "Skills", "help": "Help",
    "settings": "Settings", "version": "Version", "profile": "Profile",
    "whoami": "Whoami", "commands": "Commands", "topup": "Top up",
    "deepresearch": "Deep research", "goal": "Goal", "subgoal": "Subgoals",
    "heartbeat": "Heartbeat", "loop": "Loop", "kanban": "Kanban",
    "memory": "Memory", "bundles": "Bundles", "curator": "Curator",
    "suggestions": "Suggestions", "blueprint": "Blueprint", "queue": "Queue",
    "steer": "Steer", "bg": "Background", "btw": "Ask", "egress": "Egress",
    "moa": "MoA", "codex-runtime": "Codex runtime", "personality": "Personality",
    "busy": "Busy", "autoprune": "Auto-prune", "platform": "Platform",
    "rollback": "Rollback", "diff": "Diff",
}

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`\n]+)`")
# Pictographic emoji (hourglass, warning sign, faces, transport, …) are
# stripped at render time: converted cards carry 0 emojis, even when the
# text comes from a shared engine (goal status lines carry a ⏳). Plain text
# status marks (✓/✗/♥) are not emoji presentation and are kept.
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2B00-\u2BFF\u26A0\u274C\u2705\uFE0F]")


def run_command_payload(command: str) -> str:
    """``hm:run:/<command>`` callback payload for an allowlisted command."""
    name = str(command or "").strip().lstrip("/")
    if name not in RUN_COMMAND_ALLOWLIST:
        raise ValueError("Only read-only commands may run from a card button")
    return f"hm:run:/{name}"


def command_nav_markup(commands: Iterable[str]) -> Any | None:
    """Regular inline keyboard, two buttons per row, for *commands*."""
    from .mtproto_rich import inline_markup

    names = [str(c).strip().lstrip("/") for c in commands]
    rows: list[list[tuple[str, str, None]]] = []
    for i in range(0, len(names), 2):
        row = [(NAV_LABELS[name], run_command_payload(name), None) for name in names[i:i + 2]]
        rows.append(row)
    return inline_markup(rows) if rows else None


def session_menu_buttons(rows: Iterable[dict]) -> list[tuple[str, str]]:
    """``(label, hm:resume:<id>)`` for session-listing rows (one button per row).

    The session id is only a routing handle: tapping it re-dispatches
    ``/resume <id>`` through the normal inbound path, where the IDOR guard in
    the resume handler still decides — a forged callback is no stronger than
    typing the command.
    """
    buttons: list[tuple[str, str]] = []
    for row in rows:
        session_id = str(row.get("id") or "").strip()
        title = " ".join(str(row.get("title") or session_id).split())[:40] or session_id
        if not session_id:
            continue
        buttons.append((title, f"hm:resume:{session_id}"))
    return buttons


def command_card_markup(*, nav: Iterable[str] = (),
                        buttons: Iterable[tuple[str, str]] = ()) -> Any | None:
    """One inline keyboard: explicit one-per-row *buttons* first, then nav rows."""
    from .mtproto_rich import inline_markup

    rows: list[list[tuple[str, str, None]]] = [
        [(label, payload, None)] for label, payload in buttons
    ]
    names = [str(c).strip().lstrip("/") for c in nav]
    for i in range(0, len(names), 2):
        rows.append([(NAV_LABELS[name], run_command_payload(name), None)
                     for name in names[i:i + 2]])
    return inline_markup(rows) if rows else None


def _inline_markup(line: str) -> str:
    """Escape one line, then render ``**bold**`` and ```code``` spans as HTML."""
    safe = html.escape(line, quote=False)
    safe = _CODE_RE.sub(r"<code>\1</code>", safe)
    return _BOLD_RE.sub(r"<strong>\1</strong>", safe)


def lines_to_rich_html(title: str, lines: Iterable[str]) -> str:
    """Render handler lines as one Telegram rich-HTML card.

    ``# ``/``## `` become headings, ``- `` lines group into a list, blank lines
    break paragraphs, and ```` ``` ```` fences become ``<pre>`` blocks.
    Anything else is a paragraph. Pictographic emoji are stripped (converted
    cards carry 0 emojis); no emoji is added here.
    """
    parts: list[str] = []
    if title:
        parts.append(f"<h1>{html.escape(title, quote=False)}</h1>")
    paragraph: list[str] = []
    list_items: list[str] = []
    fence_lines: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            parts.append("<p>" + "<br>".join(_inline_markup(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    def flush_fence() -> None:
        if fence_lines:
            parts.append("<pre>" + "\n".join(html.escape(line, quote=False) for line in fence_lines) + "</pre>")
            fence_lines.clear()

    for raw in lines:
        line = _EMOJI_RE.sub("", str(raw or "")).rstrip()
        if line.strip().startswith("```"):
            if in_fence:
                flush_fence()
                in_fence = False
            else:
                flush_paragraph()
                flush_list()
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("# "):
            flush_paragraph()
            flush_list()
            parts.append(f"<h1>{_inline_markup(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            parts.append(f"<h2>{_inline_markup(line[3:].strip())}</h2>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(_inline_markup(line[2:].strip()))
        else:
            flush_list()
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    flush_fence()
    return "\n".join(parts)


def with_action_buttons(html_text: str, *, links: Iterable[tuple[str, str]] = (),
                        copies: Iterable[tuple[str, str]] = ()) -> str:
    """Append in-body URL / copy button rows to *html_text* (no buttons, no change)."""
    from .mtproto_rich import rich_button, rich_button_row

    rows: list[str] = []
    link_buttons = [rich_button(label, kind="url", value=url) for label, url in links]
    if link_buttons:
        rows.append(rich_button_row(link_buttons))
    copy_buttons = [rich_button(label, kind="copy", value=text) for label, text in copies]
    if copy_buttons:
        rows.append(rich_button_row(copy_buttons))
    if not rows:
        return html_text
    return html_text.rstrip() + "\n" + "\n".join(rows)
