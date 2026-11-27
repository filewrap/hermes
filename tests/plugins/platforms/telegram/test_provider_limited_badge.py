"""A rate-limited provider carries its "limited" state in its own row button.

On the main /providers list the row is one button — name, credential
status and the limited state with its wait — so the badge is visibly tied
to its provider; tapping the row opens the provider AND answers a toast
with the fresh details (when the limit was hit, when it lifts), computed
on tap. The standalone provider page keeps the small red "limited" badge
right after the provider name; tapping it answers the same fresh toast.
"""

import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.callback_answer import answer_badge_callback
from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from plugins.platforms.telegram.provider_management_ui_text import (
    limited_provider_toast,
    provider_cooldown_state,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: Platform = Platform.TELEGRAM
    message_id: str = "50"
    profile: str | None = None


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.requests = []
        self.plain_sends = []
        self.deleted = []
        self.delete_result = True
        self.gateway_runner = None

    def _peer_for(self, chat_id):
        return chat_id

    @staticmethod
    def _sent_message_id(response):
        return str(getattr(response, "id", "")) or None

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=55)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), str(message_id)))
        return self.delete_result

    async def send(self, *args, **kwargs):
        self.plain_sends.append((args, kwargs))
        return SimpleNamespace(success=True)


def _provider(**updates):
    row = {
        "slug": "openrouter",
        "name": "OpenRouter",
        "description": "Route requests through the OpenRouter API.",
        "auth_type": "api_key",
        "api_key_env_vars": ["OPENROUTER_API_KEY"],
        "base_url_env_var": "OPENROUTER_BASE_URL",
        "signup_url": "https://openrouter.ai/keys",
        "is_catalog_provider": True,
        "configured": True,
        "is_current": True,
        "is_profile_default": True,
        "profile_default_model": "vendor/model-a",
        "models": ["vendor/model-a", "vendor/model-b"],
        "total_models": 2,
        "default_model": "vendor/model-a",
        "default_reasoning": "medium",
    }
    row.update(updates)
    return row


def _entry(**updates):
    row = {
        "last_status": "ok",
        "last_status_at": None,
        "last_error_code": None,
        "last_error_reason": None,
        "last_error_message": None,
        "last_error_reset_at": None,
        "failure_reason": None,
    }
    row.update(updates)
    return SimpleNamespace(**row)


class _FakePool:
    def __init__(self, entries):
        self._entries = list(entries)

    def entries(self):
        return list(self._entries)


def _limited_state(now, *, reset_in=3600, hit_ago=3700):
    return {"reset_at": now + reset_in, "hit_at": now - hit_ago}


def _keyboard_buttons(markup):
    return [(button.text, button.type.data.decode("utf-8"))
            for row in markup.rows for button in row.buttons]


def _walk_tl_objects(value):
    from telethon import types

    if isinstance(value, types.TLObject):
        yield value
        for child in vars(value).values():
            if isinstance(child, (list, tuple)):
                for nested in child:
                    yield from _walk_tl_objects(nested)
            elif isinstance(child, types.TLObject):
                yield from _walk_tl_objects(child)


def _plain_rich_text(value):
    if isinstance(value, str):
        return value
    for name in ("texts", "items", "blocks"):
        children = getattr(value, name, None)
        if children is not None:
            separator = "" if name == "texts" else "\n"
            return separator.join(_plain_rich_text(child) for child in children)
    if hasattr(value, "text"):
        text = _plain_rich_text(value.text)
        url = getattr(value, "url", None)
        return f"{text} ({url})" if url else text
    return ""


# ── Cooldown state ────────────────────────────────────────────────────


def test_cooldown_state_is_none_without_credentials(monkeypatch):
    monkeypatch.setattr(
        "agent.credential_pool.load_pool", lambda slug: _FakePool([]))
    assert provider_cooldown_state("nous") is None


def test_cooldown_state_is_none_when_a_credential_is_healthy(monkeypatch):
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([_entry(last_status="ok")]),
    )
    assert provider_cooldown_state("nous") is None


def test_cooldown_state_is_none_when_one_credential_still_routes(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([
            _entry(last_status="exhausted", last_status_at=now - 600,
                   last_error_reset_at=now + 300),
            _entry(last_status="ok"),
        ]),
    )
    assert provider_cooldown_state("nous") is None


