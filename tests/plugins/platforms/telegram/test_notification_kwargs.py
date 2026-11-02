"""Telegram notification-mode gating (#132516): "important" mode silences ordinary sends, but a
human-decision prompt (``is_approval_prompt``) must always push — a silently delivered prompt is
indistinguishable from "no prompt" and costs the full approvals.timeout before the command is refused.

Fork shape: the fork's adapter is the MTProto ``TelethonAdapter`` (no PTB ``TelegramAdapter``),
so "disable_notification" is the ``silent`` flag on the ``SendMessageRequest`` built by ``send()``.
"""

import pytest

from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.mark.parametrize("mode,metadata,expected", [
    ("important", None, {"disable_notification": True}),
    ("important", {"thread_id": "t1"}, {"disable_notification": True}),
    ("important", {"notify": True}, {}),
    ("important", {"thread_id": "t1", "is_approval_prompt": True}, {}),
    ("all", None, {}),
    ("all", {"is_approval_prompt": True}, {}),
])
def test_notification_kwargs(mode, metadata, expected):
    adapter = object.__new__(TelethonAdapter)
    adapter._notifications_mode = mode
    assert adapter._notification_kwargs(metadata) == expected


def test_clarify_text_fallback_pushes_in_important_mode():
    """A failed native clarify card retries as base numbered text via adapter.send(); that
    human-decision prompt must not go out with silent=True. The fork's clarify fallback
    (``BasePlatformAdapter.send_clarify``) routes through ``send()`` with the
    ``is_approval_prompt`` metadata injected by ``text_fallback_coro``, so the assertion is
    on the request ``send()`` builds, not on a PTB ``send_message`` call."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    adapter = object.__new__(TelethonAdapter)
    adapter._notifications_mode = "important"
    adapter._client = MagicMock()
    adapter._telegram_pending_receipts = {}
    adapter._input_peer_for = AsyncMock(return_value=MagicMock())
    adapter._reply_top_target = lambda metadata: None
    adapter._reply_target = lambda reply_to, metadata: None
    adapter._request_with_bounded_retry = AsyncMock(return_value=None)
    adapter._sent_message_id = lambda *args, **kwargs: "42"
    adapter._set_task_delivery_failure = lambda *args, **kwargs: None

    asyncio.run(adapter.send("123", "Which env?", metadata={"is_approval_prompt": True}))
    request = adapter._request_with_bounded_retry.await_args.args[0]
    assert request.silent is None

    asyncio.run(adapter.send("123", "Ordinary update"))
    request = adapter._request_with_bounded_retry.await_args.args[0]
    assert request.silent is True
