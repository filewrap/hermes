"""Behaviour contracts for /whisper: encrypted store, ephemeral delivery,
target-only reveal, expiry, and the admin -> DM -> notice fallback chain."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types
from telethon.errors.rpcerrorlist import UserNotParticipantError

from gateway.allowgc import create_group_profile, load_state, resolve_home
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.whisper_store import (
    WHISPER_TTL_SECONDS,
    load_whisper,
    store_whisper,
)

OWNER = "100"
MEMBER = "201"
TARGET = "202"
OTHER = "203"
BOT_USER = "999"
GROUP = "-100123"
SECRET = "meet <at> dawn"


# --- store contracts (real Fernet, temp home) --------------------------------


def test_store_round_trip_and_ciphertext_at_rest(tmp_path):
    home = tmp_path / "gc-123"
    home.mkdir()
    wid = store_whisper(home, sender_id=MEMBER, target_id=TARGET, text=SECRET)

    assert len(wid) == 32 and wid != SECRET
    status, text, target_id = load_whisper(home, wid)
    assert (status, text, target_id) == ("ok", SECRET, TARGET)
    raw = (home / "whispers.json").read_text(encoding="utf-8")
    assert SECRET not in raw and MEMBER in raw and TARGET in raw
    key = home / "whisper_key"
    assert key.exists() and (key.stat().st_mode & 0o777) == 0o600
    assert SECRET not in key.read_text(encoding="utf-8", errors="replace")


def test_store_ttl_is_24h(tmp_path):
    home = tmp_path / "gc-123"
    home.mkdir()
    wid = store_whisper(home, sender_id=MEMBER, target_id=TARGET, text=SECRET)

    entry = json.loads((home / "whispers.json").read_text(encoding="utf-8"))[wid]
    assert entry["expires_at"] - entry["created_at"] == WHISPER_TTL_SECONDS == 24 * 3600


def test_store_expired_load_reports_expired_and_purges(tmp_path):
    home = tmp_path / "gc-123"
    home.mkdir()
    wid = store_whisper(home, sender_id=MEMBER, target_id=TARGET, text=SECRET)
    path = home / "whispers.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[wid]["expires_at"] = data[wid]["created_at"] - 1
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load_whisper(home, wid) == ("expired", None, TARGET)
    assert wid not in json.loads(path.read_text(encoding="utf-8"))


def test_store_missing_returns_missing(tmp_path):
    home = tmp_path / "gc-123"
    home.mkdir()
    assert load_whisper(home, "0" * 32) == ("missing", None, None)


def test_store_key_survives_reload_and_undecryptable_is_expired(tmp_path):
    home = tmp_path / "gc-123"
    home.mkdir()
    wid = store_whisper(home, sender_id=MEMBER, target_id=TARGET, text=SECRET)
    key_before = (home / "whisper_key").read_bytes()

    assert load_whisper(home, wid)[0] == "ok"
    assert (home / "whisper_key").read_bytes() == key_before

    from cryptography.fernet import Fernet

    (home / "whisper_key").write_bytes(Fernet.generate_key())
    assert load_whisper(home, wid) == ("expired", None, TARGET)


# --- adapter harness (mirrors test_allowgc_imout.py) -------------------------


def _source(user_id=OWNER, *, chat_type="group", chat_id=GROUP, is_bot=False):
    return SimpleNamespace(
        platform="telegram",
        chat_id=str(chat_id),
        chat_type=chat_type,
        user_id=str(user_id),
        user_name=f"User {user_id}",
        chat_name=f"Chat {chat_id}",
        thread_id=None,
        is_bot=is_bot,
    )


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n", encoding="utf-8",
    )
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_name = "Naina"
    adapter.send_reaction = AsyncMock(return_value=True)

    def _resolve(src):
        if getattr(src, "chat_type", None) in {"group", "forum"}:
            gc = resolve_home(getattr(src, "chat_id", None), reap=False)
            if gc is not None:
                return gc
        return home

    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=_resolve,
        _profile_name_for_source=lambda _source, adapter_profile=None: adapter_profile,
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter._edit_rich_ui_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._input_peer_for = AsyncMock(
        side_effect=lambda uid: types.InputPeerUser(int(uid), 98765))
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=901))
    adapter._client.get_entity = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(
        side_effect=lambda peer: types.InputUser(int(getattr(peer, "user_id", peer)), 98765))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()

    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message",
                        AsyncMock(return_value={"final_response": "tenant reply"}))
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming",
                        AsyncMock())

    async def _entity(ref):
        key = str(ref).lstrip("@")
        table = {
            "201": SimpleNamespace(id=201, username="user201", bot=False, first_name="One"),
            "202": SimpleNamespace(id=202, username="user202", bot=False, first_name="Two"),
            "203": SimpleNamespace(id=203, username="user203", bot=False, first_name="Three"),
            "user202": SimpleNamespace(id=202, username="user202", bot=False, first_name="Two"),
            "botuser": SimpleNamespace(id=999, username="botuser", bot=True, first_name="Bot"),
            "999": SimpleNamespace(id=999, username="botuser", bot=True, first_name="Bot"),
        }
        if key in table:
            return table[key]
        raise ValueError(f"unknown user {ref!r}")

    adapter._client.get_entity = AsyncMock(side_effect=_entity)

    async def _perms(entity, user=None):
        if user == "me":
            return SimpleNamespace(is_admin=True, has_left=False)
        return SimpleNamespace(is_admin=False, has_left=False)

    adapter._client.get_permissions = AsyncMock(side_effect=_perms)

    store = store_for_home(home)
    return SimpleNamespace(adapter=adapter, store=store, home=home)


def _admit_member(transport, user_id=MEMBER):
    from agent.mongo_tenants import Principal

    gc_home = resolve_home(GROUP, reap=False)
    store_for_home(gc_home).allow_user(Principal(OWNER), user_id, True)


def _ephemeral_message(text, source, *, message_id=77, entities=None, reply_to=None):
    return types.UpdateNewEphemeralMessage(message=types.Message(
        id=message_id,
        peer_id=(types.PeerUser(user_id=int(source.user_id))
                 if source.chat_type == "dm"
                 else types.PeerChat(chat_id=abs(int(source.chat_id)))),
        message=text,
        from_id=types.PeerUser(user_id=int(source.user_id)),
        entities=entities or [],
        reply_to=reply_to,
    ))


def _ephemeral_callback(data, source, *, message_id=901, query_id=5):
    return types.UpdateEphemeralBotCallbackQuery(
        query_id=query_id,
        user_id=int(source.user_id),
        msg_id=message_id,
        data=data.encode(),
        message=types.Message(
            id=message_id,
            peer_id=(types.PeerUser(user_id=int(source.user_id))
                     if source.chat_type == "dm"
                     else types.PeerChat(chat_id=abs(int(source.chat_id)))),
            message="Tap to reveal",
            from_id=types.PeerUser(user_id=999),
        ),
        peer=(types.PeerUser(user_id=int(source.user_id))
              if source.chat_type == "dm"
              else types.PeerChat(chat_id=abs(int(source.chat_id)))),
    )


def _ephemeral_sends(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.ephemeral.SendMessageRequest)]


def _ephemeral_edits(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.ephemeral.EditMessageRequest)]


def _callback_answers(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.messages.SetBotCallbackAnswerRequest)]


def _reveal_data(adapter):
    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    buttons = [(button.text, button.type.data.decode())
               for row in sends[0].reply_markup.rows for button in row.buttons]
    assert len(buttons) == 1 and buttons[0][0] == "Reveal"
    return buttons[0][1]


@pytest.mark.asyncio
async def test_whisper_reply_target_delivers_ephemeral_to_target(transport):
    """A /whisper replying to a member's message delivers an ephemeral
    'Tap to reveal' card to that member only, in the group."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    adapter._client.get_messages = AsyncMock(return_value=SimpleNamespace(
        id=55, from_id=types.PeerUser(user_id=int(TARGET))))

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper {SECRET}", _source(MEMBER), message_id=77,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=55)))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    request = sends[0]
    assert request.receiver_id.user_id == int(TARGET)
    assert request.peer.user_id == int(GROUP)
    assert "Tap to reveal" in request.message
    assert SECRET not in request.message
    data = _reveal_data(adapter)
    assert data.startswith("hm:whisper:") and data.endswith(f":{GROUP}")
    assert SECRET not in data
    raw = (gc_home / "whispers.json").read_text(encoding="utf-8")
    assert SECRET not in raw
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_whisper_username_target_delivers(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    assert sends[0].receiver_id.user_id == int(TARGET)
    assert sends[0].peer.user_id == int(GROUP)


@pytest.mark.asyncio
async def test_whisper_numeric_id_and_mention_name_targets_deliver(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper {TARGET} {SECRET}", _source(MEMBER), message_id=77))
    assert _ephemeral_sends(adapter)[0].receiver_id.user_id == int(TARGET)

    adapter._client.reset_mock()
    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper Two {SECRET}", _source(MEMBER), message_id=78,
        entities=[types.MessageEntityMentionName(offset=9, length=3, user_id=int(TARGET))]))
    assert _ephemeral_sends(adapter)[0].receiver_id.user_id == int(TARGET)


