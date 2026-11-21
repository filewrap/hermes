"""Correlated-reply waiter: strong reply-to match, DM-only weak match, expiry."""

import threading
import time

from tools import conversation_reply_wait as waiter_mod


def _waiter(chat_type="dm", **kw):
    return waiter_mod.register_waiter(
        "/tmp/home-a", "telegram", "-10042", chat_type=chat_type, timeout_seconds=30, **kw)


def _notify(message_id, reply_to=None, **kw):
    return waiter_mod.notify_inbound(
        "/tmp/home-a", "telegram", "-10042", message_id=message_id,
        reply_to_msg_id=reply_to, text="hello back", **kw)


def test_strong_reply_to_match_settles_in_group():
    waiter = _waiter(chat_type="group")
    waiter_mod.set_sent_message(waiter, 555)
    assert _notify(777, reply_to=555) == 1
    status, message = waiter_mod.await_reply(waiter)
    assert status == "replied"
    assert message["message_id"] == 777
    assert message["text"] == "hello back"


def test_weak_match_only_in_dm():
    group = _waiter(chat_type="group")
    waiter_mod.set_sent_message(group, 555)
    assert _notify(777) == 0  # a bare group message is not a reply to us
    waiter_mod.cancel_waiter(group)

    dm = _waiter(chat_type="dm")
    waiter_mod.set_sent_message(dm, 556)
    assert _notify(778) == 1
    status, message = waiter_mod.await_reply(dm)
    assert status == "replied" and message["message_id"] == 778


def test_bot_messages_never_match():
    waiter = _waiter(chat_type="dm")
    waiter_mod.set_sent_message(waiter, 555)
    assert _notify(777, is_bot=True) == 0
    assert _notify(778, reply_to=555, is_bot=True) == 0
    waiter_mod.cancel_waiter(waiter)


def test_wrong_chat_does_not_match():
    waiter = _waiter(chat_type="dm")
    waiter_mod.set_sent_message(waiter, 555)
    assert waiter_mod.notify_inbound(
        "/tmp/home-a", "telegram", "-10099", message_id=777, text="elsewhere") == 0
    assert waiter_mod.notify_inbound(
        "/tmp/other-home", "telegram", "-10042", message_id=777, text="other home") == 0
    waiter_mod.cancel_waiter(waiter)


def test_pre_stamp_reply_to_is_not_lost():
    waiter = _waiter(chat_type="group")
    _notify(777, reply_to=555)  # arrived before the send was stamped
    waiter_mod.set_sent_message(waiter, 555)
    status, message = waiter_mod.await_reply(waiter)
    assert status == "replied" and message["message_id"] == 777


def test_pre_stamp_bare_message_is_not_weak_matched():
    waiter = _waiter(chat_type="dm")
    _notify(777)  # arrived before the send; may predate it entirely
    waiter_mod.set_sent_message(waiter, 555)
    assert not waiter["event"].is_set()
    waiter_mod.cancel_waiter(waiter)


def test_timeout_when_nobody_replies():
    waiter = waiter_mod.register_waiter(
        "/tmp/home-a", "telegram", "-10042", chat_type="dm", timeout_seconds=0.2)
    waiter_mod.set_sent_message(waiter, 555)
    started = time.monotonic()
    status, message = waiter_mod.await_reply(waiter)
    assert status == "timeout" and message is None
    assert time.monotonic() - started < 5


def test_interrupt_ends_the_wait():
    waiter = _waiter(chat_type="dm")
    waiter_mod.set_sent_message(waiter, 555)
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()
    status, _message = waiter_mod.await_reply(waiter, interrupted=flag.is_set)
    assert status == "interrupted"


def test_expired_waiter_is_pruned_and_not_notified():
    waiter = waiter_mod.register_waiter(
        "/tmp/home-a", "telegram", "-10042", chat_type="dm", timeout_seconds=30)
    waiter_mod.set_sent_message(waiter, 555)
    waiter["deadline"] = time.monotonic() - 1  # expired without waiting it out
    assert _notify(777) == 0
    waiter_mod.cancel_waiter(waiter)
