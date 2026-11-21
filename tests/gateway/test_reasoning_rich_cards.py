"""Behaviour contracts for the gateway /reasoning rich-message rewrite.

Every /reasoning variant renders as a native Telegram RichMessage card when
the delivery adapter supports it: Effort/Scope/Display values are nested
in-body ``<tg-button>`` callback pills (never backticked code pills), the
Usage line carries the ONLY copy pill, and no response carries an emoji.
Other platforms (and adapters without a card method) keep the plain-text
reply.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2B00-\u2BFF\u26A0\u2713\u2714\u274C\uFE0F]"
)

_ALL_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def _make_event(text="/reasoning", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


class _CardAdapter:
    """Adapter with the Telegram card method; records every card sent."""

    def __init__(self):
        self.cards = []

    async def send_command_card(self, *, source, html, links=(), copies=(), buttons=(), reply_to=None):
        self.cards.append({
            "source": source, "html": html, "links": links,
            "copies": copies, "buttons": buttons, "reply_to": reply_to,
        })
        return True


class _NoCardAdapter:
    """Adapter with no card method (plain-text fallback path)."""


class _PickerAdapter:
    """Adapter whose *type* exposes ``send_choice_picker`` (the gate the
    handler checks via ``getattr(type(adapter), 'send_choice_picker', None)``)."""

    def __init__(self, success=True):
        self.calls = []
        self._success = success

    async def send_choice_picker(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=self._success, message_id="m1")


def _make_runner(adapter=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    return runner


def _home_with_config(tmp_path, monkeypatch, body):
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    return home


def _assert_no_emoji(text):
    assert not _EMOJI_RE.search(text), f"emoji found in: {text!r}"


class TestBareReasoningCard:
    @pytest.mark.asyncio
    async def test_bare_reasoning_sends_rich_card_on_telegram(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert result is None  # card sent — runner sends nothing else
        assert len(adapter.cards) == 1
        html = adapter.cards[0]["html"]
        # Effort / Scope / Display render as nested callback buttons, not pills.
        assert html.count("callback_data") == 3
        assert "hm:reasoning:pick" in html
        assert "hm:reasoning:scope" in html
        assert "hm:reasoning:toggle-display" in html
        assert ">medium<" in html  # current effort button label
        # The Usage line carries the ONLY copy pill in the response.
        assert html.count("copy_text") == 1
        assert "/reasoning &lt;none|minimal" in html
        # No backticked code pills anywhere in the card.
        assert "`" not in html
        assert "<code>" not in html
        _assert_no_emoji(html)

    @pytest.mark.asyncio
    async def test_bare_reasoning_plain_fallback_without_card_adapter(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        runner = _make_runner(_NoCardAdapter())

        result = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert result is not None
        assert "*Usage:*" in result  # the italic Usage fix survives
        _assert_no_emoji(result)

    @pytest.mark.asyncio
    async def test_bare_reasoning_plain_on_non_telegram(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(
            _make_event("/reasoning", platform=Platform.DISCORD))

        assert result is not None  # no card off Telegram
        assert adapter.cards == []
        _assert_no_emoji(result)


class TestTypedReasoningCards:
    @pytest.mark.asyncio
    async def test_reasoning_level_set_renders_effort_and_scope_buttons(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)
        event = _make_event("/reasoning high")
        session_key = runner._session_key_for_source(event.source)

        result = await runner._handle_reasoning_command(event)

        assert result is None
        assert len(adapter.cards) == 1
        html = adapter.cards[0]["html"]
        assert "hm:reasoning:pick" in html
        assert ">high<" in html
        assert "hm:reasoning:scope" in html
        assert "session override" in html
        # Confirmations carry no Usage line and no copy pill at all.
        assert "copy_text" not in html
        assert "Usage" not in html
        _assert_no_emoji(html)
        assert runner._session_reasoning_overrides[session_key] == {
            "enabled": True, "effort": "high",
        }

    @pytest.mark.asyncio
    async def test_reasoning_level_set_global_renders_global_scope(self, tmp_path, monkeypatch):
        home = _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(_make_event("/reasoning high --global"))

        assert result is None
        html = adapter.cards[0]["html"]
        assert "hm:reasoning:pick" in html
        assert ">high<" in html
        assert "global config" in html
        assert "Saved to config" in html
        assert "copy_text" not in html
        _assert_no_emoji(html)
        assert "reasoning_effort: high" in (home / "config.yaml").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arg,state", [("show", "on"), ("hide", "off")])
    async def test_reasoning_display_toggle_renders_display_button(
        self, tmp_path, monkeypatch, arg, state
    ):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(_make_event(f"/reasoning {arg}"))

        assert result is None
        html = adapter.cards[0]["html"]
        assert "hm:reasoning:toggle-display" in html
        assert f">{state}<" in html
        assert "telegram" in html
        assert "copy_text" not in html
        _assert_no_emoji(html)
        assert runner._show_reasoning is (arg == "show")

    @pytest.mark.asyncio
    async def test_reasoning_reset_renders_scope_button_and_clears_override(
        self, tmp_path, monkeypatch
    ):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)
        event = _make_event("/reasoning reset")
        session_key = runner._session_key_for_source(event.source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

        result = await runner._handle_reasoning_command(event)

        assert result is None
        html = adapter.cards[0]["html"]
        assert "hm:reasoning:scope" in html
        assert "global config" in html
        assert "copy_text" not in html
        _assert_no_emoji(html)
        assert session_key not in runner._session_reasoning_overrides

    @pytest.mark.asyncio
    async def test_reasoning_reset_global_unsupported_has_usage_copy_only(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(_make_event("/reasoning reset --global"))

        assert result is None
        html = adapter.cards[0]["html"]
        assert html.count("copy_text") == 1  # Usage line only
        assert "/reasoning &lt;level&gt; --global" in html
        _assert_no_emoji(html)

    @pytest.mark.asyncio
    async def test_reasoning_unknown_arg_lists_levels_current_first(self, tmp_path, monkeypatch):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _CardAdapter()
        runner = _make_runner(adapter)

        result = await runner._handle_reasoning_command(_make_event("/reasoning bogus"))

        assert result is None
        html = adapter.cards[0]["html"]
        # Every valid level is listed; the current one comes first, marked.
        for level in _ALL_LEVELS:
            assert level in html
        assert html.index("<b>medium</b> (current)") < html.index(">none<")
        # Italic page indexation for the list, single page.
        assert "<i>Page 1 of 1 \u2022 total 8</i>" in html
        # The Usage line carries the ONLY copy pill in the response.
        assert html.count("copy_text") == 1
        assert "copy_text" in html and "/reasoning &lt;none|minimal" in html
        assert "`" not in html
        _assert_no_emoji(html)


class TestReasoningPlainRepliesStayEmojiFree:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("arg", ["high", "show", "hide", "reset", "bogus", "ultra --global"])
    async def test_typed_plain_replies_have_no_emoji(self, tmp_path, monkeypatch, arg):
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        runner = _make_runner(_NoCardAdapter())

        result = await runner._handle_reasoning_command(_make_event(f"/reasoning {arg}"))

        assert result is not None
        _assert_no_emoji(result)


class TestReasoningApplierContract:
    def test_apply_reasoning_selection_stays_sync_plain_text(self, tmp_path, monkeypatch):
        """The single application path keeps returning plain text: the picker,
        /model --reasoning and /msettings reasoning callers all rely on it."""
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        runner = _make_runner(_NoCardAdapter())
        event = _make_event("/reasoning high")
        session_key = runner._session_key_for_source(event.source)

        reply = runner._apply_reasoning_selection(session_key, "telegram", "high")

        assert isinstance(reply, str)
        assert "high" in reply
        _assert_no_emoji(reply)

    @pytest.mark.asyncio
    async def test_picker_selection_still_returns_plain_text(self, tmp_path, monkeypatch):
        """Picker taps keep the plain-text reply (pickers only exist on
        adapters without a card method)."""
        _home_with_config(
            tmp_path, monkeypatch,
            "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
        )
        adapter = _PickerAdapter()
        runner = _make_runner(adapter)
        event = _make_event("/reasoning")
        session_key = runner._session_key_for_source(event.source)

        await runner._handle_reasoning_command(event)
        on_choice = adapter.calls[0]["on_choice_selected"]

        reply = await on_choice(event.source.chat_id, "ultra")

        assert isinstance(reply, str)
        assert "ultra" in reply
        _assert_no_emoji(reply)
        assert runner._session_reasoning_overrides[session_key] == {
            "enabled": True, "effort": "ultra",
        }
