from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.slash_commands_status import _telegram_status_tables
from plugins.platforms.telegram import telegram_user_settings as user_settings
from plugins.platforms.telegram import telegram_user_settings_store as store


def test_user_settings_are_scoped_to_user_and_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "_mongodb_collection", lambda _home: None)
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"

    saved = store.update_user_settings(profile_a, "1001", {
        "name": "Kailash",
        "instructions": {"general": "Be concise."},
        "skills": ["research"],
    })

    assert saved["name"] == "Kailash"
    assert saved["instructions"]["general"] == "Be concise."
    assert store.load_user_settings(profile_a, "1001")["skills"] == ["research"]
    assert store.load_user_settings(profile_a, "1002")["name"] == ""
    assert store.load_user_settings(profile_b, "1001")["name"] == ""
    assert (profile_a / "telegram_user_settings.sqlite3").stat().st_mode & 0o777 == 0o600


def test_settings_normalization_discards_unknown_fields_and_bounds_text():
    normalized = store.normalize_user_settings({
        "name": "A" * 200,
        "instructions": {"general": "B" * 3000, "search": "find sources"},
        "admin": True,
    })

    assert len(normalized["name"]) == 64
    assert len(normalized["instructions"]["general"]) == 2000
    assert normalized["instructions"]["search"] == "find sources"
    assert "admin" not in normalized


def test_mongo_backend_lazily_migrates_existing_sqlite_user(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(store, "_mongodb_collection", lambda _home: None)
    store.save_user_settings(tmp_path, "42", {"name": "Legacy", "memory_enabled": False})

    class FakeCollection:
        def __init__(self):
            self.document = None

        def find_one(self, _query):
            return self.document

        def replace_one(self, query, document, upsert=False):
            assert upsert is True
            assert query["_id"] == document["_id"]
            self.document = document

    collection = FakeCollection()
    monkeypatch.setattr(store, "_mongodb_collection", lambda _home: (collection, "profile"))

    loaded = store.load_user_settings(tmp_path, "42")

    assert loaded["name"] == "Legacy"
    assert loaded["memory_enabled"] is False
    assert collection.document["settings"]["name"] == "Legacy"


def test_provider_button_is_only_shown_when_two_providers_are_configured(monkeypatch):
    monkeypatch.setattr(user_settings, "inline_markup", lambda rows: rows)

    _text, one_provider_rows = user_settings._settings_view(store.DEFAULT_SETTINGS)
    _text, two_provider_rows = user_settings._settings_view(store.DEFAULT_SETTINGS, provider_selectable=True)

    one_provider_callbacks = [button[1] for row in one_provider_rows for button in row]
    two_provider_callbacks = [button[1] for row in two_provider_rows for button in row]
    assert "hm:user:providers" not in one_provider_callbacks
    assert "hm:user:providers" in two_provider_callbacks


def test_search_request_explicitly_requests_parallel_top_ten_source_crawl():
    prompt = user_settings.search_request_text("Hermes agent")

    assert prompt.startswith("SEARCH_REQUEST: Hermes agent")
    assert "top 10" in prompt
    assert "concurrently" in prompt
    assert "Do not rely on search snippets alone" in prompt
    assert "direct URLs" in prompt


def test_telegram_status_uses_separate_session_and_platform_tables():
    rendered = _telegram_status_tables([
        "📊 Hermes Gateway Status",
        "",
        "**Session ID:** `20261013_abcd`",
        "**Title:** Plan mode | with no task",
        "**Connected Platforms:** telegram",
    ], ["telegram"])

    assert "<table><thead><tr><th>Detail</th><th>Value</th></tr></thead>" in rendered
    assert "<tr><td>Title</td><td>Plan mode | with no task</td></tr>" in rendered
    assert "<th>Platform</th><th>State</th>" in rendered
    assert "<tr><td>telegram</td><td>Connected</td></tr>" in rendered
    assert "| Detail | Value |" not in rendered
    from plugins.platforms.telegram.mtproto_rich import detect_rich_format, text_and_entities
    assert detect_rich_format(rendered) == "html"
    plain_text, _entities = text_and_entities(rendered)
    assert "Plan mode | with no task" in plain_text


@pytest.mark.asyncio
async def test_personal_preferences_are_attached_only_to_private_turns(monkeypatch):
    settings = {
        "name": "Kailash", "skills": ["research"],
        "instructions": {"general": "Keep answers concise.", "search": "Prefer official sources."},
        "memory_enabled": False, "self_learning_enabled": False,
    }
    monkeypatch.setattr(user_settings, "_load", AsyncMock(return_value=settings))
    source = SimpleNamespace(chat_type="dm", user_id="77", user_name="Telegram name")
    event = SimpleNamespace(
        text="SEARCH_REQUEST: find official docs", user_name="Telegram name",
        channel_prompt="Telegram identity", auto_skill=None,
    )

    attached = await user_settings.attach_user_preferences(SimpleNamespace(), source, event)

    assert attached is settings
    assert source.user_name == "Kailash" and event.user_name == "Kailash"
    assert event.auto_skill == ["research"]
    assert "Keep answers concise." in event.channel_prompt
    assert "Prefer official sources." in event.channel_prompt
    assert "disabled personal memory" in event.channel_prompt
    assert "disabled personal self-learning" in event.channel_prompt

    group = SimpleNamespace(chat_type="group", user_id="77", user_name="Group name")
    group_event = SimpleNamespace(text="question", user_name="Group name", channel_prompt=None, auto_skill=None)
    assert await user_settings.attach_user_preferences(SimpleNamespace(), group, group_event) is None


@pytest.mark.asyncio
async def test_personal_mcp_menu_offers_profile_default_reset(monkeypatch):
    monkeypatch.setattr(user_settings, "_load", AsyncMock(return_value=store.DEFAULT_SETTINGS))
    monkeypatch.setattr(user_settings, "_mcp_catalog", AsyncMock(return_value=["docs-server"]))
    monkeypatch.setattr(user_settings, "inline_markup", lambda rows: rows)
    editor = AsyncMock()
    monkeypatch.setattr(user_settings, "_edit_or_send", editor)
    adapter = SimpleNamespace()
    source = SimpleNamespace(chat_type="dm", user_id="77", chat_id="77")
    event = SimpleNamespace(answer=AsyncMock())

    await user_settings._show_catalog(adapter, event, source, "mcps")

    _text, rows = editor.await_args.args[3:5]
    callbacks = [button[1] for row in rows for button in row]
    assert "hm:user:mcpsinherit" in callbacks
