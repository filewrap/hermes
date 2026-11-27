"""Stripping a command's ``@bot`` suffix must not eat the space before its arguments.

Regression: ``/reasoning@Emiliabot off`` reached the model as ``/reasoningoff`` —
the mention stripper removed the separator between the command and its args.
Driven through the live path (``group_trigger_text``, the event-text build in
the Telethon adapter), not the command dispatcher, which parses args separately.
"""

from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.telegram_event_context import group_trigger_text

BOT = "Emiliabot"


class MessageEntityMention:
    """Stand-in for Telethon's entity: the kind is read from the class name."""

    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


def _trigger(text, *, chat_type="group", messages=None, username=BOT, bot_id=123):
    return group_trigger_text(text, chat_type, messages or [], username, bot_id)


@pytest.mark.parametrize("text, expected", [
    ("/reasoning@Emiliabot off", "/reasoning off"),
    ("/reasoning@Emiliabot", "/reasoning"),
    ("/reasoning@Emiliabot, off", "/reasoning off"),
    ("@Emiliabot hello", "hello"),
    ("thanks @Emiliabot", "thanks"),
    ("/reasoning@OtherBot off", "/reasoning@OtherBot off"),
    ("@Emiliabot", "@Emiliabot"),
    ("@Emiliabot, summarize this", "summarize this"),
    ("/reasoning off", "/reasoning off"),
])
def test_command_mention_strip_keeps_the_argument_separator(text, expected):
    assert _trigger(text) == expected


def test_mention_strip_in_dm():
    assert _trigger("@Emiliabot hello", chat_type="private") == "hello"


def test_no_username_leaves_text_untouched():
    assert _trigger("/reasoning@Emiliabot off", username=None) == "/reasoning@Emiliabot off"


def test_username_with_at_prefix_still_strips():
    assert _trigger("/reasoning@Emiliabot off", username="@Emiliabot") == "/reasoning off"


def test_other_participants_named_leaves_text_untouched():
    text = "@other_bot, what do you think?"
    messages = [SimpleNamespace(
        message=text, entities=[MessageEntityMention(0, len("@other_bot"))])]
    assert _trigger(text, messages=messages) == text
