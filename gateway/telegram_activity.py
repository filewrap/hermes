"""Safe presentation helpers for temporary per-user Telegram turn activity."""
from __future__ import annotations

import html
import re
from typing import Any

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DISPLAY_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,63}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_TOOL_STATUS_LINES = {
    "running": "⏳ {tool} — running",
    "succeeded": "✓ {tool} — finished",
    "failed": "⚠ {tool} — failed",
}
_TURN_STATUS_LINES = {
    "turn-complete": "✓ Turn finished",
    "turn-failed": "⚠ Turn failed before completion",
    "turn-interrupted": "⏹ Turn stopped before completion",
}


def safe_tool_label(value: Any) -> str:
    """Return a short tool-name label; arguments and previews are never accepted."""
    name = str(value or "").strip()
    if _TOOL_NAME_RE.fullmatch(name):
        return name.replace("_", " ").replace(".", " ").replace("-", " ").title()[:48]
    if not _DISPLAY_LABEL_RE.fullmatch(name):
        return "Tool"
    return " ".join(name.split()).title()[:48]


def _safe_basename(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        return None
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {"", ".", ".."}:
        return None
    return name[:120]


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if 1 <= number <= 1_000_000 else default


def _safe_tool_detail(tool: str, args: Any) -> str:
    """Select only a filename and numeric line facts; never return arbitrary arguments."""
    if not isinstance(args, dict):
        return ""
    filename = _safe_basename(args.get("path"))
    if not filename:
        return ""

    if tool == "read_file":
        start = _positive_int(args.get("offset", 1), 1)
        limit = _positive_int(args.get("limit", 2000), 2000)
        return f"{filename} · lines {start}–{start + limit - 1}"
    if tool == "write_file":
        content = args.get("content")
        if isinstance(content, str):
            return f"{filename} · {len(content.splitlines())} lines"
        return filename
    if tool == "patch":
        mode = str(args.get("mode") or "replace").strip().lower()
        old, new = args.get("old_string"), args.get("new_string")
        if mode == "replace" and isinstance(old, str) and isinstance(new, str):
            changed_lines = len(old.splitlines()) + len(new.splitlines())
            return f"{filename} · {changed_lines} changed lines"
        return filename
    return ""


def tool_activity_event(
    call_id: Any, tool_name: Any, status: str, args: Any = None,
) -> dict[str, str] | None:
    """Build a display-only lifecycle event with only explicitly safe file metadata."""
    if status not in _TOOL_STATUS_LINES:
        return None
    tool = safe_tool_label(tool_name)
    raw_id = str(call_id or "")
    safe_id = raw_id if _CALL_ID_RE.fullmatch(raw_id) else "name:" + tool
    event = {
        "kind": "telegram_activity",
        "event": "tool",
        "call_id": safe_id,
        "tool": tool,
        "status": status,
    }
    if status == "running":
        detail = _safe_tool_detail(str(tool_name or ""), args)
        if detail:
            event["detail"] = detail
    return event


def tool_activity_message(event: Any) -> str:
    """Render one safe standalone activity message as a native RichMessage HTML body."""
    if not isinstance(event, dict) or event.get("kind") != "telegram_activity" or event.get("event") != "tool":
        return ""
    if event.get("status") != "running":
        return ""

    tool = safe_tool_label(event.get("tool"))
    detail = str(event.get("detail") or "")
    if tool == "Terminal":
        label = "Running command"
    elif tool == "Read File" and detail:
        filename, _, lines = detail.partition(" · ")
        label = f"Reading <code>{html.escape(filename)}</code>"
        if lines:
            label += f" ({html.escape(lines)})"
    elif tool in {"Write File", "Patch"} and detail:
        filename, separator, lines = detail.partition(" · ")
        label = f"Editing <code>{html.escape(filename)}</code>"
        if separator and lines:
            label += f" ({html.escape(lines)})"
    else:
        label = f"Running {html.escape(tool)}"
    return f"<p>⏳ {label}</p>"


def turn_activity_event(status: str) -> dict[str, str] | None:
    """Build a terminal turn marker without exposing exception or result text."""
    if status not in _TURN_STATUS_LINES:
        return None
    return {"kind": "telegram_activity", "event": "turn", "status": status}
