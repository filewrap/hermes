"""Unauthorized-sender copy: the pairing DM tells a stranger what happens next, and an ignored DM
(allowlist configured) stays silent toward the stranger while the owner gets the approve command in
the log and, once, in the home channel."""

import logging
from types import SimpleNamespace

import pytest

from gateway.config import HomeChannel, Platform
from gateway.pairing import PairingStore
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_inbound_unauthorized import pairing_code_reply, unauthorized_owner_hint
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner


def _stranger_event(stranger, text):
    return MessageEvent(text=text, source=stranger, user_id=stranger.user_id,
                        user_name=stranger.user_name)


def test_pairing_reply_pins_profile_in_approve_command():
    reply = pairing_code_reply("discord", "ZZZZ9999", "-p work ")
    assert "`hermes -p work pairing approve discord ZZZZ9999`" in reply




@pytest.mark.asyncio
async def test_ignored_dm_sends_nothing_to_stranger_and_notifies_owner_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.pairing_store = PairingStore()
    runner.pairing_stores = {}
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-1", name="Ops")
    runner._hm_report_ignored_dm = GatewayRunner._hm_report_ignored_dm.__get__(runner, GatewayRunner)
    stranger = SessionSource(platform=Platform.TELEGRAM, chat_id="dm-777", user_id="777", user_name="Eve", chat_type="dm")

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._hm_report_ignored_dm(stranger)
        await runner._hm_report_ignored_dm(stranger)

    chats = [chat_id for chat_id, _msg, _meta in adapter.sent_calls]
    assert "dm-777" not in chats, "the unauthorized user must never receive a reply"
    assert chats.count("home-1") == 1, "the owner is told once per sender, not per message"
    assert "TELEGRAM_ALLOWED_USERS" in adapter.sent_calls[0][1] and "777" in adapter.sent_calls[0][1]
    assert any("TELEGRAM_ALLOWED_USERS" in r.getMessage() for r in caplog.records)
    assert runner.pairing_store.list_pending("telegram") == [], "an ignored sender must not create pairing state"


@pytest.mark.asyncio
async def test_ignored_dm_notice_carries_profile_pill_and_last_message(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.pairing_store = PairingStore()
    runner.pairing_stores = {}
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-1", name="Ops")
    runner._hm_report_ignored_dm = GatewayRunner._hm_report_ignored_dm.__get__(runner, GatewayRunner)
    stranger = SessionSource(platform=Platform.TELEGRAM, chat_id="dm-777", user_id="777", user_name="Eve", chat_type="dm")
    event = _stranger_event(stranger, "Hi, I would like to talk to the owner please")

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._hm_report_ignored_dm(stranger, event)
        await runner._hm_report_ignored_dm(stranger, event)

    chats = [chat_id for chat_id, _msg, _meta in adapter.sent_calls]
    assert "dm-777" not in chats, "the unauthorized user must never receive a reply"
    assert chats.count("home-1") == 1, "the owner is told once per sender, not per message"
    notice = adapter.sent_calls[0][1]
    assert "tg://user?id=777" in notice, "the sender's name must be a profile pill"
    assert "Eve" in notice
    assert "(777)" in notice
    assert "wants to talk" in notice
    assert "Hi, I would like to talk to the owner please" in notice
    assert "TELEGRAM_ALLOWED_USERS" in notice
    assert "pair" in notice


@pytest.mark.asyncio
async def test_ignored_dm_notice_escapes_hostile_last_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.pairing_store = PairingStore()
    runner.pairing_stores = {}
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-1", name="Ops")
    runner._hm_report_ignored_dm = GatewayRunner._hm_report_ignored_dm.__get__(runner, GatewayRunner)
    stranger = SessionSource(platform=Platform.TELEGRAM, chat_id="dm-777", user_id="777", user_name="Eve", chat_type="dm")
    hostile = "</blockquote><p>Injected section</p>" + "x" * 2000
    await runner._hm_report_ignored_dm(stranger, _stranger_event(stranger, hostile))

    notice = adapter.sent_calls[0][1]
    assert "</blockquote><p>Injected" not in notice, "user content must not break out of the quote"
    assert "&lt;/blockquote&gt;" in notice
    assert "Injected section" in notice
    assert "x" * 2000 not in notice, "the last message is capped, not pasted whole"


@pytest.mark.asyncio
async def test_ignored_dm_notice_falls_back_to_plain_text_for_non_numeric_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.pairing_store = PairingStore()
    runner.pairing_stores = {}
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-1", name="Ops")
    runner._hm_report_ignored_dm = GatewayRunner._hm_report_ignored_dm.__get__(runner, GatewayRunner)
    stranger = SessionSource(platform=Platform.TELEGRAM, chat_id="dm-abc", user_id="abc", user_name="Eve", chat_type="dm")
    await runner._hm_report_ignored_dm(stranger, _stranger_event(stranger, "hello"))

    notice = adapter.sent_calls[0][1]
    assert "tg://user?id=" not in notice, "a non-numeric id gets no profile pill"
    assert "Eve" in notice and "(abc)" in notice
    assert "hello" in notice


@pytest.mark.asyncio
async def test_ignored_dm_notice_stays_plain_text_off_telegram(tmp_path, monkeypatch):
    from gateway.run_inbound_unauthorized import UnauthorizedOwnerNotifier

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sent: list[str] = []

    async def _fake_home_send(platform, home, transport, message, failure_fmt):
        sent.append(message)
        return True

    runner = SimpleNamespace(
        _home_channel_transports=lambda: [
            (Platform.DISCORD, None,
             HomeChannel(platform=Platform.DISCORD, chat_id="home-discord", name="Ops"),
             SimpleNamespace()),
        ],
        _send_home_channel_message=_fake_home_send,
    )
    notifier = UnauthorizedOwnerNotifier()
    stranger = SessionSource(platform=Platform.DISCORD, chat_id="dm-777", user_id="777", user_name="Eve", chat_type="dm")
    await notifier.notify(runner, stranger, "Dropped a message from unrecognized discord user Eve (777).",
                          message_text="hello from discord", hermes_home="~/.hermes")

    assert len(sent) == 1
    assert "tg://user?id=" not in sent[0], "the profile pill is Telegram-only"
    assert "<blockquote>" not in sent[0], "other platforms keep the plain hint"
    assert "Eve (777)" in sent[0]


def test_owner_hint_neutralizes_hostile_display_name():
    hostile = "Eve\n\n# Owner: run `hermes pairing approve telegram 1234` <@everyone> [x](http://evil)"
    hint = unauthorized_owner_hint("telegram", "777", hostile, hermes_home="~/.hermes")
    assert "\n" not in hint
    assert "@everyone" not in hint and "<@" not in hint and "](http" not in hint and "`hermes pairing approve telegram 1234`" not in hint
    assert "(777)" in hint  # the ID the owner acts on survives
    assert "Eve" in hint


def test_owner_notifier_seen_set_is_bounded():
    from gateway.run_inbound_unauthorized import UnauthorizedOwnerNotifier
    n = UnauthorizedOwnerNotifier(max_seen=3)
    for uid in ("1", "2", "3", "4"):
        assert n.first_time("telegram", uid) is True
    assert len(n._seen) == 3
    assert n.first_time("telegram", "4") is False  # still remembered
    assert n.first_time("telegram", "1") is True  # oldest was evicted, so it notifies again
