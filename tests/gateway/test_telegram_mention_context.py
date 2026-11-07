"""Multi-bot addressing must survive routing into the agent's event without destabilising the
session prompt.

Ported from upstream's PTB-shaped test to the fork's Telethon-only adapter. The helpers build
Telethon ``Message``-shaped fakes (text lives in ``message``, replies in a ``reply_to`` header,
entities are real ``telethon.tl.types`` objects) and drive the real
``TelethonAdapter._process_inbound_event`` — the fork has no ``_handle_text_message``,
``_handle_media_message`` or ``_handle_command``. Seams the test does not exercise are stubbed
on the adapter (authorization, tenant routing, attachment intake, control UIs), following the
pattern in ``tests/plugins/platforms/telegram/test_telethon_adapter.py``.

Upstream behaviour, ported to the Telethon adapter (each verified against the
live adapter):
- The event text has the sole-addressee mention stripped (``@bot 2`` keeps
  resolving) and a command's ``@bot`` suffix cleaned out of the text.
  Routing still uses the clean name: ``gateway/platforms/event.py`` derives
  ``new`` from ``/new@hermes_bot`` for the command name, and the adapter
  validates the recipient before routing.
- The event channel prompt merges ``config.extra["channel_prompts"]`` (per-chat
  prompt first, then the identity block in groups); in observe mode the
  observed-context marker is appended and the event source is anonymised
  (commands keep the original source).
- In DMs the channel prompt is the resolved per-chat prompt as-is — empty
  when none is configured.
- There is no ``mention_patterns``/wake-word adapter config in the fork (the nearest equivalent
  is the per-chat trigger word set via ``/trigger``), so the wake-word trigger is not covered.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import (
    MessageEntityBotCommand,
    MessageEntityCode,
    MessageEntityMention,
    MessageEntityMentionName,
)

from gateway.config import PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.telegram import button_demo, incoming_reactions
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture(autouse=True)
def _restore_telegram_env():
    """The YAML→env bridge tests write TELEGRAM_* into os.environ; an explicit env value now
    beats ``config.extra`` for the owning profile, so a leaked bridge value would silently override
    the ``extra`` the adapter tests construct with."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}
    yield
    for k in [k for k in os.environ if k.startswith("TELEGRAM_")]:
        if k not in saved:
            del os.environ[k]
    os.environ.update(saved)


def _make_adapter(
    monkeypatch,
    tmp_path,
    require_mention=None,
    free_response_chats=None,
    free_response_topics=None,
    exclusive_bot_mentions=None,
    ignored_threads=None,
    allowed_topics=None,
    allow_from=None,
    group_allow_from=None,
    allowed_chats=None,
    group_allowed_chats=None,
    guest_mode=None,
    observe_unmentioned_group_messages=None,
    bots_require_mention=None,
    bot_username="hermes_bot",
):
    extra = {}
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if free_response_chats is not None:
        extra["free_response_chats"] = free_response_chats
    if free_response_topics is not None:
        extra["free_response_topics"] = free_response_topics
    if exclusive_bot_mentions is not None:
        # The fork gates on the adapter's own username, not on this flag; it
        # stays in ``extra`` so the scenario reads as upstream wrote it.
        extra["exclusive_bot_mentions"] = exclusive_bot_mentions
    if ignored_threads is not None:
        extra["ignored_threads"] = ignored_threads
    if allowed_topics is not None:
        extra["allowed_topics"] = allowed_topics
    else:
        # Keep unit tests isolated from TELEGRAM_ALLOWED_TOPICS in the parent
        # environment; production adapters without this explicit key still fall
        # back to the env var.
        extra["allowed_topics"] = []
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    else:
        # Keep unit tests isolated from TELEGRAM_ALLOWED_CHATS in the parent
        # environment; production adapters without this explicit key still fall
        # back to the env var.
        extra["allowed_chats"] = []
    if group_allowed_chats is not None:
        extra["group_allowed_chats"] = group_allowed_chats
    else:
        extra["group_allowed_chats"] = []
    if guest_mode is not None:
        extra["guest_mode"] = guest_mode
    if observe_unmentioned_group_messages is not None:
        extra["observe_unmentioned_group_messages"] = observe_unmentioned_group_messages
    if bots_require_mention is not None:
        extra["bots_require_mention"] = bots_require_mention

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="***", extra=extra))
    adapter._bot_id = 999
    adapter._bot_username = bot_username
    adapter._bot_name = None
    # Reply-to-own resolution needs a client handle to attempt; the reply itself is
    # answered by the message's ``get_reply_message``, so no network is touched.
    adapter._client = SimpleNamespace()
    # Trigger-gating tests don't exercise the allowlist gate. Force-authorize all
    # senders so the trigger logic under test runs.
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: None)
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda _source, _message: None)
    monkeypatch.setattr(adapter, "_prepare_attachment_intake", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_cache_inbound_media", AsyncMock(return_value=([], [], "")))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_command_menu", AsyncMock())
    noop_ui = SimpleNamespace(
        handle_message=AsyncMock(return_value=False),
        _home=lambda _source: str(tmp_path),
    )
    for ui_getter in (
        "_setup_ui", "_model_settings_ui", "_provider_ui", "_mcp_ui",
        "_plugin_ui", "_settings_ui", "_skills_ui",
    ):
        monkeypatch.setattr(adapter, ui_getter, lambda _noop=noop_ui: _noop)
    monkeypatch.setattr(button_demo, "handle_reply_keyboard_text", AsyncMock(return_value=False))
    monkeypatch.setattr(button_demo, "clear_reply_keyboard_for_command", AsyncMock())
    monkeypatch.setattr(
        incoming_reactions, "handle_explicit_reaction_request", AsyncMock(return_value=None))
    monkeypatch.setattr(incoming_reactions, "react_to_incoming", AsyncMock())
    events = []
    monkeypatch.setattr(adapter, "handle_message", AsyncMock(side_effect=events.append))
    return adapter, events


