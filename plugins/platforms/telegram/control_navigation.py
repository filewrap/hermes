"""Semantic, non-secret history shared by controls on one Telegram message."""
from __future__ import annotations


def setup_frame(state: dict) -> dict:
    return {"kind": "setup", "section": state["section"], "page": state.get("page", 1),
            "inventory_node": state.get("inventory_node", 0)}


class ControlNavigation:
    """Store destinations, never rendered settings, credentials or action callbacks."""

    def __init__(self, limit: int = 32):
        self.frames: list[dict] = []
        self.limit = limit

    def push(self, frame: dict) -> None:
        if not self.frames or self.frames[-1] != frame:
            self.frames.append(dict(frame))
            del self.frames[:-self.limit]

    def pop(self) -> dict | None:
        return self.frames.pop() if self.frames else None

    def __bool__(self) -> bool:
        return bool(self.frames)
