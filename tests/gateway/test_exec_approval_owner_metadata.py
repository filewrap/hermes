"""Approval cards must carry the turn's initiator, profile home and owner.

The card is rendered on the gateway loop, outside the turn's profile scope,
so the adapter cannot resolve the session's owner from the ambient
HERMES_HOME. Before this fix the adapter stored an empty owner (and the
prompt metadata never carried a user_id), so the owner themself was denied
at tap time with "Only the owner or the account that initiated this action
can approve it." These tests drive the real ``TurnRunner._approval_notify_sync``
with a recording adapter and a real profile ``.env``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.platforms.base import SendResult


class _RecordingAdapter:
    """Renders native buttons and records the approval metadata it received."""

    typed_command_prefix = "/"

    def __init__(self) -> None:
        self.metadatas: list[dict] = []

    def pause_typing_for_chat(self, chat_id: str) -> None:
        return None

    async def send_exec_approval(self, *args: Any, **kwargs: Any) -> SendResult:
        self.metadatas.append(dict(kwargs.get("metadata") or {}))
        return SendResult(success=True, message_id="card-1")


def _runner(adapter, tmp_path, *, source_user_id="100"):
    from gateway.run_turn_runner import TurnRunner

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="100",
        _status_thread_metadata={"thread_id": "t1"},
        session_key="agent:main:telegram:dm:1",
        source=SimpleNamespace(
            chat_id="100", user_id=source_user_id, platform="telegram",
            session_key="agent:main:telegram:dm:1",
        ),
    )
    runner._runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )

    class _Fut:
        def __init__(self, result):
            self._r = result

        def result(self, timeout=None):
            return self._r

    runner._schedule = lambda coro, _label: _Fut(asyncio.run(coro))
    runner._close_native_stream_boundary = lambda _why: None
    return runner


def _approval_data():
    return {
        "command": "rm -rf /tmp/x", "description": "recursive delete",
        "pattern_key": "k", "request_id": "request-a",
    }


def test_notify_stamps_initiator_owner_and_profile_home(tmp_path):
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    adapter = _RecordingAdapter()
    _runner(adapter, tmp_path)._approval_notify_sync(_approval_data())

    assert len(adapter.metadatas) == 1
    metadata = adapter.metadatas[0]
    assert metadata["user_id"] == "100", "the turn initiator must reach the card"
    assert metadata["owner_user_id"] == "100", "the session's owner must reach the card"
    assert metadata["profile_home"] == str(Path(tmp_path).resolve())


def test_notify_still_arms_card_when_owner_is_unresolvable(tmp_path):
    """No owner configured anywhere: the card still arms with the initiator
    (who can approve), instead of failing the send."""
    adapter = _RecordingAdapter()
    _runner(adapter, tmp_path)._approval_notify_sync(_approval_data())

    assert len(adapter.metadatas) == 1
    metadata = adapter.metadatas[0]
    assert metadata["user_id"] == "100"
    assert "owner_user_id" not in metadata
