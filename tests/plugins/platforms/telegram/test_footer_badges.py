"""Footer-badge taps answer their detail toast — for anyone, with no guard.

The badges sit on a message anyone in the chat can already see, so the tap
carries no owner, user, group or admission check: click and you get the
answer.

The badges themselves are RICH buttons, not keyboard buttons: they ride the
final message as bare ``<tg-button>`` elements nested INLINE inside the
footer paragraph — the same small in-body form the settings patches use.
A ``<tg-button-row>`` block renders as big keyboard-sized buttons; a bare
``<tg-button>`` nested in paragraph text renders small, like the patches.
The footer itself carries them: ``gateway.runtime_footer.build_footer_html``
renders the footer paragraph with the badges in it, and the turn converts
the Markdown body to HTML first, so the message that reaches the adapter
is already complete Rich HTML. The adapter sends it verbatim; there is no
badge metadata, no hand-over, and no PageBlockButtonRow anywhere in the path.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from telethon import types as _tl

from gateway.config import PlatformConfig
from plugins.platforms.telegram.callback_answer import answer_footer_badge_callback
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


_BADGE_BUTTON_RE = re.compile(
    r'<tg-button[^>]*data="([^"]*)"[^>]*>([^<]*)</tg-button>')


def _html_badge_buttons(request):
    """The footer badge (label, data) pairs in an HTML final, or None."""
    html = getattr(getattr(request, "rich_message", None), "html", None) or ""
    if not _BADGE_BUTTON_RE.search(html):
        return None
    return [(label, data) for data, label in _BADGE_BUTTON_RE.findall(html)]


class _Event:
    """Minimal Telethon callback-query event: records every answer."""

    def __init__(self, data, sender_id=1, chat_id=1, message_id=101):
        self.data = data
        self.message_id = message_id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.sender = SimpleNamespace(id=sender_id, first_name="Mallory", bot=False)
        self.chat = SimpleNamespace(id=chat_id, first_name="Mallory")
        self.message = SimpleNamespace(id=101, chat_id=chat_id, reply_to=None)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def _adapter():
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="t"))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(id=777)

    adapter._client = Client()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("data,toast", [
    ("fb:m:agentrouter|nousresearch/hermes-4-405b",
     "nousresearch/hermes-4-405b · agentrouter"),
    ("fb:m:|hermes-4-405b", "hermes-4-405b"),
    ("fb:c:38400|256000", "38,400 tokens of 256,000 tokens"),
    ("fb:s:nousresearch/hermes-4-405b|hermes-4-405b-prod",
     "requested nousresearch/hermes-4-405b, served as hermes-4-405b-prod"),
    ("fb:l:22s", "turn took 22s"),
    ("fb:p:/data/home/.hermes", "/data/home/.hermes"),
])
async def test_footer_badge_answers_its_detail(data, toast):
    event = _Event(data)
    assert await answer_footer_badge_callback(event, data) is True
    assert event.answers == [((toast,), {})]


@pytest.mark.asyncio
async def test_footer_badge_cwd_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    event = _Event("fb:p:~/projects/hermes")
    assert await answer_footer_badge_callback(event, "fb:p:~/projects/hermes") is True
    assert event.answers == [((str(tmp_path / "projects" / "hermes"),), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "fb:x:whatever", "fb:c:not-a-number|256000", "fb:m:", "badge:pending", "",
])
async def test_footer_badge_unknown_or_malformed_answers_empty(data):
    event = _Event(data)
    assert await answer_footer_badge_callback(event, data) is True
    assert event.answers == [((), {})]


@pytest.mark.asyncio
async def test_footer_badge_tap_dispatched_through_callback_query():
    adapter = _adapter()
    event = _Event("fb:m:agentrouter|nousresearch/hermes-4-405b")
    await adapter._on_callback_query(event)
    assert event.answers == [(("nousresearch/hermes-4-405b · agentrouter",), {})]


def _unauthorized_group_stubs(monkeypatch, adapter):
    """Multiuser on; the tapper is a group member the bot never admitted."""
    import gateway.mongo_multiuser as multiuser
    monkeypatch.setattr(multiuser, "enabled", lambda home: True)

    async def _group_source(_event):
        return SimpleNamespace(user_id=999, chat_type="group", chat_id=-100123,
                               is_bot=False, thread_id=None)

    monkeypatch.setattr(adapter, "_source", _group_source)
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: "/tmp/home")


@pytest.mark.asyncio
async def test_footer_badge_tap_by_unauthorized_group_member_still_answers(monkeypatch):
    """No owner/user/group/admission guard: a never-admitted group member
    tapping a badge on a message they can see still gets the answer."""
    adapter = _adapter()
    _unauthorized_group_stubs(monkeypatch, adapter)
    event = _Event("fb:m:agentrouter|nousresearch/hermes-4-405b",
                   sender_id=999, chat_id=-100123)
    await adapter._on_callback_query(event)
    assert event.answers == [(("nousresearch/hermes-4-405b · agentrouter",), {})]


@pytest.mark.asyncio
async def test_gate_still_refuses_other_callbacks_for_the_same_user(monkeypatch):
    """The bypass is fb:-only: the same unauthorized member tapping a
    session control still gets the refusal alert."""
    adapter = _adapter()
    _unauthorized_group_stubs(monkeypatch, adapter)
    event = _Event("ms:model", sender_id=999, chat_id=-100123)
    await adapter._on_callback_query(event)
    assert event.answers == [(("This control is not authorized for your account or chat scope.",),
                              {"alert": True})]


def _footer_html():
    """What the turn sends: the body as HTML, then the footer HTML (line +
    badge row). Built the way _hmwa_attach_footer builds it."""
    from gateway.runtime_footer import build_footer_html

    body_html = "<p>Hey! \U0001f44b</p><p>Some <b>bold</b> text.</p>"
    footer = build_footer_html(
        "gpt-6-luna · 14% · ~",
        [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
         ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")])
    return f"{body_html}\n\n{footer}"


@pytest.mark.asyncio
async def test_send_carries_the_footer_badges_verbatim():
    """The adapter sends the footer HTML verbatim: the badges ride as bare
    <tg-button> pills nested inline in the footer paragraph — the small
    form the settings patches use — never as reply_markup, never as a
    <tg-button-row> block, and never as a PageBlockButtonRow."""
    adapter = _adapter()
    result = await adapter.send("42", _footer_html())
    assert result.success is True
    assert len(adapter._client.requests) == 1
    request = adapter._client.requests[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert request.reply_markup is None, "footer badges must not ride a keyboard"
    assert type(request.rich_message).__name__ == "InputRichMessageHTML", (
        "badges must ride as literal <tg-button> pills, not a PageBlockButtonRow")
    assert "<tg-button-row" not in request.rich_message.html
    assert _html_badge_buttons(request) == [
        ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
        ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    assert 'style="primary"' in request.rich_message.html
    assert "<b>bold</b>" in request.rich_message.html


@pytest.mark.asyncio
async def test_send_without_badges_attaches_no_keyboard():
    adapter = _adapter()
    result = await adapter.send("42", "The answer.")
    assert result.success is True
    assert len(adapter._client.requests) == 1
    assert adapter._client.requests[0].reply_markup is None
    assert _html_badge_buttons(adapter._client.requests[0]) is None
    assert "<tg-button-row" not in (
        getattr(adapter._client.requests[0].rich_message, "html", None) or "")


class _SettingsAdapter:
    """Minimal adapter for the settings card: records rich requests."""

    def __init__(self):
        self._client = object()
        self.edits = []
        self._next_message_id = 100

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


@pytest.mark.asyncio
async def test_settings_toggle_flips_footer_badges_through_the_card(tmp_path):
    """The Footer badges row on the settings card is a live toggle: tapping
    it flips the persisted setting — on, then off again on a fresh card."""
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("platforms:\n  telegram: {}\n", encoding="utf-8")
    adapter = _SettingsAdapter()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: home,
    )
    ui = TelegramPersonalSettingsUI(adapter)
    source = SimpleNamespace(chat_id="123", user_id="123", user_name="Test User",
                             thread_id=None, chat_type="dm", profile=None)
    state = ui._new_state(source, home, message_id=55)
    state["user_name"] = source.user_name
    assert TelegramPersonalSettingsUI._footer_badges_enabled(str(home)) is False
    data = f"ps:{state['id']}:footer-badges-toggle"
    assert await ui.handle_callback(_Event(data, message_id=55), source, data)
    assert TelegramPersonalSettingsUI._footer_badges_enabled(str(home)) is True
    # The card rotates its callback state on every tap, so the off-tap goes
    # through a fresh card.
    state = ui._new_state(source, home, message_id=56)
    state["user_name"] = source.user_name
    data = f"ps:{state['id']}:footer-badges-toggle"
    assert await ui.handle_callback(_Event(data, message_id=56), source, data)
    assert TelegramPersonalSettingsUI._footer_badges_enabled(str(home)) is False


@pytest.mark.asyncio
async def test_badged_markdown_reply_keeps_its_formatting():
    """The turn converts the Markdown body to HTML and appends the footer
    HTML: bold, list and code keep their formatting, and no literal
    Markdown survives in the payload."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = ("Hey! \U0001f44b\n\nSome **bold** text and a list:\n\n- one\n- two\n\n"
            "```python\nprint('hi')\n```")
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    result = await adapter.send("42", str(response))
    assert result.success is True
    request = adapter._client.requests[0]
    assert request.reply_markup is None
    assert type(request.rich_message).__name__ == "InputRichMessageHTML", (
        "badges must ride as literal <tg-button> pills, not a PageBlockButtonRow")
    assert _html_badge_buttons(request) == [
        ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
        ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    html = request.rich_message.html
    assert "<b>bold</b>" in html and "<ul>" in html and "<pre" in html
    assert "**" not in html and "```" not in html


@pytest.mark.asyncio
async def test_badged_reply_with_table_and_math_converts_to_html():
    """A pipe table and display math convert to <table> and <tg-math-block>;
    the badges still ride as <tg-button> pills."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = ("The sum is $$x^2 + y^2$$\n\n| A | B |\n|---|---|\n| 1 | 2 |")
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    result = await adapter.send("42", str(response))
    assert result.success is True
    request = adapter._client.requests[0]
    assert request.reply_markup is None
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    html = request.rich_message.html
    assert "<tg-math-block>" in html and "<table>" in html
    assert "$$" not in html and "| A | B |" not in html
    assert _html_badge_buttons(request) == [
        ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
        ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]


@pytest.mark.asyncio
async def test_badged_reply_with_copyable_code_keeps_a_copy_button():
    """A short inline code span keeps its copy action as an HTML
    <tg-button type="copy_text">, alongside the badge row."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = "Run `/stop` now."
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    result = await adapter.send("42", str(response))
    assert result.success is True
    request = adapter._client.requests[0]
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    html = request.rich_message.html
    assert '<tg-button type="copy_text" text="/stop">/stop</tg-button>' in html
    assert _html_badge_buttons(request) == [
        ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
        ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]


@pytest.mark.asyncio
async def test_badged_reply_with_details_converts_the_markdown_around_it():
    """A <details> collapsible converts to HTML and the Markdown around it
    converts too — no literal Markdown survives in the payload."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = ("# Head\n\n<details>\n<summary>More</summary>\n\nHidden **text**.\n\n"
            "</details>")
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    result = await adapter.send("42", str(response))
    assert result.success is True
    request = adapter._client.requests[0]
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    html = request.rich_message.html
    assert "<h1>Head</h1>" in html
    assert "<details><summary>More</summary><p>Hidden <b>text</b>.</p></details>" in html
    assert "# Head" not in html and "**" not in html
    assert _html_badge_buttons(request) == [
        ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
        ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]


@pytest.mark.asyncio
async def test_badged_reply_with_media_falls_back_to_plain_line():
    """A media block cannot be expressed in HTML: the footer line still
    rides on the reply text, and the badges are dropped — the footer is
    never lost to a row the server cannot render."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = "Look:\n\n![pic](tg://photo?id=123)"
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    assert "<tg-button-row" not in str(response)
    assert "gpt-6-luna · 14% · ~" in str(response)
    result = await adapter.send("42", str(response))
    assert result.success is True
    request = adapter._client.requests[0]
    assert request.reply_markup is None
    assert _html_badge_buttons(request) is None


@pytest.mark.asyncio
async def test_badged_reply_with_checklist_falls_back_to_plain_line():
    """A checklist item has no HTML form: the same plain-line fallback."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _adapter()
    mixin = GatewayTurnPersistenceMixin()
    text = "- [ ] todo one\n- [x] todo two"
    badges = [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
              ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")]
    response = mixin._hmwa_attach_footer(
        SimpleNamespace(), {}, text, "gpt-6-luna · 14% · ~", badges, False)
    assert "<tg-button-row" not in str(response)
    assert "gpt-6-luna · 14% · ~" in str(response)
    result = await adapter.send("42", str(response))
    assert result.success is True
    assert _html_badge_buttons(adapter._client.requests[0]) is None


@pytest.mark.asyncio
async def test_failed_send_does_not_retry(caplog):
    """No keyboard, no keyboard retry: a refused send fails once."""
    adapter = _adapter()

    class FailingClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            raise RuntimeError("boom")

    adapter._client = FailingClient()
    result = await adapter.send("42", _footer_html())
    assert result.success is False
    assert len(adapter._client.requests) == 1
