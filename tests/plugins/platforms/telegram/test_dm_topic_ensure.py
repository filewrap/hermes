"""``TelethonAdapter.ensure_dm_topic``: config/cached ids return without a
create, a missing topic is created once, cached, and persisted to config.yaml."""
from datetime import datetime, timezone, UTC
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _topic_receipt():
    service = types.MessageService(
        id=777, peer_id=types.PeerUser(42), date=NOW,
        action=types.MessageActionTopicCreate(title="Debug", icon_color=0),
    )
    return types.UpdateShort(types.UpdateNewMessage(service, pts=1, pts_count=1), date=NOW)


class _Client:
    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        assert isinstance(request, functions.messages.CreateForumTopicRequest)
        return _topic_receipt()


def _adapter(extra=None, client=None):
    adapter = TelethonAdapter(PlatformConfig(extra=extra or {}))
    adapter._client = client if client is not None else _Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    return adapter


@pytest.mark.asyncio
async def test_ensure_dm_topic_returns_configured_thread_id_without_creating():
    client = _Client()
    adapter = _adapter(
        extra={"dm_topics": [{"chat_id": 42, "topics": [{"name": "Debug", "thread_id": 777}]}]},
        client=client)
    assert await adapter.ensure_dm_topic("42", "Debug") == "777"
    assert await adapter.ensure_dm_topic("42", "Debug") == "777"
    assert client.calls == []


@pytest.mark.asyncio
async def test_ensure_dm_topic_creates_missing_topic_once_then_caches(monkeypatch):
    client = _Client()
    adapter = _adapter(client=client)
    persisted = []
    monkeypatch.setattr(adapter, "_persist_dm_topic_thread_id",
                        lambda chat_id, name, thread_id, replace_existing=False: persisted.append(
                            (chat_id, name, thread_id, replace_existing)))
    assert await adapter.ensure_dm_topic("42", "Debug") == "777"
    assert len(client.calls) == 1
    assert persisted == [(42, "Debug", 777, False)]
    assert await adapter.ensure_dm_topic("42", "Debug") == "777"
    assert len(client.calls) == 1
    # A forced refresh creates again instead of returning the cache.
    assert await adapter.ensure_dm_topic("42", "Debug", force_create=True) == "777"
    assert len(client.calls) == 2
    assert persisted[-1] == (42, "Debug", 777, True)


@pytest.mark.asyncio
async def test_ensure_dm_topic_rejects_bad_input():
    adapter = _adapter()
    assert await adapter.ensure_dm_topic("42", "   ") is None
    assert await adapter.ensure_dm_topic("not-a-chat", "Debug") is None


def test_persist_dm_topic_thread_id_writes_config_yaml(tmp_path, monkeypatch):
    import hermes_constants
    import hermes_cli.config as config_mod

    home = tmp_path / "hermes-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("platforms: {}\n", encoding="utf-8")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    adapter = TelethonAdapter(PlatformConfig())
    adapter._persist_dm_topic_thread_id(42, "Debug", 777)
    config = config_mod.read_user_config_raw(config_path)
    assert config["platforms"]["telegram"]["extra"]["dm_topics"] == [
        {"chat_id": 42, "topics": [{"name": "Debug", "thread_id": 777}]}
    ]
    # A second topic in the same chat appends; the first survives.
    adapter._persist_dm_topic_thread_id(42, "Release", 778)
    config = config_mod.read_user_config_raw(config_path)
    topics = config["platforms"]["telegram"]["extra"]["dm_topics"][0]["topics"]
    assert {"name": "Release", "thread_id": 778} in topics
    assert {"name": "Debug", "thread_id": 777} in topics
