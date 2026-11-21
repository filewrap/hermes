"""A text-less ``rich_message`` group message is addressed exactly like the same text sent as plain text (#151730):
every addressing check (leading handles, the foreign-command target, the wake-word view and the
other-participant guard on handle stripping) reads the same text, so a message meant for another bot stays theirs.

The PTB-era gate (``_should_process_message``) and ``group_reply_expected`` are gone with the PTB
adapter; the checks now live in ``telegram_entities.addressing_sources`` /
``telegram_addressing`` (pure layer) and in the Telethon adapter's foreign-command veto plus
``_group_triggered`` (live gate). ``exclusive_bot_mentions`` is a retired option and no longer vetoes.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telegram_addressing import foreign_command_target, leading_bot_handles
from plugins.platforms.telegram.telegram_entities import (
    addressing_sources, addressing_text, rich_message_text,
)
from plugins.platforms.telegram.telegram_event_context import group_trigger_text
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

BOT = "hermes_bot"


class MessageEntityMention:
    """Stand-in for Telethon's entity: the kind is read from the class name."""

    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


def _rich(*paragraphs):
    """A text-less message whose content lives in ``rich_message`` blocks."""
    blocks = [p if isinstance(p, dict) else {"type": "paragraph", "text": p} for p in paragraphs]
    return SimpleNamespace(text=None, caption=None, entities=[], caption_entities=[],
                           api_kwargs={"rich_message": {"blocks": blocks}})


def _plain(text, *spans):
    """A plain-text message; spans are ``(entity type, substring)`` in the addressing layer's
    ``.type``-tagged entity protocol (the shape ``addressing_sources`` reads)."""
    return SimpleNamespace(
        text=text, caption=None, entities=[
            SimpleNamespace(type=kind, offset=text.index(span), length=len(span)) for kind, span in spans
        ], caption_entities=[])


def _telethon_message(text):
    """The same text as a Telethon-shaped message: mention spans are
    ``MessageEntityMention`` objects, which is what ``group_trigger_text``'s
    other-participant guard reads."""
    entities = [MessageEntityMention(match.start(), len(match.group())) for match in
                re.finditer(r"@[A-Za-z0-9_]{2,31}\b", text)]
    return SimpleNamespace(message=text, entities=entities)


def _trigger_text(message):
    """Trigger text for a plain or rich message, through the live ``group_trigger_text``."""
    text = addressing_text(message)
    return group_trigger_text(text, "group", [_telethon_message(text)], BOT, 999)


_LINK = ["see ", {"type": "url", "text": "docs", "url": "https://chompy.example.com/docs"}]
_BOTH = "@hermes_bot , @ops_bot are you both listening?"


def _leading_handles(message):
    return leading_bot_handles(message, lambda handle: handle.endswith("bot"))


@pytest.mark.parametrize("probe, plain, rich, expected", [
    (_leading_handles, _plain("@other_bot please deploy now", ("mention", "@other_bot")),
     _rich("@other_bot please deploy now", "p2"), {"other_bot"}),
    (_leading_handles, _plain("/status@other_bot now", ("bot_command", "/status@other_bot")),
     _rich("/status@other_bot now", "second paragraph"), {"other_bot"}),
    (_leading_handles, _plain("@hermes_bot deploy", ("mention", "@hermes_bot")),
     _rich("@hermes_bot deploy", "p2"), {"hermes_bot"}),
    (_leading_handles, _plain("/status@hermes_bot", ("bot_command", "/status@hermes_bot")),
     _rich("/status@hermes_bot", "p2"), {"hermes_bot"}),
    (_leading_handles, _plain(_BOTH, ("mention", "@hermes_bot"), ("mention", "@ops_bot")),
     _rich(_BOTH), {"hermes_bot", "ops_bot"}),
    # Leading code is content, as in plain text: the later foreign handle is not the addressee.
    (_leading_handles, _plain("x @other_bot please explain", ("code", "x"), ("mention", "@other_bot")),
     _rich([{"type": "code", "text": "x"}, " @other_bot please explain"]), set()),
    (_leading_handles, _plain("ls\n@other_bot run it", ("pre", "ls"), ("mention", "@other_bot")),
     _rich({"type": "pre", "text": "ls"}, "@other_bot run it"), set()),
    (lambda message: foreign_command_target(message, BOT),
     _plain("/status@other_bot now", ("bot_command", "/status@other_bot")),
     _rich("/status@other_bot now", "second paragraph"), "other_bot"),
    (lambda message: foreign_command_target(message, BOT),
     _plain("/status@hermes_bot", ("bot_command", "/status@hermes_bot")),
     _rich("/status@hermes_bot", "p2"), None),
    (lambda message: foreign_command_target(message, BOT),
     _plain("@other_bot please deploy now", ("mention", "@other_bot")),
     _rich("@other_bot please deploy now", "p2"), None),
    (_trigger_text, _plain(_BOTH, ("mention", "@hermes_bot"), ("mention", "@ops_bot")),
     _rich(_BOTH), _BOTH),
    (_trigger_text, _plain("@hermes_bot deploy", ("mention", "@hermes_bot")),
     _rich("@hermes_bot deploy"), "deploy"),
])
def test_rich_only_group_message_is_addressed_like_plain_text(probe, plain, rich, expected):
    assert probe(plain) == expected
    assert probe(rich) == expected


