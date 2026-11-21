"""JSON-string allowlists must work through the real native MTProto trigger gate."""
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def adapter(extra):
    native = TelethonAdapter(PlatformConfig(enabled=True, token="fixture", extra=extra))
    native._chat_settings = lambda _source: {}
    native._guestbot_enabled = lambda: False
    return native


def group_source(chat="-100", thread=None):
    return SimpleNamespace(chat_id=chat, chat_type="group", thread_id=thread, user_id="111")


@pytest.mark.asyncio
async def test_json_string_allowlists_control_native_chat_topic_and_free_response():
    event = SimpleNamespace(message=SimpleNamespace(message="hello", reply_to_msg_id=None))
    native = adapter({"allowed_chats": '["-100","-200"]', "require_mention": False})
    assert await native._group_triggered(event, group_source("-100"))
    assert not await native._group_triggered(event, group_source("-999"))
    native = adapter({"allowed_topics": '["5"]', "require_mention": False})
    assert await native._group_triggered(event, group_source(thread="5"))
    assert not await native._group_triggered(event, group_source(thread="9"))
    for extra, scope in (({"free_response_chats": '["-100"]'}, group_source()),
                         ({"free_response_topics": '["5"]'}, group_source(thread="5"))):
        assert await adapter(extra)._group_triggered(event, scope)


@pytest.mark.asyncio
async def test_list_comma_and_malformed_allowlists_preserve_fail_closed_behavior():
    event = SimpleNamespace(message=SimpleNamespace(message="hello", reply_to_msg_id=None))
    for value in ("-100, -200", ["-100", "-200"]):
        assert await adapter({"allowed_chats": value, "require_mention": False})._group_triggered(event, group_source())
    malformed = adapter({"allowed_chats": '["-100", "-200', "require_mention": False})
    assert not await malformed._group_triggered(event, group_source())


def test_runner_side_allow_set_decodes_json_string(monkeypatch):
    from gateway.authz_mixin import _coerce_allow_set
    from gateway.platforms._shared import platform_gate_env

    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", '["-100","-200"]')
    assert _coerce_allow_set(platform_gate_env("TELEGRAM_GROUP_ALLOWED_CHATS")) == {"-100", "-200"}
    assert _coerce_allow_set("-100, -200") == {"-100", "-200"}
    assert _coerce_allow_set(["-100"]) == {"-100"}
