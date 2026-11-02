"""Telegram ``/topic`` mode: opt-in root-DM auto transfer (``dm_topic_auto_transfer``, #139136).

Driven through the real ``TelethonAdapter.handle_message`` ingress (topic transfer, topic
recovery, session keying, background processing, final send) into the real
``GatewayRunner._handle_message``; only the MTProto client (a fake recording requests) and the
model call (``runner._run_agent``) are faked.

* default (key unset): a root-DM message keeps the v0.22 lobby recovery — it is answered in the
  user's most recently bound topic (#136567);
* opted in: the message opens a fresh topic via ``CreateForumTopicRequest`` and the reply lands there;
* opted in but topic creation fails: it falls back to lobby recovery.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import InputPeerUser

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from hermes_state import SessionDB
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.gateway.test_telegram_topic_mode import (
    _make_runner,
    _make_source,
    _seed_two_topic_bindings,
)
from tests.waiting import async_wait_until

NEW_TOPIC_ID = 777


class _FakeMTProtoClient:
    """Minimal Telethon-shaped client: records requests, answers sends."""

    def __init__(self):
        self.requests: list = []
        self._next_message_id = 9000
        # Inbound message id -> topic id, for reply-anchor verification: a lobby reply
        # quotes the triggering message, so get_messages must find it in its topic.
        self.anchor_topics: dict[int, int] = {}

    async def __call__(self, request):
        self.requests.append(request)
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    async def get_input_entity(self, target):
        digits = str(target).lstrip("-")
        user_id = int(digits) if digits.isdigit() else 208214988
        return InputPeerUser(user_id=user_id, access_hash=0)

    async def get_messages(self, peer, ids=None):
        # Fabricate the triggering message in its topic so verified_topic_reply
        # accepts the quote anchor; anything else is genuinely unfindable.
        if isinstance(ids, int) and ids in self.anchor_topics:
            from telethon.tl.types import PeerUser
            return SimpleNamespace(
                id=ids,
                peer_id=PeerUser(208214988),
                reply_to=SimpleNamespace(reply_to_top_id=self.anchor_topics[ids]),
            )
        return None


def _rig(tmp_path, monkeypatch, *, extra: dict, create_fails: bool = False):
    import gateway.run as gateway_run

    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_two_topic_bindings(db)  # topic mode on; topic "222" is the most recently bound
    # The stub session store names every topic-lane session "sess-topic"; the binding FK needs its row.
    db.create_session(session_id="sess-topic", source="telegram", user_id="208214988")
    runner = _make_runner(session_db=db)
    runner.config.platforms[Platform.TELEGRAM].extra.update(extra)
    runs: list[dict] = []

    async def fake_run_agent(*args, **kwargs):
        runs.append(kwargs)
        return {"success": True, "final_response": "agent reply", "session_id": kwargs.get("session_id"),
                "messages": []}

    runner._run_agent = AsyncMock(side_effect=fake_run_agent)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="***", extra=dict(extra)))
    client = _FakeMTProtoClient()
    adapter._client = client
    # The seeded most-recent topic is 222; lobby replies quote the triggering
    # message 4242, so the fake client must verify that anchor in topic 222.
    client.anchor_topics = {4242: 222}
    if create_fails:
        adapter._create_dm_topic = AsyncMock(side_effect=RuntimeError("chat is not a forum"))
    else:
        adapter._create_dm_topic = AsyncMock(return_value=NEW_TOPIC_ID)
    runner.adapters[Platform.TELEGRAM] = adapter
    # The production wiring: every callback GatewayRunner installs on an adapter (topic recovery and,
    # where it exists, the topic-transfer hook), with the message handler pointed at the real runner.
    runner._busy_text_mode = "interrupt"
    runner._wire_adapter_handlers(
        adapter, message_handler=runner._handle_message,
        authorization_check=lambda *_a, **_k: True, platform_event_handler=AsyncMock())
    return runner, adapter, client, db, runs


async def _deliver_root_dm(adapter, client, runs, text: str = "plan the migration") -> MessageEvent:
    """Drive a root-DM MessageEvent through the real Telethon ingress; wait for the turn + reply."""
    event = MessageEvent(text=text, source=_make_source(thread_id=None), message_id="4242")
    await adapter.handle_message(event)
    await async_wait_until(
        lambda: len(runs) >= 1 and bool(_reply_requests(client)),
        what="agent turn ran and the reply was sent")
    return event


def _reply_requests(client) -> list:
    """The agent's final reply sends (not typing indicators or one-off notices)."""
    return [req for req in client.requests
            if isinstance(req, SendMessageRequest) and "agent reply" in str(req.message)]


