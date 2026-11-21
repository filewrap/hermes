"""Tests for the business trigger gate (design §4).

``business_triggered`` is a pure function: direct mode handles every
non-empty text; trigger mode handles only a word-boundary,
case-insensitive word match or a reply to a business-bot-sent message.
The manager-level tests prove the gate sits before the debounce: a
no-match records the message in recent, logs, and starts no turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from telethon.tl.types import BotBusinessConnection, BusinessBotRights, Message, PeerUser

from plugins.platforms.telegram.business_manager import (
    BusinessModeManager,
    business_triggered,
)
from plugins.platforms.telegram.business_state import BusinessStateDB


@pytest.fixture()
def db(tmp_path):
    """Fresh business state DB."""
    sdb = BusinessStateDB(tmp_path / "biz_state.db")
    yield sdb
    sdb.close()


def _tl_connection(*, conn_id="conn1", owner_id=42) -> Any:
    return BotBusinessConnection(
        connection_id=conn_id,
        user_id=owner_id,
        dc_id=1,
        date=None,
        disabled=False,
        rights=BusinessBotRights(reply=True),
    )


def _tl_message(*, msg_id=1, customer_chat_id=200, text="Hi there") -> Any:
    return Message(
        id=msg_id,
        peer_id=PeerUser(customer_chat_id),
        date=None,
        message=text,
        out=False,
        mentioned=False,
        media_unread=False,
        silent=False,
        post=False,
        from_scheduled=False,
        legacy=False,
        edit_hide=False,
        pinned=False,
        noforwards=False,
        invert_media=False,
        offline=False,
        from_id=PeerUser(999),
        via_business_bot_id=None,
    )


class _SentRecorder:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._next_id = 1000

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        sent = SimpleNamespace(message_id=self._next_id, id=self._next_id)
        self._next_id += 1
        return sent


def _make_manager(db, *, trigger_mode="direct", trigger_word=""):
    reply_calls: List[Dict[str, Any]] = []
    sender = _SentRecorder()

    async def _reply(customer_text, customer_chat_id, context):
        reply_calls.append({
            "customer_text": customer_text,
            "customer_chat_id": customer_chat_id,
            "context": context,
        })
        return "Reply text!"

    mgr = BusinessModeManager(
        session_db=db,
        send_message=sender,
        reply_via_turn=_reply,
        send_business_message=_SentRecorder(),
        debounce_seconds=0.0,
        trigger_mode=trigger_mode,
        trigger_word=trigger_word,
    )
    return mgr, sender, reply_calls


class TestBusinessTriggeredPure:
    def test_direct_mode_handles_any_non_empty_text(self):
        assert business_triggered("hello", mode="direct", word="", reply_to_business_bot=False)
        assert business_triggered("  hi  ", mode="direct", word="", reply_to_business_bot=False)

    def test_direct_mode_rejects_empty_text(self):
        assert not business_triggered("", mode="direct", word="", reply_to_business_bot=False)
        assert not business_triggered("   ", mode="direct", word="", reply_to_business_bot=False)
        assert not business_triggered(None, mode="direct", word="", reply_to_business_bot=False)

    def test_trigger_mode_word_match(self):
        assert business_triggered("please help me", mode="trigger", word="help", reply_to_business_bot=False)
        assert business_triggered("HELP!", mode="trigger", word="help", reply_to_business_bot=False)
        assert business_triggered("Help", mode="trigger", word="HELP", reply_to_business_bot=False)

    def test_trigger_mode_word_boundary(self):
        # "help" inside a longer word does not trigger.
        assert not business_triggered("helpful tip", mode="trigger", word="help", reply_to_business_bot=False)
        assert not business_triggered("shelfhelp", mode="trigger", word="help", reply_to_business_bot=False)
        # Word at the edges still matches.
        assert business_triggered("help", mode="trigger", word="help", reply_to_business_bot=False)
        assert business_triggered("can you help?", mode="trigger", word="help", reply_to_business_bot=False)

    def test_trigger_mode_reply_to_business_bot(self):
        assert business_triggered(
            "yes please", mode="trigger", word="help", reply_to_business_bot=True,
        )
        assert not business_triggered(
            "yes please", mode="trigger", word="help", reply_to_business_bot=False,
        )

    def test_trigger_mode_empty_word_needs_reply(self):
        assert not business_triggered("hello", mode="trigger", word="", reply_to_business_bot=False)
        assert business_triggered("hello", mode="trigger", word="", reply_to_business_bot=True)

    def test_unknown_mode_never_triggers(self):
        assert not business_triggered("hello", mode="bogus", word="", reply_to_business_bot=False)
        assert not business_triggered("hello", mode="bogus", word="", reply_to_business_bot=True)


class TestTriggerGateInManager:
    @pytest.mark.asyncio
    async def test_trigger_mode_match_replies(self, db):
        mgr, sender, reply_calls = _make_manager(
            db, trigger_mode="trigger", trigger_word="help",
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.calls.clear()
        await mgr.handle_business_message("conn1", _tl_message(text="please help"))
        assert len(reply_calls) == 1
        assert sender.calls

    @pytest.mark.asyncio
    async def test_trigger_mode_no_match_records_but_no_turn(self, db):
        mgr, sender, reply_calls = _make_manager(
            db, trigger_mode="trigger", trigger_word="help",
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.calls.clear()
        await mgr.handle_business_message("conn1", _tl_message(text="just saying hi"))
        assert reply_calls == []
        assert sender.calls == []
        # ... but the message is still recorded in recent.
        assert [r["text"] for r in db.list_recent_business_messages("42")] == ["just saying hi"]

    @pytest.mark.asyncio
    async def test_trigger_mode_reply_to_bot_message_replies(self, db):
        mgr, sender, reply_calls = _make_manager(
            db, trigger_mode="trigger", trigger_word="help",
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.calls.clear()
        await mgr.handle_business_message(
            "conn1", _tl_message(text="yes please"), reply_to_business_bot=True,
        )
        assert len(reply_calls) == 1
        assert reply_calls[0]["context"]["trigger"] == "reply"
        assert sender.calls

    @pytest.mark.asyncio
    async def test_trigger_mode_word_match_context_names_trigger(self, db):
        mgr, _, reply_calls = _make_manager(
            db, trigger_mode="trigger", trigger_word="help",
        )
        await mgr.handle_connection_update(_tl_connection())
        await mgr.handle_business_message("conn1", _tl_message(text="HELP me"))
        assert reply_calls[0]["context"]["trigger"] == "word"