@pytest.mark.asyncio
async def test_whisper_reveal_by_target_edits_to_secret_and_answers_silently(transport):
    """Only the target's tap reveals: the same ephemeral message is edited
    into the secret (HTML-escaped) and the callback answered silently."""
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    adapter._client.get_messages = AsyncMock(return_value=SimpleNamespace(
        id=55, from_id=types.PeerUser(user_id=int(TARGET))))
    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper {SECRET}", _source(MEMBER), message_id=77,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=55)))
    data = _reveal_data(adapter)

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(data, _source(TARGET)))

    edits = _ephemeral_edits(adapter)
    assert len(edits) == 1
    assert edits[0].id == 901
    assert edits[0].receiver_id.user_id == int(TARGET)
    assert edits[0].message == "meet &lt;at&gt; dawn"
    assert "<at>" not in edits[0].message
    answers = _callback_answers(adapter)
    assert len(answers) == 1
    assert answers[0].query_id == 5 and answers[0].cache_time == 0
    assert not answers[0].message and not answers[0].alert


@pytest.mark.asyncio
async def test_whisper_tap_by_other_user_reveals_nothing(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))
    data = _reveal_data(adapter)

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(data, _source(OTHER)))

    assert _ephemeral_edits(adapter) == []
    answers = _callback_answers(adapter)
    assert len(answers) == 1 and not answers[0].message and not answers[0].alert


