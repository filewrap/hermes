"""Tests for reply-to pointer injection in _prepare_inbound_message_text.

The `[Replying to: "..."]` prefix is a *disambiguation pointer*, not
deduplication. It must always be injected when the user explicitly replies
to a prior message — even when the quoted text already exists somewhere
in the conversation history. History can contain the same or similar text
multiple times, and without an explicit pointer the agent has to guess
which prior message the user is referencing.
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


@pytest.mark.asyncio
async def test_reply_prefix_injected_when_text_absent_from_history():
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text="Japan is great for culture, food, and efficiency.",
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[{"role": "user", "content": "unrelated"}],
    )

    assert result is not None
    assert result.startswith(
        '[Replying to: "Japan is great for culture, food, and efficiency."]'
    )
    assert result.endswith("What's the best time to go?")


@pytest.mark.asyncio
async def test_telegram_long_reply_reaches_prompt_without_losing_later_items():
    """The native reply already has the full message; preparation must not trim it.

    Builds the MessageEvent directly: the upstream helper module
    (tests.gateway.test_telegram_reply_quote) never landed in this tree, and
    the merged Telethon adapter has no _build_message_event helper — it builds
    the event inline from the fetched reply message, with the same fields
    (reply_to_message_id / reply_to_text) used here.
    """
    from gateway.platforms.event import MessageType

    quoted = "\n".join(
        f"{index}. {company}: " + "Evidence from the supplied list. " * 12
        for index, company in enumerate(
            ["GoCar", "Urban Drive", "DubCar", "GRPS", "Halucar"], 1
        )
    )
    event = MessageEvent(
        text="Review all five companies.",
        message_type=MessageType.TEXT,
        source=_source(),
        reply_to_message_id="42",
        reply_to_text=quoted,
    )
    history = [{"role": "user", "content": "Previous request"}]
    result = await _make_runner()._prepare_inbound_message_text(
        event=event, source=event.source, history=history,
    )
    assert result is not None
    assert quoted in result
    assert result.endswith("Review all five companies.")
    assert history == [{"role": "user", "content": "Previous request"}]


@pytest.mark.asyncio
async def test_quoted_reply_references_stay_literal_while_typed_ones_expand(tmp_path, monkeypatch):
    """The replied-to author's ``@file:`` is quoted text, not the replier's request: no local read.
    The same reference typed in the new message still expands (positive control)."""
    import threading

    payload = tmp_path / "notes.txt"
    payload.write_text("LOCAL-FILE-MARKER", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    runner = _make_runner()
    runner._session_model_overrides, runner._last_resolved_model = {}, {}
    runner._agent_cache, runner._agent_cache_lock = {}, threading.Lock()
    runner._resolve_session_agent_runtime = lambda **kw: ("openai/gpt-4.1-mini", {"base_url": None, "api_key": ""})
    source = _source()

    quoted = ("x " * 300) + f"\nsee @file:{payload.name} for details"
    quoted_ref = MessageEvent(text="what does this say?", source=source, reply_to_message_id="7", reply_to_text=quoted)
    result = await runner._prepare_inbound_message_text(event=quoted_ref, source=source, history=[])
    assert quoted in result
    assert "LOCAL-FILE-MARKER" not in result

    typed_ref = MessageEvent(text=f"read @file:{payload.name}", source=source, reply_to_message_id="7", reply_to_text="short")
    result = await runner._prepare_inbound_message_text(event=typed_ref, source=source, history=[])
    assert result.startswith('[Replying to: "short"]')
    assert "LOCAL-FILE-MARKER" in result


@pytest.mark.asyncio
async def test_reply_prefix_still_injected_when_text_in_history():
    """Regression test: the pointer must survive even when the quoted text
    already appears in history. Previously a `found_in_history` guard
    silently dropped the prefix, leaving the agent to guess which prior
    message the user was referencing."""
    runner = _make_runner()
    source = _source()
    quoted = "Japan is great for culture, food, and efficiency."
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    history = [
        {"role": "user", "content": "I'm thinking of going to Japan or Italy."},
        {
            "role": "assistant",
            "content": (
                f"{quoted} Italy is better if you prefer a relaxed pace."
            ),
        },
        {"role": "user", "content": "How long should I stay?"},
        {"role": "assistant", "content": "For Japan, 10-14 days is ideal."},
    ]

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=history,
    )

    assert result is not None
    assert result.startswith(f'[Replying to: "{quoted}"]')
    assert result.endswith("What's the best time to go?")




@pytest.mark.asyncio
async def test_native_quote_excerpt_is_preferred_over_full_replied_text():
    """A Telegram quote-reply highlights an excerpt; the agent sees the excerpt, not the whole message."""
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="Explain this part.",
        source=source,
        reply_to_message_id="42",
        reply_to_text="First sentence. The quoted part is here. Last sentence.",
        quoted_text="The quoted part is here.",
        reply_to_is_own_message=True,
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    assert result.startswith('[Replying to your previous message, quoting: "The quoted part is here."]')
    assert "First sentence." not in result
    assert result.endswith("Explain this part.")


@pytest.mark.asyncio
async def test_native_quote_survives_without_fetched_reply_text():
    """The excerpt travels in the reply header, so it is shown even when the original message is gone."""
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="What about this?",
        source=source,
        reply_to_message_id="42",
        quoted_text="The quoted part is here.",
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    assert result.startswith('[Replying to, quoting: "The quoted part is here."]')
    assert result.endswith("What about this?")


@pytest.mark.asyncio
async def test_quote_without_a_reply_target_adds_no_pointer():
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="What about this?",
        source=source,
        quoted_text="The quoted part is here.",
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result == "What about this?"
