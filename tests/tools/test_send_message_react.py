"""Tests for send_message action='react'/'unreact' dispatch.

Kept separate from ``test_send_message_tool.py`` because that module skips
wholesale when optional Telegram dependencies are not installed.
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.send_message_tool as smt


class _FakePhotonAdapter:
    """Adapter exposing add_reaction/remove_reaction coroutines."""

    def __init__(self):
        self.calls = []

    async def add_reaction(self, chat_id, emoji, message_id=None):
        self.calls.append(("add", chat_id, emoji, message_id))
        return {"success": True, "emoji": emoji}

    async def remove_reaction(self, chat_id, message_id=None):
        self.calls.append(("remove", chat_id, message_id))
        return {"success": True}


class _NoReactionAdapter:
    """Adapter with no reaction support at all."""


def _runner_with(adapter):
    from gateway.config import Platform

    return SimpleNamespace(adapters={Platform("telegram"): adapter})


def _call(args):
    return json.loads(smt.send_message_tool(args))


def test_react_dispatches_to_add_reaction():
    adapter = _FakePhotonAdapter()
    with patch("gateway.run._gateway_runner_ref", lambda: _runner_with(adapter)):
        result = _call(
            {"action": "react", "target": "telegram:12345", "emoji": "❤️"}
        )
    assert result["success"] is True
    assert adapter.calls == [("add", "12345", "❤️", None)]


def test_react_without_live_gateway():
    with patch("gateway.run._gateway_runner_ref", lambda: None):
        result = _call(
            {"action": "react", "target": "telegram:12345", "emoji": "👍"}
        )
    assert result.get("success") is not True
    assert "live" in json.dumps(result)


@pytest.mark.asyncio
async def test_react_dispatches_on_gateway_loop_from_model_tool_worker(monkeypatch):
    """Reaction coroutines must share the live adapter's event loop, like sends."""
    from gateway.config import Platform

    gateway_loop = asyncio.new_event_loop()
    started = threading.Event()
    reaction_loops = []

    class LoopBoundAdapter:
        async def add_reaction(self, chat_id, emoji, message_id=None):
            reaction_loops.append(asyncio.get_running_loop())
            return {"success": True, "chat_id": chat_id, "emoji": emoji}

    adapter = LoopBoundAdapter()
    runner = SimpleNamespace(
        adapters={Platform("telegram"): adapter},
        _gateway_loop=gateway_loop,
    )

    def run_gateway():
        asyncio.set_event_loop(gateway_loop)
        started.set()
        gateway_loop.run_forever()

    thread = threading.Thread(target=run_gateway, daemon=True)
    thread.start()
    assert started.wait(timeout=2)
    monkeypatch.setattr(smt, "_live_adapter", lambda _platform: (runner, adapter))
    try:
        result = _call({
            "action": "react", "target": "telegram:12345",
            "emoji": "👍", "message_id": "42",
        })
        assert result["success"] is True
        assert result["chat_id"] == "12345"
        assert reaction_loops == [gateway_loop]
    finally:
        gateway_loop.call_soon_threadsafe(gateway_loop.stop)
        thread.join(timeout=2)
        gateway_loop.close()
