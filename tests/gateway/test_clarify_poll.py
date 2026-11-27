"""Telegram clarify polls: the question goes out as a native poll, only the user
the question was asked to may answer it, and the first vote or typed reply TO
THE POLL stops the poll.

Runner side (this file): the turn's triggering user is recorded on the clarify
entry as ``respondent_id``. Adapter side (``tests/plugins/platforms/telegram/
test_clarify_poll.py``): poll rendering, vote handling, retire.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tools import clarify_gateway as cm


_ONE_QUESTION = [{"qid": "q0", "question": "Pick one", "choices": ["a", "b"], "multi_select": False}]


class _CardAdapter:
    def __init__(self):
        self.asked: list[str] = []

    def pause_typing_for_chat(self, chat_id):
        return None

    def resume_typing_for_chat(self, chat_id):
        return None

    async def send_clarify(self, **kwargs):
        self.asked.append(kwargs["question"])
        return SendResult(success=True, message_id="1.2")


def _clear_clarify_state():
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
        getattr(cm, "_polls", {}).clear()
        getattr(cm, "_poll_index", {}).clear()


def _run_one_question(source):
    """Drive one clarify question through the real runner callback; returns the
    registered entry."""
    from gateway.run_turn_runner import TurnRunner
    _clear_clarify_state()
    adapter = _CardAdapter()
    runner = object.__new__(TurnRunner)
    runner._approval_owner = None
    runner._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id="C1", _status_thread_metadata={},
        session_key="sk1", stream_consumer_holder=[None], source=source)

    class _Fut:
        def __init__(self, r):
            self._r = r

        def result(self, timeout=None):
            return self._r

    runner._schedule = lambda coro, label: _Fut(asyncio.run(coro))
    runner._close_native_stream_boundary = lambda *a, **k: None
    real_register = cm.register
    seen = {}

    def _register(**kwargs):
        entry = real_register(**kwargs)
        seen["entry"] = entry
        cm.resolve_gateway_clarify(kwargs["clarify_id"], "a")
        return entry

    with patch.object(cm, "register", _register), \
            patch("tools.clarify_gateway.get_clarify_timeout", return_value=0.05):
        runner._clarify_callback_sync(_ONE_QUESTION)
    return seen["entry"]


def test_runner_records_triggering_user_as_respondent():
    """The clarify entry carries the turn's triggering user id, so poll delivery
    can enforce that only that user answers."""
    source = SimpleNamespace(platform=Platform.TELEGRAM, user_id="6802929470")
    entry = _run_one_question(source)
    assert entry.respondent_id == "6802929470"


def test_runner_records_no_respondent_without_a_source_user():
    """A turn with no source identity (classic-CLI batch, local viewer) registers
    no respondent — poll delivery then enforces nothing, as before."""
    source = SimpleNamespace(platform=Platform.TELEGRAM)
    entry = _run_one_question(source)
    assert entry.respondent_id is None


# --- _hm_clarify_reply respondent gate (poll-delivered clarifies) -----------

SESSION_KEY = "agent:main:telegram:group:-1000000000042:100"


class _RetireAdapter:
    """Records retire_clarify_card calls like a persistent native card."""

    def __init__(self):
        self.retired: list[tuple[str, str]] = []

    def resume_typing_for_chat(self, chat_id):
        return None

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        self.retired.append((clarify_id, notice))


def _reply_event(text, user_id, reply_to=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="-1000000000042", chat_type="group",
            user_id=user_id, thread_id="77"),
        message_id="msg1",
        reply_to_message_id=reply_to)


def _make_inbound_runner(adapter):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: SESSION_KEY
    runner._delivery_adapter_for = lambda source: adapter
    runner._update_prompt_pending = {}
    return runner


def _register_poll_clarify(clarify_id="pc1", *, respondent="100", poll=True):
    _clear_clarify_state()
    entry = cm.register(clarify_id, SESSION_KEY, "Pick one", ["A", "B"],
                        respondent_id=respondent)
    cm.mark_awaiting_text(clarify_id)
    if poll:
        cm.register_clarify_poll(clarify_id, poll_id=4242, message_id=99,
                                 chat_id="-1000000000042", thread_id="77")
    return entry


@pytest.mark.asyncio
async def test_poll_reply_from_respondent_resolves_and_retires():
    entry = _register_poll_clarify()
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    event = _reply_event("2", "100", reply_to="99")  # replying to the poll message
    result = await runner._hm_clarify_reply(event, event.source, SESSION_KEY)
    assert result == ""
    assert entry.response == "B"
    assert [clarify_id for clarify_id, _ in adapter.retired] == ["pc1"]
    assert "B" in adapter.retired[0][1]


@pytest.mark.asyncio
async def test_poll_reply_from_other_user_falls_through():
    entry = _register_poll_clarify()
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    # Even a reply TO the poll from another member is not the respondent's answer.
    event = _reply_event("2", "200", reply_to="99")
    result = await runner._hm_clarify_reply(event, event.source, SESSION_KEY)
    assert result is None
    assert entry.response is None
    assert not entry.event.is_set()
    assert adapter.retired == []
    assert cm.get_clarify_poll("pc1") is not None  # the poll stays open


@pytest.mark.asyncio
async def test_poll_reply_from_respondent_without_reply_to_falls_through():
    """A bare message from the intended user is their own message, not the
    answer to a poll-delivered clarify — only a reply to the poll counts."""
    entry = _register_poll_clarify()
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    event = _reply_event("2", "100")
    result = await runner._hm_clarify_reply(event, event.source, SESSION_KEY)
    assert result is None
    assert entry.response is None
    assert not entry.event.is_set()
    assert adapter.retired == []
    assert cm.get_clarify_poll("pc1") is not None  # the poll stays open


@pytest.mark.asyncio
async def test_poll_skip_from_respondent_skips_and_retires():
    entry = _register_poll_clarify()
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    event = _reply_event("skip", "100", reply_to="99")  # replying to the poll message
    result = await runner._hm_clarify_reply(event, event.source, SESSION_KEY)
    assert result == ""
    assert entry.response == cm.SKIPPED
    assert [clarify_id for clarify_id, _ in adapter.retired] == ["pc1"]


@pytest.mark.asyncio
async def test_poll_skip_from_other_user_falls_through():
    entry = _register_poll_clarify()
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    event = _reply_event("skip", "200", reply_to="99")
    result = await runner._hm_clarify_reply(event, event.source, SESSION_KEY)
    assert result is None
    assert entry.response is None
    assert adapter.retired == []


@pytest.mark.asyncio
async def test_text_card_reply_from_other_user_still_resolves():
    """The respondent gate applies to poll-delivered clarifies only; a text
    card keeps today's behaviour — any message in the session answers it."""
    entry = _register_poll_clarify(poll=False)
    adapter = _RetireAdapter()
    runner = _make_inbound_runner(adapter)
    result = await runner._hm_clarify_reply(_reply_event("2", "200"), _reply_event("2", "200").source, SESSION_KEY)
    assert result == ""
    assert entry.response == "B"
