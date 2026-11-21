"""Final-message Reasoning section vs streaming response: the shown-once matrix.

SHOW the Reasoning section in the final message IF /reasoning show +
streaming OFF. DON'T SHOW it IF show + streaming ON, or off + streaming
ON. (off + OFF -> don't show.) The final message must not contain
reasoning if streaming is enabled, because it was already shown in the
draft — reasoning is shown ONCE, never repeated in the final.
"""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _streaming_adapter(enabled):
    """A delivery adapter whose streaming-response setting is ``enabled``."""
    adapter = SimpleNamespace(
        config=SimpleNamespace(extra={"streaming_response": enabled}))
    adapter._truthy_setting = TelethonAdapter._truthy_setting
    return adapter


def _prepend(monkeypatch, *, show, streaming, platform=Platform.TELEGRAM,
             adapter=None):
    """The final message ``_hmwa_prepend_reasoning`` builds for one turn."""
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run, "_load_gateway_config",
        lambda: {"display": {"show_reasoning": show}})
    runner = GatewayTurnPersistenceMixin()
    runner._delivery_adapter_for = lambda source: adapter
    return runner._hmwa_prepend_reasoning(
        {"last_reasoning": "weigh it"}, "final answer",
        SimpleNamespace(platform=platform), False)


@pytest.mark.parametrize(
    ("show", "streaming", "attaches"),
    [
        (True, False, True),    # show + streaming OFF -> show in final
        (True, True, False),    # show + streaming ON -> already in the draft
        (False, True, False),   # off + streaming ON -> nowhere
        (False, False, False),  # off + streaming OFF -> nowhere
    ],
)
def test_reasoning_final_matrix(monkeypatch, show, streaming, attaches):
    reply = _prepend(monkeypatch, show=show, streaming=streaming,
                     adapter=_streaming_adapter(streaming))
    assert ("weigh it" in reply) is attaches
    assert reply.endswith("final answer")


def test_reasoning_final_attaches_when_streaming_is_telegram_only(monkeypatch):
    """Streaming response is a Telegram draft feature: another platform
    with the setting on still gets its Reasoning section in the final."""
    reply = _prepend(monkeypatch, show=True, streaming=True,
                     platform=Platform.DISCORD, adapter=_streaming_adapter(True))
    assert "weigh it" in reply
    assert reply.endswith("final answer")


def test_reasoning_final_attaches_when_no_delivery_adapter(monkeypatch):
    """An unresolvable delivery adapter fails open to the old behaviour:
    show + no adapter -> the Reasoning section attaches."""
    reply = _prepend(monkeypatch, show=True, streaming=False, adapter=None)
    assert "weigh it" in reply
    assert reply.endswith("final answer")


def test_reasoning_final_keeps_code_block_rendering(monkeypatch):
    """The final-message Reasoning section stays a code block — the
    draft-only italic never changes how the final renders."""
    reply = _prepend(monkeypatch, show=True, streaming=False,
                     adapter=_streaming_adapter(False))
    assert "```" in reply
    assert "weigh it" in reply