@pytest.mark.asyncio
async def test_default_root_dm_keeps_lobby_recovery_to_last_bound_topic(tmp_path, monkeypatch):
    """Key unset: no topic is created; the turn and its reply go to the last bound topic (#136567)."""
    _runner, adapter, client, db, runs = _rig(tmp_path, monkeypatch, extra={})

    event = await _deliver_root_dm(adapter, client, runs)

    adapter._create_dm_topic.assert_not_awaited()
    assert event.source.thread_id == "222"
    assert len(runs) == 1
    (reply,) = _reply_requests(client)
    assert reply.reply_to.top_msg_id == 222
    # A lobby reply quotes the triggering message inside the lobby topic (DM-topic lanes
    # reply to the triggering message); only a fresh-topic reply is root-addressed.
    assert reply.reply_to.reply_to_msg_id == 4242
    db.close()


@pytest.mark.asyncio
async def test_opt_in_root_dm_opens_new_topic_and_reply_lands_there(tmp_path, monkeypatch):
    _runner, adapter, client, db, runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})

    event = await _deliver_root_dm(adapter, client, runs)

    adapter._create_dm_topic.assert_awaited_once()
    assert adapter._create_dm_topic.await_args.args[0] == 208214988
    assert event.source.thread_id == str(NEW_TOPIC_ID)
    assert len(runs) == 1, "the root-DM message must run as a turn, not get the lobby reminder"
    (reply,) = _reply_requests(client)
    assert reply.reply_to.top_msg_id == NEW_TOPIC_ID
    # The root-DM message lives outside the new topic: the reply is addressed at the topic
    # root itself, never as a quote of the inbound message 4242.
    assert reply.reply_to.reply_to_msg_id == NEW_TOPIC_ID
    assert reply.reply_to.reply_to_msg_id != 4242
    # The new topic is a bound session lane, so the next message inside it continues this session.
    binding = db.get_telegram_topic_binding(chat_id="208214988", thread_id=str(NEW_TOPIC_ID))
    assert binding is not None and binding["user_id"] == "208214988"
    db.close()


@pytest.mark.asyncio
async def test_opt_in_topic_create_failure_falls_back_to_lobby_recovery(tmp_path, monkeypatch):
    _runner, adapter, client, db, runs = _rig(
        tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True}, create_fails=True)

    event = await _deliver_root_dm(adapter, client, runs)

    adapter._create_dm_topic.assert_awaited_once()
    assert event.source.thread_id == "222"
    assert len(runs) == 1
    (reply,) = _reply_requests(client)
    assert reply.reply_to.top_msg_id == 222
    db.close()


@pytest.mark.asyncio
async def test_opt_in_root_dm_command_stays_in_the_lobby(tmp_path, monkeypatch):
    """``/topic`` (and every other command) typed in the root DM is a lobby command: no topic."""
    _runner, adapter, _client, db, runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})

    event = MessageEvent(text="/topic help", source=_make_source(thread_id=None), message_id="4242")
    await adapter.handle_message(event)
    await async_wait_until(
        lambda: not adapter._session_tasks or all(t.done() for t in adapter._session_tasks.values()),
        what="command session task settled")

    adapter._create_dm_topic.assert_not_awaited()
    assert event.source.thread_id is None  # the transfer hook declines commands at ingress
    assert runs == []
    db.close()


def test_top_level_telegram_yaml_key_reaches_extra(monkeypatch):
    """``telegram: dm_topic_auto_transfer: true`` (top-level YAML) lands in PlatformConfig.extra,
    like its neighbour ``disable_topic_auto_rename``."""
    from plugins.platforms.telegram.adapter import _apply_yaml_config

    extras = _apply_yaml_config({}, {"dm_topic_auto_transfer": True}) or {}
    assert extras.get("dm_topic_auto_transfer") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("launch,secondary", [(False, True), (True, False), (False, False), (True, True)])