def test_rich_linked_foreign_command_does_not_override_our_mention():
    """A rich hyperlink label is text_link, not Telegram's bot_command entity (#151730)."""
    linked = "/status@other_bot"
    plain = _plain(
        f"{linked} @hermes_bot",
        ("text_link", linked),
        ("mention", "@hermes_bot"),
    )
    plain.entities[0].url = "https://example.com/status"
    rich = _rich([{"type": "url", "text": linked, "url": "https://example.com/status"}, " @hermes_bot"])

    for message in (plain, rich):
        # The user deliberately mentions us; a clickable command-shaped label cannot veto that.
        assert foreign_command_target(message, BOT) is None
        sources = addressing_sources(message)
        assert not any(entity.type == "bot_command"
                       for _text, entities in sources for entity in entities)
        # The mention span is read against its own source text: the rich source
        # masks the linked label with a placeholder, so its offsets differ.
        assert any(entity.type == "mention" and
                   text[entity.offset:entity.offset + entity.length] == "@hermes_bot"
                   for text, entities in sources for entity in entities)


def test_hidden_link_url_is_not_a_wake_word():
    """A link to ``chompy.example.com`` is not the wake word ``chompy``: the addressing view
    leaves hidden URLs out, while the agent-visible text still inlines them."""
    plain = _plain("see docs", ("text_link", "docs"))
    plain.entities[0].url = "https://chompy.example.com/docs"
    rich = _rich(_LINK)

    assert addressing_text(plain) == "see docs"
    assert addressing_text(rich) == "see docs"
    assert "chompy" not in addressing_text(plain)
    assert "chompy" not in addressing_text(rich)
    assert rich_message_text(rich) == "see docs (https://chompy.example.com/docs)"


async def _drive(text, *, require_mention=True, rich=False, tmp_path=None):
    """Feed one group message through the real adapter gate; return dispatched event texts.

    The message carries the plain-text fallback in ``.message``; with ``rich=True`` it also
    carries a native rich payload, as an MTProto rich message does.
    """
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    adapter = TelethonAdapter(PlatformConfig(extra={
        "require_mention": require_mention, "allowed_chats": ["-100"]}))
    adapter._bot_username = BOT
    adapter._bot_id = 999
    adapter._bot_name = "Hermes"
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path)
    source = SimpleNamespace(chat_id="-100", user_id="111", user_name="Alice",
                             chat_type="group", thread_id=None, is_bot=False)
    message = SimpleNamespace(message=text, id=42, reply_to_msg_id=None, entities=[])
    if rich:
        message.rich_message = input_rich_message(text)
    event = SimpleNamespace(message=message, source=source)
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._multiuser_home = lambda _source: None
    adapter._observe_telegram_message = lambda _source, _message: None
    adapter._cache_inbound_peer = AsyncMock()
    adapter._refresh_command_menu = AsyncMock()
    seen = []
    adapter.handle_message = AsyncMock(side_effect=lambda dispatched: seen.append(dispatched))
    await adapter._process_inbound_event(event, [message])
    return [dispatched.text for dispatched in seen]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected", [
    ("@other_bot please deploy now", []),
    ("/status@other_bot now", []),
    ("@hermes_bot deploy", ["deploy"]),
    ("/status@hermes_bot", ["/status"]),
    ("anyone around?", []),
])
async def test_group_gate_reads_the_plain_text_of_a_rich_message(text, expected, tmp_path):
    """The rich payload changes nothing about addressing: the gate reads the plain text."""
    assert await _drive(text, rich=False, tmp_path=tmp_path) == expected
    assert await _drive(text, rich=True, tmp_path=tmp_path) == expected


@pytest.mark.asyncio
async def test_foreign_led_message_is_processed_when_mention_is_not_required(tmp_path):
    """``exclusive_bot_mentions`` is retired: with ``require_mention`` off, a message that
    merely opens with another bot's handle is no longer vetoed for that reason alone."""
    assert await _drive("@other_bot please deploy now", require_mention=False,
                        tmp_path=tmp_path) == ["@other_bot please deploy now"]