def test_cooldown_state_reports_reset_and_hit_when_all_exhausted(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([
            _entry(last_status="exhausted", last_status_at=now - 600,
                   last_error_code=429, last_error_reset_at=now + 300),
        ]),
    )
    state = provider_cooldown_state("nous")
    assert state is not None
    assert state["reset_at"] == pytest.approx(now + 300)
    assert state["hit_at"] == pytest.approx(now - 600)


def test_cooldown_state_uses_the_earliest_reset_across_credentials(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([
            _entry(last_status="exhausted", last_status_at=now - 900,
                   last_error_reset_at=now + 1800),
            _entry(last_status="exhausted", last_status_at=now - 600,
                   last_error_reset_at=now + 300),
        ]),
    )
    state = provider_cooldown_state("nous")
    assert state is not None
    assert state["reset_at"] == pytest.approx(now + 300)
    assert state["hit_at"] == pytest.approx(now - 900)


def test_cooldown_state_is_none_when_the_reset_already_passed(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([
            _entry(last_status="exhausted", last_status_at=now - 3600,
                   last_error_reset_at=now - 60),
        ]),
    )
    assert provider_cooldown_state("nous") is None


def test_cooldown_state_reports_unknown_reset_without_a_reset_time(monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda slug: _FakePool([
            _entry(last_status="exhausted", last_status_at=now - 30),
        ]),
    )
    state = provider_cooldown_state("nous")
    assert state is not None
    assert state["reset_at"] is None
    assert state["hit_at"] == pytest.approx(now - 30)


def test_cooldown_state_is_none_when_the_pool_cannot_be_read(monkeypatch):
    def _boom(slug):
        raise OSError("auth store unreadable")

    monkeypatch.setattr("agent.credential_pool.load_pool", _boom)
    assert provider_cooldown_state("nous") is None


# ── Badge on the main providers list ──────────────────────────────────


def test_providers_list_shows_limited_badge_after_a_limited_provider(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: _limited_state(now) if slug == "nous" else None,
    )
    ui = TelegramProviderManagementUI(FakeAdapter())
    # The render re-reads the pool; pin the fixture ready flags through the
    # same seams the credential tests use so this stays a rendering test.
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda provider: bool(provider.get("ready"))
    providers = [
        _provider(slug="nous", name="Nous Portal", is_current=False, ready=True),
        _provider(slug="openrouter", name="OpenRouter", is_current=True, ready=True),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="providers",
    )
    _rendered, markup = ui._render(state)
    buttons = _keyboard_buttons(markup)
    nous_buttons = [
        (label, data) for label, data in buttons
        if data == "pv:1:provider:1"
    ]
    assert nous_buttons == [("Nous Portal · Added · ⚠ limited · 1h", "pv:1:provider:1")]
    assert ("limited · 1h", "badge:limited:nous") not in buttons
    openrouter_buttons = [
        (label, data) for label, data in buttons
        if data == "pv:1:provider:0"
    ]
    assert openrouter_buttons == [("Current · OpenRouter · Added", "pv:1:provider:0")]


def test_providers_list_badge_without_a_reset_time_is_just_limited(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: {"reset_at": None, "hit_at": None} if slug == "nous" else None,
    )
    ui = TelegramProviderManagementUI(FakeAdapter())
    # The render re-reads the pool; pin the fixture ready flags through the
    # same seams the credential tests use so this stays a rendering test.
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda provider: bool(provider.get("ready"))
    providers = [_provider(slug="nous", name="Nous Portal", is_current=True, ready=True)]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="nous",
        session_key="s", profile_home=None, view="providers",
    )
    _rendered, markup = ui._render(state)
    buttons = _keyboard_buttons(markup)
    assert ("Current · Nous Portal · Added · ⚠ limited", "pv:1:provider:0") in buttons
    assert ("limited", "badge:limited:nous") not in buttons


# ── Badge on the standalone provider page ─────────────────────────────


def test_provider_page_shows_limited_badge_after_the_provider_name(monkeypatch):
    from telethon import types
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: _limited_state(now) if slug == "nous" else None,
    )
    ui = TelegramProviderManagementUI(FakeAdapter())
    provider = _provider(
        slug="nous", name="Nous Portal", auth_type="oauth", ready=True, is_current=True,
        description="About: Nous Portal provider description.",
        is_profile_default=True, profile_default_model="vendor/model-a",
        default_model="vendor/model-a", default_reasoning="high", total_models=42,
        catalog_refreshed=False, catalog_status="cached_or_curated",
    )
    state = ui._new_state(
        Source(), [provider], current_model="vendor/model-a", current_provider="nous",
        session_key="s", profile_home=None, view="provider", selected_provider="nous",
    )
    text, _markup = ui._render(state)
    assert isinstance(text, types.InputRichMessage)
    badges = [
        node for node in _walk_tl_objects(text)
        if isinstance(node, types.TextButton)
        and isinstance(node.type, types.InlineButtonTypeCallback)
        and node.type.data == b"badge:limited:nous"
    ]
    assert len(badges) == 1
    assert _plain_rich_text(badges[0].text) == "limited · 1h"
    assert badges[0].style.bg_danger is True
    heading = next(
        block for block in text.blocks
        if isinstance(block, types.PageBlockHeading1)
    )
    assert _plain_rich_text(heading.text).startswith("Nous Portal limited")