async def test_receiving_secondary_bot_controls_opt_in(tmp_path, monkeypatch, launch, secondary):
    """Multiplexed: the bot that RECEIVED the root DM owns ``dm_topic_auto_transfer``, not the launch bot."""
    runner, primary, _client, db, _runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": launch})
    runner.config.multiplex_profiles = True
    runner._primary_profile_name = "default"
    second = TelethonAdapter(PlatformConfig(enabled=True, token="***", extra={"dm_topic_auto_transfer": secondary}))
    second._client = _FakeMTProtoClient()
    second._create_dm_topic = AsyncMock(return_value=NEW_TOPIC_ID)
    second.gateway_runner = runner
    second.set_owner_profile("probe-secondary")
    runner._profile_adapters = {"probe-secondary": {Platform.TELEGRAM: second}}
    db.enable_telegram_topic_mode(chat_id="208214988", user_id="208214988", profile_name="probe-secondary")
    db.create_session(session_id="secondary-old", source="telegram", user_id="208214988",
                      profile_name="probe-secondary")
    db.bind_telegram_topic(chat_id="208214988", thread_id="333", user_id="208214988",
                           profile_name="probe-secondary", session_key="secondary-old-route",
                           session_id="secondary-old")
    done = asyncio.Event()

    async def capture(_event):
        done.set()

    runner._wire_adapter_handlers(second, message_handler=capture, authorization_check=lambda *_a, **_k: True,
                                  platform_event_handler=AsyncMock())
    source = second.build_source(chat_id="208214988", user_id="208214988", chat_type="dm", message_id="4242")
    event = MessageEvent(text="root prompt", source=source, message_id="4242")
    await second.handle_message(event)
    await asyncio.wait_for(done.wait(), 10)
    await async_wait_until(lambda: not second._session_tasks
                           or all(t.done() for t in second._session_tasks.values()),
                           what="secondary session task settles")
    db.close()
    assert second._create_dm_topic.await_count == int(secondary)
    assert event.source.thread_id == (str(NEW_TOPIC_ID) if secondary else "333")
    primary._create_dm_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_topic_reply_anchor_survives_native_codec(tmp_path, monkeypatch):
    """A durably admitted (snapshot -> restore) transferred event still sends without quoting the root DM."""
    from gateway.session_envelope import restore_native, snapshot_native
    from gateway.session_ingress import deliver_response
    from plugins.platforms.telegram.telegram_topic_transfer import apply_root_dm_topic_transfer

    runner, adapter, client, db, _runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})
    runner.config.multiplex_profiles = False
    runner._is_user_authorized = lambda _source, **_kwargs: True
    event = MessageEvent(text="root prompt", source=_make_source(), message_id="4242")
    await apply_root_dm_topic_transfer(adapter, event)
    assert event.source.thread_id == str(NEW_TOPIC_ID)
    payload = snapshot_native(runner, event)
    restored = restore_native(payload, runner)
    assert restored.message_id == "4242"  # inbound identity is kept
    await deliver_response(adapter, restored, payload["native_text_v1"]["route"], "restored reply")
    sends = [req for req in client.requests
             if isinstance(req, SendMessageRequest) and "restored reply" in str(req.message)]
    db.close()
    assert len(sends) == 1
    send = sends[0]
    assert send.reply_to.top_msg_id == NEW_TOPIC_ID
    assert send.reply_to.reply_to_msg_id == NEW_TOPIC_ID  # topic root, not a quote of 4242
    # The default (None) override stays out of the envelope, so older retries keep their fingerprint.
    plain = MessageEvent(text="x", source=_make_source(), message_id="4243")
    assert "reply_anchor_override" not in snapshot_native(runner, plain)["native_text_v1"]["event"]


@pytest.mark.asyncio
async def test_second_root_dm_does_not_cancel_an_in_flight_topic_transfer(tmp_path, monkeypatch):
    """A second root-DM chunk arriving while ``CreateForumTopic`` for the first is still in flight must
    not cancel the committed transfer: the first message keeps its topic and every created topic
    receives its message. (The fork has no PTB text batching and no held-inbound queue: the two
    events are driven concurrently through ``handle_message`` directly, not merged into one batch.)"""
    _runner, adapter, client, db, runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})
    release = asyncio.Event()
    ids = iter(range(NEW_TOPIC_ID, NEW_TOPIC_ID + 10))

    async def paused_create(chat_id, title=None, **_kwargs):
        await release.wait()
        return next(ids)

    adapter._create_dm_topic = AsyncMock(side_effect=paused_create)
    first = MessageEvent(text="plan the migration", source=_make_source(thread_id=None), message_id="4242")
    second = MessageEvent(text="and the rollback", source=_make_source(thread_id=None), message_id="4243")
    first_task = asyncio.create_task(adapter.handle_message(first))
    await async_wait_until(lambda: adapter._create_dm_topic.await_count == 1, what="first create in flight")
    second_task = asyncio.create_task(adapter.handle_message(second))
    await async_wait_until(lambda: adapter._create_dm_topic.await_count == 2, what="second create in flight")
    release.set()
    await asyncio.gather(first_task, second_task)
    await async_wait_until(lambda: len(runs) >= 2 and len(_reply_requests(client)) >= 2,
                           what="both turns ran and both replies were sent")
    db.close()
    created = adapter._create_dm_topic.await_count
    assert created == 2
    assert first.source.thread_id == str(NEW_TOPIC_ID)
    landed = {first.source.thread_id, second.source.thread_id}
    assert landed == {str(NEW_TOPIC_ID + i) for i in range(created)}, "a created topic was left empty"
    assert len(runs) == 2
