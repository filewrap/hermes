"""``/busy`` reply rendering, lifted out of ``gateway/slash_commands.py``.

The mode tables and the card reformat live here so the slash-commands
facade stays under its line cap; behaviour is unchanged.
"""
from __future__ import annotations

import re
from typing import Optional

from agent.i18n import t

# /busy input modes -> (status-card behavior key, set-confirmation behavior key); text via t().
_BUSY_MODE_BEHAVIOR = {
    "queue": ("gateway.busy.behavior_queue_short", "gateway.busy.behavior_queue_long"),
    "steer": ("gateway.busy.behavior_steer_short", "gateway.busy.behavior_steer_long"),
    "interrupt": ("gateway.busy.behavior_interrupt_short", "gateway.busy.behavior_interrupt_long"),
}

# /busy change commands as card copy pills (label, copied text).
_BUSY_CHANGE_COPIES = (
    ("/busy queue", "/busy queue"),
    ("/busy steer", "/busy steer"),
    ("/busy interrupt", "/busy interrupt"),
)


def _busy_mode_rows(current: str) -> list[str]:
    """Mode list rows — current mode first and marked, the rest in canonical order."""
    if current in _BUSY_MODE_BEHAVIOR:
        names = [current] + [name for name in _BUSY_MODE_BEHAVIOR if name != current]
    else:
        names = list(_BUSY_MODE_BEHAVIOR)
    rows = []
    for name in names:
        behavior = t(_BUSY_MODE_BEHAVIOR[name][0])
        marker = " ✓" if name == current else ""
        rows.append(f"- `{name}{marker}` — {behavior}")
    return rows


def _rich_busy_text(text: str) -> str:
    """Reformat one /busy reply as card lines (card text only).

    The status reply becomes a headed mode list — current mode first, marked
    with a check — followed by the change hint; the set reply becomes a
    confirmation head plus the new mode and its behavior. Anything else gets
    a ``## Busy input mode`` head over the original lines.
    """
    text = (text or "").strip()
    if not text:
        return "## Busy input mode"
    match = re.match(r"\*\*Busy input mode: `(?P<mode>\w+)`", text)
    if match:
        lines = text.split("\n")
        hint = lines[2].strip() if len(lines) > 2 else ""
        card = ["## Busy input mode", "", *_busy_mode_rows(match.group("mode"))]
        if hint:
            card += ["", hint]
        return "\n".join(card)
    match = re.match(
        r"Busy input mode set to \*\*`(?P<mode>\w+)`\*\* \(saved\)\.\n_(?P<behavior>.*)_$",
        text, re.DOTALL)
    if match:
        behavior = match.group("behavior")
        behavior = behavior[:1].upper() + behavior[1:] if behavior else behavior
        return "\n".join([
            "## Busy input mode set",
            "",
            f"- **Mode:** `{match.group('mode')}` ✓",
            f"- {behavior}",
        ])
    return "## Busy input mode\n\n" + text


# Session-key slot parsing, lifted out of gateway/run_busy.py (same behaviour).
def _strip_slot(text: str, slot: str) -> Optional[str]:
    """Remainder after ``slot`` when ``text`` starts with it as a WHOLE slot, else None.

    The ``:``-delimited slot layout is ``build_session_key``'s: an id that merely starts with another
    must never match, so a whole-slot comparison is what every caller here uses. A text that IS the
    slot yields ``""``."""
    if text == slot:
        return ""
    if text.startswith(slot + ":"):
        return text[len(slot) + 1:]
    return None


def _tail_has_slot(tail: str, slot: str) -> bool:
    """True when ``tail``'s FIRST slot is ``slot`` (``tail`` is ``""`` when the key ends at the
    chat id)."""
    return _strip_slot(tail, slot) is not None