def test_provider_page_has_no_badge_when_the_provider_is_healthy(monkeypatch):
    from telethon import types
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(text_mod, "provider_cooldown_state", lambda slug: None)
    ui = TelegramProviderManagementUI(FakeAdapter())
    provider = _provider(slug="nous", name="Nous Portal", ready=True, is_current=True)
    state = ui._new_state(
        Source(), [provider], current_model="vendor/model-a", current_provider="nous",
        session_key="s", profile_home=None, view="provider", selected_provider="nous",
    )
    text, _markup = ui._render(state)
    body_callbacks = [
        node.type.data.decode("utf-8") for node in _walk_tl_objects(text)
        if isinstance(node, types.TextButton)
        and isinstance(node.type, types.InlineButtonTypeCallback)
    ]
    assert body_callbacks == ["pv:1:credential-status"]


# ── Fresh toast on tap ────────────────────────────────────────────────


def test_limited_toast_names_hit_and_reset(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: _limited_state(now, reset_in=3700),
    )
    assert limited_provider_toast("nous") == (
        "Rate limit hit 1h 1m ago · resets in 1h 2m."
    )


def test_limited_toast_without_a_hit_time_names_only_the_reset(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: {"reset_at": now + 3700, "hit_at": None},
    )
    assert limited_provider_toast("nous") == "Rate-limited · resets in 1h 2m."


def test_limited_toast_without_a_reset_time_says_so(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: {"reset_at": None, "hit_at": now - 3700},
    )
    assert limited_provider_toast("nous") == (
        "Rate limit hit 1h 1m ago · reset time unknown."
    )


def test_limited_toast_is_none_when_no_longer_limited(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(text_mod, "provider_cooldown_state", lambda slug: None)
    assert limited_provider_toast("nous") is None


class _Event:
    """Minimal Telethon callback-query event: records every answer."""

    def __init__(self, data):
        self.data = data
        self.message_id = 101
        self.chat_id = 1
        self.sender_id = 1
        self.sender = SimpleNamespace(id=1, first_name="Alice", bot=False)
        self.chat = SimpleNamespace(id=1, first_name="Alice")
        self.message = SimpleNamespace(id=101, chat_id=1, reply_to=None)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


@pytest.mark.asyncio
async def test_badge_limited_bare_answers_the_static_toast():
    event = _Event("badge:limited")
    assert await answer_badge_callback(event, "badge:limited") is True
    assert event.answers == [((  ":( rate limited",), {})]


@pytest.mark.asyncio
async def test_badge_limited_with_slug_answers_the_fresh_toast(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(
        text_mod, "limited_provider_toast",
        lambda slug: "Rate limit hit 1h 1m ago · resets in 1h 2m.",
    )
    event = _Event("badge:limited:nous")
    assert await answer_badge_callback(event, "badge:limited:nous") is True
    assert event.answers == [(("Rate limit hit 1h 1m ago · resets in 1h 2m.",), {})]


@pytest.mark.asyncio
async def test_badge_limited_with_slug_falls_back_when_no_longer_limited(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(text_mod, "limited_provider_toast", lambda slug: None)
    event = _Event("badge:limited:nous")
    assert await answer_badge_callback(event, "badge:limited:nous") is True
    assert event.answers == [((  ":( rate limited",), {})]


@pytest.mark.asyncio
async def test_badge_limited_tap_dispatched_through_callback_query(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    monkeypatch.setattr(
        text_mod, "limited_provider_toast",
        lambda slug: "Rate limit hit 1h 1m ago · resets in 1h 2m.",
    )
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="<redacted>"))

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=101)

    adapter._client = Client()
    event = _Event("badge:limited:nous")
    await adapter._on_callback_query(event)
    assert event.answers == [(("Rate limit hit 1h 1m ago · resets in 1h 2m.",), {})]
