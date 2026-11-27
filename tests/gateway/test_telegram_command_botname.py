"""``/command@ourbot args`` from Telegram's group command menu reaches the gateway as ``/command args``.

Driven through the Telethon adapter's real inbound path (``_process_inbound_event`` ->
``group_trigger_text`` -> ``MessageEvent``), not the deleted PTB adapter's ``_handle_command``:
the foreign-``@bot`` veto is the adapter's text regex, the trigger gate is ``_group_triggered``,
and the mention strip is ``telegram_addressing.clean_bot_trigger_text`` via
``telegram_event_context.group_trigger_text``.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telegram_addressing import clean_bot_trigger_text
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

BOT = "hermes_bot"


class MessageEntityMention:
    """Stand-in for Telethon's entity: the kind is read from the class name."""

    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


class MessageEntityBotCommand:
    """Stand-in for Telethon's bot-command entity."""

    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


class MessageEntityCode:
    """Stand-in for Telethon's code entity (a span that addresses no one)."""

    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


def _telegram_entities(text):
    """Mention / bot-command spans Telegram would attach, as Telethon stand-ins.

    Offsets are UTF-16 code units, as on the wire.
    """
    entities = []
    for match in re.finditer(r"/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]{1,32})?|@[A-Za-z0-9_]{1,32}", text):
        kind = MessageEntityBotCommand if match.group().startswith("/") else MessageEntityMention
        entities.append(kind(
            len(text[:match.start()].encode("utf-16-le")) // 2,
            len(match.group().encode("utf-16-le")) // 2,
        ))
    return entities


async def _drive(text, *, bot_username=BOT, entities="auto"):
    """Feed one group message through the real adapter; return dispatched events."""
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True, "allowed_chats": ["-100"]}))
    adapter._bot_username = bot_username
    adapter._bot_id = 999
    adapter._bot_name = "Hermes"
    source = SimpleNamespace(chat_id="-100", user_id="111", user_name="Alice",
                             chat_type="group", thread_id=None, is_bot=False)
    message = SimpleNamespace(
        message=text, id=42, reply_to_msg_id=None,
        entities=_telegram_entities(text) if entities == "auto" else entities,
    )
    event = SimpleNamespace(message=message, source=source)
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._multiuser_home = lambda _source: None
    adapter._observe_telegram_message = lambda _source, _message: None
    adapter._cache_inbound_peer = AsyncMock()
    adapter._refresh_command_menu = AsyncMock()
    adapter._group_triggered = AsyncMock(return_value=True)
    seen = []
    adapter.handle_message = AsyncMock(side_effect=lambda dispatched: seen.append(dispatched))
    await adapter._process_inbound_event(event, [message])
    return seen


async def _dispatched_command(text, **kwargs):
    seen = await _drive(text, **kwargs)
    assert len(seen) == 1, "the addressed command was not dispatched"
    return seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, command, args", [
    ("/reasoning@hermes_bot medium", "reasoning", "medium"),
    ("/reasoning@hermes_bot: high --global", "reasoning", "high --global"),
    ("/memory@hermes_bot approve abc123", "memory", "approve abc123"),
    ("/new@hermes_bot", "new", ""),
    ("@hermes_bot /model@hermes_bot gpt-5", "model", "gpt-5"),
    ("@hermes_bot, /model gpt-5", "model", "gpt-5"),
    ("/btw did @hermes_bot already answer Alice?", "btw", "did @hermes_bot already answer Alice?"),
    ("/btw@hermes_bot did @hermes_bot already answer?", "btw", "did @hermes_bot already answer?"),
    ("/btw 👋 did @hermes_bot already answer?", "btw", "👋 did @hermes_bot already answer?"),
    ("/model @hermes_bot gpt-5", "model", "gpt-5"),
    ("/model gpt-5 @hermes_bot", "model", "gpt-5"),
    ("/model gpt-5 @hermes_bot @hermes_bot", "model", "gpt-5"),
    ("/reasoning high @hermes_bot --global", "reasoning", "high --global"),
    ("/reasoning high @hermes_bot @hermes_bot --global", "reasoning", "high --global"),
    ("/btw did @hermes_bot — our helper — already answer?", "btw", "did @hermes_bot — our helper — already answer?"),
    ("/x a @hermes_bot --level 3", "x", "a --level 3"),
    ("/x a @hermes_bot -v", "x", "a -v"),
    ("/x a @hermes_bot —global", "x", "a —global"),
    ("/x a @hermes_bot –global", "x", "a –global"),
    ("/reload-mcp@hermes_bot x", "reload-mcp", "x"),
])
async def test_command_menu_form_keeps_its_arguments(text, command, args):
    event = await _dispatched_command(text)
    assert event.get_command() == command
    assert event.text == f"/{command} {args}".rstrip()
    assert event.get_command_args() == args.replace("—", "--").replace("–", "-")


