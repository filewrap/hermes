"""The single Message Reactions setting drives one conditional system-prompt line.

ON (or unset, the default) puts the line in the turn's prompt; OFF leaves it
out entirely, so the model is never told it may react.
"""
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource


def _prompt(personal_settings):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="123")
    ctx = SimpleNamespace(
        source=source, context_prompt="", channel_prompt="",
        personal_settings=personal_settings, message="hi",
    )
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = SimpleNamespace(_get_system_prompt_for_channel=lambda *args, **kwargs: "")
    turn._ctx = ctx
    turn._search_task_requested = False
    turn._deep_research_requested = False
    return turn._combined_ephemeral_prompt()


def test_reactions_line_present_when_setting_on():
    prompt = _prompt({"reactions_enabled": True})
    assert "Message reactions are on" in prompt
    assert "telegram_client action='reaction'" in prompt


def test_reactions_line_present_by_default():
    # A loaded settings dict without the key (the default is ON) still gets the line.
    prompt = _prompt({"memory_enabled": True})
    assert "Message reactions are on" in prompt


def test_reactions_line_absent_when_setting_off():
    prompt = _prompt({"reactions_enabled": False})
    assert "Message reactions are on" not in prompt
    assert "telegram_client action='reaction'" not in prompt