@pytest.mark.asyncio
async def test_whisper_expired_tap_shows_expired(transport):
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))
    data = _reveal_data(adapter)
    path = gc_home / "whispers.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    for entry in records.values():
        entry["expires_at"] = entry["created_at"] - 1
    path.write_text(json.dumps(records), encoding="utf-8")

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(data, _source(TARGET)))

    edits = _ephemeral_edits(adapter)
    assert len(edits) == 1 and edits[0].message == "(expired)"
    assert SECRET not in edits[0].message


@pytest.mark.asyncio
async def test_whisper_non_admin_falls_back_to_target_dm(transport):
    """Without admin rights in the group, the whisper goes to the target's
    DM as an ephemeral message instead."""
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    async def _perms(entity, user=None):
        if user == "me":
            return SimpleNamespace(is_admin=False, has_left=False)
        return SimpleNamespace(is_admin=False, has_left=False)

    adapter._client.get_permissions = AsyncMock(side_effect=_perms)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    assert sends[0].receiver_id.user_id == int(TARGET)
    assert sends[0].peer.user_id == int(TARGET)


@pytest.mark.asyncio
async def test_whisper_undeliverable_dm_notifies_sender_only(transport):
    """When neither the group card nor the target DM can be delivered, only
    the sender is told (ephemerally), and the stored secret is dropped."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    async def _perms(entity, user=None):
        if user == "me":
            return SimpleNamespace(is_admin=False, has_left=False)
        return SimpleNamespace(is_admin=False, has_left=False)

    adapter._client.get_permissions = AsyncMock(side_effect=_perms)

    async def _fail_target(request):
        if (isinstance(request, functions.ephemeral.SendMessageRequest)
                and request.receiver_id.user_id == int(TARGET)):
            raise RuntimeError("RPCError 400: BOT_NOT_ADMIN")
        return SimpleNamespace(id=901)

    adapter._client.side_effect = _fail_target

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    sends = _ephemeral_sends(adapter)
    # The DM attempt to the target is recorded (and fails), then the notice
    # to the sender is the send that lands.
    assert len(sends) == 2
    assert sends[0].receiver_id.user_id == int(TARGET)
    assert sends[1].receiver_id.user_id == int(MEMBER)
    assert sends[1].peer.user_id == int(GROUP)
    assert SECRET not in sends[1].message
    assert "couldn't deliver" in sends[1].message
    assert not (gc_home / "whispers.json").exists() or \
        json.loads((gc_home / "whispers.json").read_text(encoding="utf-8")) == {}


@pytest.mark.asyncio
async def test_whisper_non_member_target_notifies_sender_only(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    async def _perms(entity, user=None):
        if user == "me":
            return SimpleNamespace(is_admin=True, has_left=False)
        raise UserNotParticipantError(None)

    adapter._client.get_permissions = AsyncMock(side_effect=_perms)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    assert sends[0].receiver_id.user_id == int(MEMBER)
    assert SECRET not in sends[0].message
    assert "couldn't deliver" in sends[0].message


@pytest.mark.asyncio
async def test_whisper_in_dm_answers_groups_only(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(MEMBER, chat_type="dm", chat_id=MEMBER),
        message_id=77))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    assert sends[0].receiver_id.user_id == int(MEMBER)
    assert sends[0].peer.user_id == int(MEMBER)
    assert "groups" in sends[0].message


@pytest.mark.asyncio
async def test_whisper_ignores_non_admitted_sender_and_bot_target(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @user202 {SECRET}", _source(OTHER), message_id=80,
        entities=[types.MessageEntityMention(offset=9, length=8)]))
    await adapter._on_ephemeral_message(_ephemeral_message(
        f"/whisper @botuser {SECRET}", _source(MEMBER), message_id=81,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    assert _ephemeral_sends(adapter) == []
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_whisper_without_text_notifies_sender(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(_ephemeral_message(
        "/whisper @user202", _source(MEMBER), message_id=77,
        entities=[types.MessageEntityMention(offset=9, length=8)]))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    assert sends[0].receiver_id.user_id == int(MEMBER)
    assert SECRET not in sends[0].message