@pytest.mark.asyncio
async def test_prose_mention_cleanup_is_unchanged():
    assert [e.text for e in await _drive("@hermes_bot, summarize this")] == ["summarize this"]
    assert [e.text for e in await _drive("thanks @hermes_bot")] == ["thanks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected", [
    ("@hermes_bot /new@hermes_bot/log.txt", "/new/log.txt"),
    ("/new@hermes_bot/log.txt", "/new /log.txt"),
    ("@hermes_bot /new@alice/log.txt is missing", "/new@alice/log.txt is missing"),
    ("/file.txt is missing", "/file.txt is missing"),
    ("/file-name.txt mentions @hermes_bot here", "/file-name.txt mentions here"),
    ("@hermes_bot , @ops_bot are you both listening?", "@hermes_bot , @ops_bot are you both listening?"),
    ("@hermes_bot", "@hermes_bot"),
    ("@hermes_bot, summarize this", "summarize this"),
    ("thanks @hermes_bot", "thanks"),
    ("/new", "/new"),
    ("@hermes_bot /new", "/new"),
    ("@file: /model@hermes_bot gpt-5", "@file: /model@hermes_bot gpt-5"),
    ("/set-home @hermes_bot", "/set-home"),
    ("/codex-runtime", "/codex-runtime"),
    ("/reload-mcp x", "/reload-mcp x"),
    ("@alice /model@OtherBot failed; @hermes_bot can you explain?",
     "@alice /model@OtherBot failed; @hermes_bot can you explain?"),
    ("/model@hermes_bot gpt-5 @OtherBot", "/model@hermes_bot gpt-5 @OtherBot"),
    ("@hermes_bot /btw ask @alice", "@hermes_bot /btw ask @alice"),
])
async def test_other_inputs_keep_parent_behavior(text, expected):
    assert [e.text for e in await _drive(text)] == [expected]


@pytest.mark.asyncio
async def test_full_length_bot_username_keeps_parent_behavior():
    username = "a" * 32
    events = await _drive(f"/model@{username} gpt-5", bot_username=username)
    assert [e.text for e in events] == ["/model gpt-5"]


@pytest.mark.asyncio
async def test_path_without_entities_keeps_parent_behavior():
    text = "@hermes_bot /new@alice/log.txt is missing"
    assert [e.text for e in await _drive(text, entities=[])] == ["/new@alice/log.txt is missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "/model@OtherBot gpt-5 @hermes_bot",
    "/status@OtherBot now",
])
async def test_foreign_command_is_dropped_before_the_trigger_gate(text):
    """A leading command addressed to another bot never reaches the gate,
    even when our own handle appears later in the message."""
    assert await _drive(text) == []


@pytest.mark.asyncio
async def test_code_span_foreign_command_is_still_dropped():
    """The Telethon veto reads raw text, not entities: a ``/cmd@OtherBot`` in a
    code span is still dropped. (The PTB-era test expected dispatch here; the
    live adapter has no entity-aware veto.)"""
    text = "/model@OtherBot gpt-5 @hermes_bot"
    entities = [MessageEntityCode(e.offset, e.length) if isinstance(e, MessageEntityBotCommand) else e
                for e in _telegram_entities(text)]
    assert await _drive(text, entities=entities) == []


def test_mid_text_command_suffix_keeps_its_separator():
    """A ``/cmd@ourbot`` that is not the leading command is prose, not a trigger:
    its handle is cleaned but the separator before its arguments is kept, so the
    command never glues to its arguments (``/model@Bot gpt-5`` -> ``/model gpt-5``)."""
    assert clean_bot_trigger_text("see /model@hermes_bot gpt-5", BOT) == "see /model gpt-5"
    assert clean_bot_trigger_text("@file: /model@hermes_bot gpt-5", BOT) == "@file: /model gpt-5"


@pytest.mark.asyncio
async def test_mid_text_command_suffix_is_not_glued_to_its_arguments():
    assert [e.text for e in await _drive("see /model@hermes_bot gpt-5")] == ["see /model gpt-5"]
