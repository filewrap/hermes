"""Tests for the Telegram Business Mode manager (Telethon-native port).

Direct-send model (v2): a customer message runs a reply turn and the
final text is sent straight to the customer via the business connection —
no draft card, no approval, no Send/Edit/Discard. The owner sees what was
sent (Recent + a minimal sent notice). Fakes: fake owner-DM
``send_message``, fake ``reply_via_turn``, fake customer delivery
``send_business_message``. No sleeps: debounce is driven with
``debounce_seconds=0`` or events.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from telethon.tl.types import (
    BotBusinessConnection,
    BusinessBotRights,
    Message,
    PeerChat,
    PeerUser,
)

from plugins.platforms.telegram.business_manager import (
    ONBOARDING_MESSAGE,
    BusinessModeManager,
)
from plugins.platforms.telegram.business_state import BusinessStateDB


@pytest.fixture()
def db(tmp_path):
    """Fresh business state DB."""
    sdb = BusinessStateDB(tmp_path / "biz_state.db")
    yield sdb
    sdb.close()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Sentinel distinguishing "no rights passed" from an explicit rights=None.
_UNSET = object()


def _tl_connection(
    *, conn_id="conn1", owner_id=42, enabled=True, reply=True, rights=_UNSET,
) -> Any:
    """A real TL BotBusinessConnection (owner DM chat id IS user_id)."""
    if rights is _UNSET:
        rights = BusinessBotRights(reply=reply)
    return BotBusinessConnection(
        connection_id=conn_id,
        user_id=owner_id,
        dc_id=1,
        date=None,
        disabled=not enabled,
        rights=rights,
    )


def _tl_message(
    *, msg_id=1, customer_chat_id=200, customer_id=999,
    text="Hi there", via_business_bot_id=None, peer=None, from_id=None,
) -> Any:
    """A real TL Message as delivered inside UpdateBotNewBusinessMessage."""
    return Message(
        id=msg_id,
        peer_id=peer if peer is not None else PeerUser(customer_chat_id),
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
        from_id=from_id if from_id is not None else PeerUser(customer_id),
        via_business_bot_id=via_business_bot_id,
    )


class _SentRecorder:
    """Capture send calls; signal an event per call so tests wait on it."""

    def __init__(self, *, fail: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self.fail = fail
        self._next_id = 1000
        self.called = asyncio.Event()

    def clear(self) -> None:
        """Forget past calls AND reset the wait event."""
        self.calls.clear()
        self.called.clear()

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.called.set()
        if self.fail:
            raise RuntimeError("simulated send failure")
        sent = SimpleNamespace(message_id=self._next_id, id=self._next_id)
        self._next_id += 1
        return sent


def _make_manager(db, *, reply_text="Reply text!", reply_fails=False,
                  send_recorder=None, biz_recorder=None,
                  debounce=0.0, trigger_mode="direct", trigger_word="",
                  persona=None):
    reply_calls: List[Dict[str, Any]] = []

    async def _reply(customer_text: str, customer_chat_id: str, context: dict) -> str:
        reply_calls.append({
            "customer_text": customer_text,
            "customer_chat_id": customer_chat_id,
            "context": context,
        })
        if reply_fails:
            raise RuntimeError("model boom")
        return reply_text

    sender = send_recorder if send_recorder is not None else _SentRecorder()
    biz = biz_recorder if biz_recorder is not None else _SentRecorder()
    mgr = BusinessModeManager(
        session_db=db,
        send_message=sender,
        reply_via_turn=_reply,
        send_business_message=biz,
        debounce_seconds=debounce,
        trigger_mode=trigger_mode,
        trigger_word=trigger_word,
        persona=persona,
    )
    return mgr, sender, biz, reply_calls


async def _tap(mgr, data, caller="42"):
    answered: List[Dict[str, Any]] = []

    async def _answer(**kw):
        answered.append(kw)

    result = await mgr.handle_callback(
        data=data, caller_user_id=caller, answer=_answer,
    )
    return result, answered


async def _connect(db, **kwargs):
    mgr, sender, biz, reply_calls = _make_manager(db, **kwargs)
    await mgr.handle_connection_update(_tl_connection())
    sender.clear()
    return mgr, sender, biz, reply_calls


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_first_connection_persists_and_sends_onboarding(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        row = db.get_telegram_business_connection("conn1")
        assert row is not None
        assert row["is_enabled"] is True
        assert row["can_reply"] is True
        # Owner DM chat id IS the connection user_id.
        assert row["owner_chat_id"] == "42"
        assert len(sender.calls) == 1
        assert sender.calls[0]["chat_id"] == 42
        assert "Business assistant" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_onboarding_describes_direct_send_no_emoji_no_draft(self, db):
        assert ONBOARDING_MESSAGE.startswith("<blockquote>")
        assert "You've connected me as your Telegram Business assistant." in ONBOARDING_MESSAGE
        assert "directly" in ONBOARDING_MESSAGE
        assert "draft" not in ONBOARDING_MESSAGE.lower()
        assert "approve" not in ONBOARDING_MESSAGE.lower()
        assert "`" not in ONBOARDING_MESSAGE
        assert not any(ord(ch) > 0x2500 for ch in ONBOARDING_MESSAGE)

    @pytest.mark.asyncio
    async def test_repeat_connection_does_not_resend_onboarding(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_connection_update(_tl_connection())
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_disconnection_dms_owner(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_connection_update(_tl_connection(enabled=False))
        assert any("ended" in c["text"].lower() for c in sender.calls)

    @pytest.mark.asyncio
    async def test_reply_right_flip_notifies_owner(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(reply=True))
        sender.clear()
        await mgr.handle_connection_update(_tl_connection(reply=False))
        assert sender.calls, "owner should be told send permission flipped"
        assert "Send-on-your-behalf is off" in sender.calls[0]["text"]
        sender.clear()
        await mgr.handle_connection_update(_tl_connection(reply=True))
        assert sender.calls and "Send-on-your-behalf is on" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_rights_none_means_no_reply_right(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(rights=None))
        row = db.get_telegram_business_connection("conn1")
        assert row["can_reply"] is False

    @pytest.mark.asyncio
    async def test_rights_stored_for_status_display(self, db):
        mgr, _, _, _ = _make_manager(db)
        rights = BusinessBotRights(reply=True, read_messages=True, edit_bio=True)
        await mgr.handle_connection_update(_tl_connection(rights=rights))
        row = db.get_telegram_business_connection("conn1")
        assert row["rights"]["reply"] is True
        assert row["rights"]["read_messages"] is True
        assert row["rights"]["edit_bio"] is True
        assert row["rights"]["delete_sent_messages"] is False

    @pytest.mark.asyncio
    async def test_dead_connections_purged_when_live_one_arrives(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(conn_id="conn-dead", enabled=False))
        assert db.get_telegram_business_connection("conn-dead") is not None
        await mgr.handle_connection_update(_tl_connection(conn_id="conn1", enabled=True))
        assert db.get_telegram_business_connection("conn-dead") is None
        assert db.get_telegram_business_connection("conn1") is not None

    def test_purge_dead_connections_sweeps_on_read(self, db):
        """Read-time sweep (design §5): a live connection clears dead rows."""
        mgr, _, _, _ = _make_manager(db)
        db.upsert_telegram_business_connection(
            connection_id="conn-dead", owner_user_id="42", owner_chat_id="42",
            can_reply=False, is_enabled=False)
        db.upsert_telegram_business_connection(
            connection_id="conn1", owner_user_id="42", owner_chat_id="42",
            can_reply=True, is_enabled=True)
        assert mgr.purge_dead_connections("42") == 1
        assert db.get_telegram_business_connection("conn-dead") is None
        assert db.get_telegram_business_connection("conn1") is not None

    def test_purge_dead_connections_keeps_dead_when_no_live_one(self, db):
        """No enabled connection: the dead row is the only trace — keep it."""
        mgr, _, _, _ = _make_manager(db)
        db.upsert_telegram_business_connection(
            connection_id="conn-dead", owner_user_id="42", owner_chat_id="42",
            can_reply=False, is_enabled=False)
        assert mgr.purge_dead_connections("42") == 0
        assert db.get_telegram_business_connection("conn-dead") is not None

    @pytest.mark.asyncio
    async def test_connection_missing_fields_ignored(self, db):
        mgr, sender, _, _ = _make_manager(db)
        broken = BotBusinessConnection(
            connection_id=None, user_id=42, dc_id=1, date=None,
            disabled=False, rights=BusinessBotRights(reply=True),
        )
        await mgr.handle_connection_update(broken)
        assert db.get_telegram_business_connection("conn1") is None
        assert sender.calls == []


# ---------------------------------------------------------------------------
# Direct-send flow
# ---------------------------------------------------------------------------


class TestBusinessMessageDirectSend:
    @pytest.mark.asyncio
    async def test_incoming_customer_message_direct_sends(self, db):
        mgr, sender, biz, reply_calls = _make_manager(db, reply_text="Hey, thanks!")
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message(text="Hi!"))
        # The reply went straight to the customer — no draft card, no approval.
        assert biz.calls, "reply must go through send_business_message"
        assert biz.calls[0]["connection_id"] == "conn1"
        assert biz.calls[0]["customer_chat_id"] == "200"
        assert biz.calls[0]["text"] == "Hey, thanks!"
        # Owner DMs are never used for customer delivery.
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        # The owner still sees what was sent: one minimal sent notice.
        assert len(sender.calls) == 1
        assert "Sent to" in sender.calls[0]["text"]
        assert "Hey, thanks!" in sender.calls[0]["text"]
        assert "chat" in sender.calls[0]["text"]
        # Both directions recorded in recent (newest first).
        recent = db.list_recent_business_messages("42")
        assert [r["text"] for r in recent] == ["Hey, thanks!", "Hi!"]
        assert [r["direction"] for r in recent] == ["out", "in"]
        # The reply turn got the design §5 context. Recent includes the
        # just-recorded incoming message itself.
        assert reply_calls[0]["context"]["connection_id"] == "conn1"
        assert reply_calls[0]["context"]["customer_chat_id"] == "200"
        assert reply_calls[0]["context"]["trigger"] == "direct"
        assert "persona" in reply_calls[0]["context"]
        recent_ctx = reply_calls[0]["context"]["recent_messages"]
        assert len(recent_ctx) == 1
        assert recent_ctx[0]["text"] == "Hi!"
        assert recent_ctx[0]["direction"] == "in"

    @pytest.mark.asyncio
    async def test_no_reply_right_skips_turn_but_records(self, db):
        mgr, sender, biz, reply_calls = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(reply=False))
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message())
        assert biz.calls == []
        assert reply_calls == []
        assert sender.calls == []
        # The message is still in recent for the owner to see.
        assert len(db.list_recent_business_messages("42")) == 1

    @pytest.mark.asyncio
    async def test_unknown_connection_silently_ignored(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        # No prior handle_connection_update — should silently skip.
        await mgr.handle_business_message("conn1", _tl_message())
        assert sender.calls == []
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_auto_draft_paused_skips_reply(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        db.set_telegram_business_auto_draft("conn1", auto_draft=False)
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message())
        assert sender.calls == []
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_paused_customer_chat_skipped(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        db.set_telegram_business_paused_chats("conn1", ["200"])
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message(customer_chat_id=200))
        assert sender.calls == []
        assert biz.calls == []
        # Other chat still replies.
        await mgr.handle_business_message("conn1", _tl_message(customer_chat_id=300))
        assert biz.calls

    @pytest.mark.asyncio
    async def test_empty_text_skipped_but_recorded(self, db):
        mgr, sender, biz, reply_calls = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message(text=None))
        assert sender.calls == []
        assert biz.calls == []
        assert reply_calls == []
        # Pure media without a caption: recorded in recent, no reply.
        assert len(db.list_recent_business_messages("42")) == 1

    @pytest.mark.asyncio
    async def test_reply_failure_reports_to_owner(self, db):
        mgr, sender, biz, _ = _make_manager(db, reply_fails=True)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_business_message("conn1", _tl_message(text="Hi"))
        assert sender.calls
        assert "couldn't reply" in sender.calls[0]["text"]
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_debounce_coalesces_burst(self, db):
        # Real debounce window; fire 3 messages in quick succession and
        # wait on the sent-notice event (no sleeps in the test).
        mgr, sender, biz, reply_calls = _make_manager(
            db, debounce=0.05, reply_text="single reply",
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        for i in range(3):
            await mgr.handle_business_message(
                "conn1", _tl_message(text=f"part {i}", msg_id=i),
            )
        await asyncio.wait_for(sender.called.wait(), timeout=5)
        owner_dms = [c for c in sender.calls if c.get("chat_id") == 42]
        assert len(owner_dms) == 1
        assert len(reply_calls) == 1
        assert reply_calls[0]["customer_text"] == "part 2"
        assert len(biz.calls) == 1
        assert biz.calls[0]["text"] == "single reply"

    @pytest.mark.asyncio
    async def test_superseded_timer_keeps_the_new_slot_handle(self, db):
        """A timer superseded *while already running* must not pop the new slot.

        The old cleanup popped the key unconditionally, so a timer that was
        cancelled during its (slow) reply call deleted its replacement's
        handle: a third message could then no longer cancel anything.
        """
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = {"n": 0}

        async def _reply(customer_text, customer_chat_id, context):
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await gate.wait()  # first reply hangs until superseded
            return f"reply to {customer_text}"

        sender = _SentRecorder()
        biz = _SentRecorder()
        mgr = BusinessModeManager(
            session_db=db, send_message=sender, reply_via_turn=_reply,
            send_business_message=biz,
            debounce_seconds=0.01,
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()

        key = "conn1:200"
        await mgr.handle_business_message("conn1", _tl_message(text="one"))
        first_task = mgr._debounce_tasks[key]
        await asyncio.wait_for(started.wait(), timeout=5)
        await mgr.handle_business_message("conn1", _tl_message(text="two"))
        second_task = mgr._debounce_tasks[key]
        assert second_task is not first_task
        await asyncio.sleep(0)  # let the cancelled first timer run its cleanup
        assert mgr._debounce_tasks.get(key) is second_task

        gate.set()
        await asyncio.wait_for(sender.called.wait(), timeout=5)
        assert len(biz.calls) == 1
        assert biz.calls[0]["text"] == "reply to two"

    @pytest.mark.asyncio
    async def test_owner_outgoing_message_not_replied(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(owner_id=42))
        sender.clear()
        await mgr.handle_business_message(
            "conn1", _tl_message(customer_id=42, text="On my way"),
        )
        assert sender.calls == []
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_bot_sent_message_not_replied(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        # A direct reply echoes back with via_business_bot_id set.
        await mgr.handle_business_message(
            "conn1", _tl_message(text="Reply text!", via_business_bot_id=7),
        )
        assert sender.calls == []
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_owner_reply_keeps_pending_customer_reply(self, db):
        mgr, sender, biz, _ = _make_manager(
            db, debounce=0.05, reply_text="customer reply",
        )
        await mgr.handle_connection_update(_tl_connection(owner_id=42))
        sender.clear()
        await mgr.handle_business_message(
            "conn1", _tl_message(text="Is it open?", msg_id=1),
        )
        # The owner answers by hand inside the debounce window.
        await mgr.handle_business_message(
            "conn1", _tl_message(customer_id=42, text="Yes", msg_id=2),
        )
        await asyncio.wait_for(sender.called.wait(), timeout=5)
        assert len(biz.calls) == 1
        assert biz.calls[0]["text"] == "customer reply"

    @pytest.mark.asyncio
    async def test_group_business_message_ignored(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_business_message(
            "conn1", _tl_message(peer=PeerChat(123)),
        )
        assert sender.calls == []
        assert biz.calls == []

    @pytest.mark.asyncio
    async def test_reply_lock_is_per_customer_chat(self, db):
        """A blocked reply for one customer must not stall another chat."""
        gate = asyncio.Event()
        started = asyncio.Event()

        async def _reply(customer_text, customer_chat_id, context):
            if customer_chat_id == "201":
                started.set()
                await gate.wait()
                return "reply A"
            return "reply B"

        sender = _SentRecorder()
        biz = _SentRecorder()
        mgr = BusinessModeManager(
            session_db=db, send_message=sender, reply_via_turn=_reply,
            send_business_message=biz, debounce_seconds=0.0,
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        task_a = asyncio.create_task(mgr.handle_business_message(
            "conn1", _tl_message(customer_chat_id=201, text="A"),
        ))
        await asyncio.wait_for(started.wait(), timeout=5)
        # Chat 202 replies while 201 is still blocked on its turn.
        await mgr.handle_business_message(
            "conn1", _tl_message(customer_chat_id=202, text="B"),
        )
        assert any(c["text"] == "reply B" for c in biz.calls)
        gate.set()
        await asyncio.wait_for(task_a, timeout=5)
        assert any(c["text"] == "reply A" for c in biz.calls)

    @pytest.mark.asyncio
    async def test_handle_business_delete_is_a_no_op(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await mgr.handle_business_message("conn1", _tl_message(msg_id=11, text="one"))
        assert len(db.list_recent_business_messages("42")) == 2
        discarded = await mgr.handle_business_delete("conn1", PeerUser(200), [11])
        assert discarded == 0
        assert len(db.list_recent_business_messages("42")) == 2

    @pytest.mark.asyncio
    async def test_handle_business_delete_unknown_connection_no_ops(self, db):
        mgr, _, _, _ = _make_manager(db)
        assert await mgr.handle_business_delete("nope", PeerUser(200), [11]) == 0


# ---------------------------------------------------------------------------
# Owner invocation (the @mention in a business chat)
# ---------------------------------------------------------------------------


class TestOwnerInvocation:
    @pytest.mark.asyncio
    async def test_owner_invocation_direct_sends(self, db):
        mgr, sender, biz, reply_calls = _make_manager(db, reply_text="4")
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_owner_invocation("conn1", "200", "Say what's 2+2?", "Alt")
        assert biz.calls
        assert biz.calls[0]["connection_id"] == "conn1"
        assert biz.calls[0]["customer_chat_id"] == "200"
        assert biz.calls[0]["text"] == "4"
        assert reply_calls[0]["context"]["trigger"] == "owner"
        assert reply_calls[0]["context"]["owner_text"] == "Say what's 2+2?"
        assert sender.calls and "Sent to" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_owner_invocation_bypasses_trigger_gate(self, db):
        mgr, sender, biz, _ = _make_manager(
            db, trigger_mode="trigger", trigger_word="help",
        )
        await mgr.handle_connection_update(_tl_connection())
        sender.clear()
        await mgr.handle_owner_invocation("conn1", "200", "What is 2+2?", "Alt")
        assert biz.calls

    @pytest.mark.asyncio
    async def test_owner_invocation_skipped_when_no_reply_right(self, db):
        mgr, sender, biz, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection(reply=False))
        sender.clear()
        await mgr.handle_owner_invocation("conn1", "200", "Say hi", "Alt")
        assert biz.calls == []
        assert sender.calls == []


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class TestCallbackDispatch:
    @pytest.mark.asyncio
    async def test_non_business_callback_returns_false(self, db):
        mgr, _, _, _ = _make_manager(db)

        async def _noop(**kw):
            pass

        dispatched = await mgr.handle_callback(
            data="ea:once:1", caller_user_id="42", answer=_noop,
        )
        assert dispatched is False

    @pytest.mark.asyncio
    async def test_card_action_returns_action_name_for_ui(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        result, answered = await _tap(mgr, "hm:biz:pause")
        assert result == "pause"
        # The manager does not render card buttons; the UI worker owns that.
        assert answered == []
        # ... and does not mutate state on its own.
        assert db.get_telegram_business_connection("conn1")["auto_draft"] is True

    @pytest.mark.asyncio
    async def test_unknown_action_answers(self, db):
        mgr, _, _, _ = _make_manager(db)
        result, answered = await _tap(mgr, "hm:biz:bogus")
        assert result is True
        assert answered and "Unknown action" in answered[0]["text"]

    @pytest.mark.asyncio
    async def test_malformed_action_answers(self, db):
        mgr, _, _, _ = _make_manager(db)
        result, answered = await _tap(mgr, "hm:biz:a:b:c")
        assert result is True
        assert answered and "Invalid action" in answered[0]["text"]

    @pytest.mark.asyncio
    async def test_mute_unmute_apply_and_return_recent(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await mgr.handle_business_message("conn1", _tl_message(customer_chat_id=200))
        result, answered = await _tap(mgr, "hm:biz:mute:200")
        assert result == "recent"
        assert answered == []
        assert db.get_telegram_business_connection("conn1")["paused_chats"] == ["200"]
        result, _ = await _tap(mgr, "hm:biz:unmute:200")
        assert result == "recent"
        assert db.get_telegram_business_connection("conn1")["paused_chats"] == []

    @pytest.mark.asyncio
    async def test_mute_unknown_chat_answers_and_changes_nothing(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        result, answered = await _tap(mgr, "hm:biz:mute:999")
        assert result is True
        assert answered and "Unknown customer chat" in answered[0]["text"]

    @pytest.mark.asyncio
    async def test_trigger_word_tap_arms_owner_input(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        result, answered = await _tap(mgr, "hm:biz:trigger_word")
        assert result == "trigger_word"
        assert answered == []
        assert mgr._pending_owner_input["42"] == "trigger_word"


# ---------------------------------------------------------------------------
# Owner input capture (trigger word / persona)
# ---------------------------------------------------------------------------


class TestOwnerInputCapture:
    @pytest.mark.asyncio
    async def test_trigger_word_tap_then_text_sets_word(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await _tap(mgr, "hm:biz:trigger_word")
        sender.clear()
        changes = await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="help",
        )
        assert changes == {"trigger_word": "help"}
        assert mgr.get_settings()["trigger_word"] == "help"
        assert sender.calls and "Trigger word set to help" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_persona_tap_then_text_sets_persona(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await _tap(mgr, "hm:biz:persona")
        sender.clear()
        changes = await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="Be terse.",
        )
        assert changes == {"owner_persona": "Be terse."}
        assert mgr.get_settings()["persona"] == "Be terse."
        assert sender.calls and "Persona updated" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_owner_input_idle_returns_none(self, db):
        mgr, _, _, _ = _make_manager(db)
        assert await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="hello",
        ) is None

    @pytest.mark.asyncio
    async def test_empty_input_cancels_capture(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await _tap(mgr, "hm:biz:trigger_word")
        sender.clear()
        changes = await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="   ",
        )
        assert changes == {}
        assert sender.calls and "Cancelled" in sender.calls[0]["text"]
        # The capture is consumed, so a later message is not eaten.
        assert await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="help",
        ) is None

    @pytest.mark.asyncio
    async def test_multi_word_trigger_word_rejected_and_retry_works(self, db):
        mgr, sender, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await _tap(mgr, "hm:biz:trigger_word")
        sender.clear()
        changes = await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="two words",
        )
        assert changes == {}
        assert sender.calls and "single word" in sender.calls[0]["text"]
        # The capture survived, so the retry still applies.
        changes = await mgr.maybe_handle_owner_input(
            owner_chat_id="42", text="help",
        )
        assert changes == {"trigger_word": "help"}


# ---------------------------------------------------------------------------
# Query / mutation helpers + settings
# ---------------------------------------------------------------------------


class TestManagerHelpers:
    @pytest.mark.asyncio
    async def test_list_connections_recent(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        await mgr.handle_business_message("conn1", _tl_message(text="hello"))
        assert len(mgr.list_connections(owner_user_id="42")) == 1
        assert [r["text"] for r in mgr.recent_messages("42")] == ["Reply text!", "hello"]

    @pytest.mark.asyncio
    async def test_set_auto_draft_and_paused_chats(self, db):
        mgr, _, _, _ = _make_manager(db)
        await mgr.handle_connection_update(_tl_connection())
        mgr.set_auto_draft("conn1", False)
        assert db.get_telegram_business_connection("conn1")["auto_draft"] is False
        mgr.set_paused_chats("conn1", ["200"])
        assert db.get_telegram_business_connection("conn1")["paused_chats"] == ["200"]

    def test_set_trigger_and_persona_are_in_memory(self, db):
        mgr, _, _, _ = _make_manager(db)
        out = mgr.set_trigger(mode="trigger", word="help")
        assert out["trigger_mode"] == "trigger"
        assert out["trigger_word"] == "help"
        out = mgr.set_persona("Be terse.")
        assert out["persona"] == "Be terse."
        settings = mgr.get_settings()
        assert settings["trigger_mode"] == "trigger"
        assert settings["trigger_word"] == "help"
        assert settings["persona"] == "Be terse."
        assert settings["debounce_seconds"] == 0.0
        assert "draft_ttl_hours" not in settings
        with pytest.raises(ValueError):
            mgr.set_trigger(mode="bogus")
        with pytest.raises(ValueError):
            mgr.set_trigger(word="two words")

    def test_sent_notice_carries_chat_badge(self, db):
        mgr, _, _, _ = _make_manager(db)
        notice = mgr._render_sent_notice(
            customer_name="Alt", customer_chat_id="200", reply_text="4",
        )
        assert "Sent to" in notice
        assert "Alt" in notice
        assert "chat" in notice
        assert "4" in notice
