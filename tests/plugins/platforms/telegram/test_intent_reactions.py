"""Behavior contracts for Telegram's conservative intent reaction chooser."""

import pytest
from plugins.platforms.telegram.intent_reactions import (
    TELEGRAM_STANDARD_REACTION_EMOJI,
    choose_reaction,
    explicit_reaction_request,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Any suggestions?", "👀"),
        ("What do you recommend we try?", "👀"),
        ("We should consider a smaller change", "👀"),
        ("It worked", "🎉"),
        ("The fix finally worked!", "🎉"),
        ("Got it working", "🎉"),
        ("Thanks for your help", "🙏"),
        ("I really appreciate it", "🙏"),
        ("haha that was hilarious", "🤣"),
        ("Exactly, you're right", "👍"),
        ("Does this support exports?", "🤔"),
        ("Why is that happening", "🤔"),
        ("It is broken", "😢"),
        ("The test failed, this is frustrating", "😢"),
        ("I'm sorry to hear about your loss", "😢"),
        ("Thanks but still not working", "😢"),
        ("It worked yesterday but now broken", "😢"),
        ("It did not work", "😢"),
        ("It didn’t work", "😢"),
        ("Not fixed yet", "😢"),
        ("It has not worked", "😢"),
        ("It never worked", "😢"),
        ("not thanks", None),
        ("I do not appreciate the delay", None),
        ("I lost my job, thanks", "😢"),
        ("Can you explain why it worked?", "🤔"),
        ("Can you explain why it worked", "🤔"),
        ("I lost my job, any suggestions?", "😢"),
        ("It did not work, what do you recommend?", "😢"),
        ("Not it worked", None),
        ("I suggest adding a bug report button", "👀"),
        ("no errors, it worked", "🎉"),
        ("the bug is fixed, it worked", "🎉"),
        ("I don't agree that this works", None),
        ("This could be better", None),
        ("Hello there", None),
        ("", None),
        ("   \n", None),
        ("/help", None),
        ("/suggest an idea", None),
        ("> It worked\nThanks for explaining", "🙏"),
        ("`it worked`", None),
        ("```text\nit worked\n```", None),
        ("> Any suggestions?", None),
        ('Did it work?', "🤔"),
        ("It worked yesterday but it's not working now", "😢"),
        ('"It worked" is what they said', None),
    ],
)
def test_choose_reaction(text, expected):
    result = choose_reaction(text)
    assert result == expected
    assert result is None or result in TELEGRAM_STANDARD_REACTION_EMOJI


@pytest.mark.parametrize(
    ("text", "reply_text", "expected"),
    [
        ("Yes", "Did it work after the fix?", "🎉"),
        ("Yes", "Did it work?", "🎉"),
        ("Yes", "Try this fix", "👍"),
        ("Yep", "Try this fix", "👍"),
        ("It worked", "Does this work?", "🎉"),
        ("All good", "Can you confirm the test?", "🎉"),
        ("Yep", "The app is broken", "😢"),
        ("Yes", "Just checking in", None),
        ("It worked", "", "🎉"),
        ("Thanks but still not working", "Did it work?", "😢"),
    ],
)
def test_reply_context_is_narrow_and_never_overrides_current_intent(text, reply_text, expected):
    assert choose_reaction(text, reply_text=reply_text) == expected


def test_non_string_input_fails_closed():
    assert choose_reaction(None) is None
    assert choose_reaction("yes", reply_text=None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("React to this message 🔥", "🔥"),
        ("Please react to this message ❤️.", "❤️"),
        ("react to this message 👀", "👀"),
        ("React to this message 🎯", None),
        ("React to this message", "👍"),
        ("React to this message 🔥!", "🔥"),
        ("React to this message 🔥 and explain why", None),
        ("", None),
    ],
)
def test_explicit_reaction_request_is_exact_and_standard_only(text, expected):
    assert explicit_reaction_request(text) == expected