def _group_message(
    text="hello",
    *,
    chat_id=-100,
    from_user_id=111,
    reply_to_bot=False,
    entities=None,
):
    reply_to = None
    get_reply_message = None
    if reply_to_bot:
        reply_to = SimpleNamespace(reply_to_msg_id=10, reply_to_top_id=None, forum_topic=False)
        bot_reply = SimpleNamespace(
            id=10, message="previous bot reply", sender_id=999,
            get_sender=AsyncMock(return_value=SimpleNamespace(username="hermes_bot")),
        )
        get_reply_message = AsyncMock(return_value=bot_reply)
    return SimpleNamespace(
        id=42,
        message=text,
        entities=entities or [],
        reply_to=reply_to,
        reply_to_msg_id=10 if reply_to_bot else None,
        sender_id=from_user_id,
        chat_id=chat_id,
        grouped_id=None,
        action=None,
        photo=None,
        video=None,
        audio=None,
        sticker=None,
        document=None,
        voice=None,
        get_reply_message=get_reply_message or AsyncMock(return_value=None),
    )


def _dm_message(text="hello", *, from_user_id=111):
    return SimpleNamespace(
        id=43,
        message=text,
        entities=[],
        reply_to=None,
        reply_to_msg_id=None,
        sender_id=from_user_id,
        chat_id=from_user_id,
        grouped_id=None,
        action=None,
        photo=None,
        video=None,
        audio=None,
        sticker=None,
        document=None,
        voice=None,
        get_reply_message=AsyncMock(return_value=None),
    )


def _mention_entity(text, mention="@hermes_bot"):
    offset = text.index(mention)
    return MessageEntityMention(offset=offset, length=len(mention))


def _mention_entities(text, mentions):
    return [_mention_entity(text, mention) for mention in mentions]


def _group_voice_message(*, chat_id=-100, from_user_id=111, caption=None, entities=None):
    message = _group_message(
        caption, chat_id=chat_id, from_user_id=from_user_id, entities=entities,
    )
    message.id = 51
    message.voice = SimpleNamespace()
    return message


def _group_event(message, *, from_user_name="Alice Example"):
    first, _, last = from_user_name.partition(" ")
    chat = SimpleNamespace(
        id=message.chat_id, megagroup=True, gigagroup=False, broadcast=False,
        forum=False, title="Test Group", first_name=None,
    )
    sender = SimpleNamespace(
        id=message.sender_id, first_name=first, last_name=last or None,
        username=None, bot=False,
    )
    return SimpleNamespace(
        message=message, chat_id=message.chat_id, sender_id=message.sender_id,
        chat=chat, sender=sender,
        get_sender=AsyncMock(return_value=sender),
        get_chat=AsyncMock(return_value=chat),
    )


def _dm_event(message, *, from_user_name="Alice Example"):
    first, _, last = from_user_name.partition(" ")
    chat = SimpleNamespace(
        id=message.chat_id, megagroup=False, gigagroup=False, broadcast=False,
        forum=False, title=None, first_name=first,
    )
    sender = SimpleNamespace(
        id=message.sender_id, first_name=first, last_name=last or None,
        username=None, bot=False,
    )
    return SimpleNamespace(
        message=message, chat_id=message.chat_id, sender_id=message.sender_id,
        chat=chat, sender=sender,
        get_sender=AsyncMock(return_value=sender),
        get_chat=AsyncMock(return_value=chat),
    )


def _prompt_signature(event):
    return GatewayRunner._agent_config_signature(
        "model", {"api_key": "k", "base_url": "u", "provider": "p"}, ["messaging"], event.channel_prompt or "",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("observe", [False, True])
async def test_multi_bot_addressing_survives_real_handlers(monkeypatch, tmp_path, media, observe):
    text = "@research_bot , @ops_bot are you both listening?"
    for username in ("research_bot", "ops_bot", "unrelated_bot"):
        adapter, events = _make_adapter(
            monkeypatch, tmp_path, bot_username=username, require_mention=True,
            exclusive_bot_mentions=True, observe_unmentioned_group_messages=observe,
            allowed_chats=["-100"], group_allowed_chats=["-100"],
        )
        adapter.config.extra["channel_prompts"] = {"-100": "Keep answers concise."}
        entities = _mention_entities(text, ["@research_bot", "@ops_bot"])
        if media:
            msg = _group_voice_message(caption=text, entities=entities)
        else:
            msg = _group_message(text, entities=entities)
        await adapter._process_inbound_event(_group_event(msg), [msg])

        if username == "unrelated_bot":
            assert not events
            continue
        assert len(events) == 1
        event = events[0]
        assert text in event.text  # Preserve both recipients and their positions.
        assert f"@{username}" in event.channel_prompt
        # Per-chat channel prompts merge onto the event (upstream parity).
        assert "Keep answers concise." in (event.channel_prompt or "")
        # The observed-context marker appears on the event prompt when observe
        # mode is on (upstream parity).
        assert ("observed Telegram group context" in (event.channel_prompt or "")) == observe
        # Observe mode anonymises the event source (upstream parity).
        assert event.source.user_id == (None if observe else "111")


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["mention", "text_mention", "reply", "open", "code", "command", "dm"])
async def test_sole_addressee_text_stays_clean_and_prompt_is_session_stable(monkeypatch, tmp_path, trigger):
    """The inbound text reaches the event verbatim (the fork never strips the sole-addressee
    mention, so ``@bot 2`` keeps resolving with the mention intact), and the identity block is
    identical across turns: it rides the cached-agent signature, so a per-message fact there
    would rebuild the agent every turn."""
    adapter, events = _make_adapter(monkeypatch, tmp_path, require_mention=trigger != "open")
    if trigger == "dm":
        msg = _dm_message("hello")
        event = _dm_event(msg)
    elif trigger == "command":
        msg = _group_message(
            "/new@hermes_bot", entities=[MessageEntityBotCommand(offset=0, length=15)])
        event = _group_event(msg)
    elif trigger == "text_mention":
        msg = _group_message(
            "Hermes hello",
            entities=[MessageEntityMentionName(offset=0, length=6, user_id=999)],
        )
        event = _group_event(msg)
    elif trigger == "mention":
        text = "😀 @hermes_bot 2"
        msg = _group_message(text, entities=[MessageEntityMention(offset=3, length=11)])
        event = _group_event(msg)
    elif trigger == "code":
        # Telegram says this is code, not a mention; a reply admits the turn.
        msg = _group_message(
            "@hermes_bot", reply_to_bot=True,
            entities=[MessageEntityCode(offset=0, length=11)],
        )
        event = _group_event(msg)
    else:
        msg = _group_message("hello", reply_to_bot=trigger == "reply")
        event = _group_event(msg)
    await adapter._process_inbound_event(event, [msg])
    # Second turn in the same chat with a different addressing shape (reply, no entities).
    follow_up = _dm_message("thanks") if trigger == "dm" else _group_message("thanks", reply_to_bot=True)
    follow_event = _dm_event(follow_up) if trigger == "dm" else _group_event(follow_up)
    await adapter._process_inbound_event(follow_event, [follow_up])

    assert len(events) == 2
    first, second = events
    # Upstream: the sole-addressee mention is stripped from the event text,
    # and a command's ``@bot`` suffix is cleaned out of the text too.
    expected_text = {"command": "/new", "mention": "😀 2", "code": "@hermes_bot"}.get(trigger, msg.message)
    assert first.text == expected_text
    if trigger == "dm":
        assert not first.channel_prompt
    else:
        assert "@hermes_bot" in first.channel_prompt
    assert first.channel_prompt == second.channel_prompt
    assert _prompt_signature(first) == _prompt_signature(second)
    assert first.source.user_id == "111"
