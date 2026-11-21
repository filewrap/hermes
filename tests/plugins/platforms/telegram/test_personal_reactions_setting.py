import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gateway.telegram_personal_settings import (
    _validated_changes,
    load_personal_settings,
    public_personal_settings,
    save_personal_settings,
)
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
from plugins.platforms.telegram.rich_ui import personal_settings_view


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []
        self.answer_kwargs = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text or "")
        self.answer_kwargs.append(kwargs)


class Adapter:
    def __init__(self):
        self._client = object()
        self.edits = []

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return SimpleNamespace(id=55)


def test_reactions_default_migrates_existing_settings_and_stays_profile_user_scoped(tmp_path):
    old_path = tmp_path / "personal/telegram/users/123/settings.json"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps({"memory_enabled": False}), encoding="utf-8")

    existing = load_personal_settings(tmp_path, "123")
    assert existing["memory_enabled"] is False
    assert existing["reactions_enabled"] is True
    assert existing["activity_trail_enabled"] is False
    saved = save_personal_settings(tmp_path, "123", {"reactions_enabled": False})
    assert load_personal_settings(tmp_path, "123")["reactions_enabled"] is False
    assert load_personal_settings(tmp_path, "456")["reactions_enabled"] is True
    assert load_personal_settings(tmp_path / "other-profile", "123")["reactions_enabled"] is True
    assert load_personal_settings(tmp_path, "456")["activity_trail_enabled"] is False
    assert saved["reactions_enabled"] is False


def test_reactions_setting_requires_strict_boolean_and_survives_export_import_payload():
    for invalid in (0, 1, "true", "false", None, []):
        with pytest.raises(ValueError):
            _validated_changes({"reactions_enabled": invalid})
    exported = public_personal_settings({"reactions_enabled": False})
    assert exported["reactions_enabled"] is False
    assert _validated_changes({key: value for key, value in exported.items() if key != "updated_at"})["reactions_enabled"] is False


def test_activity_trail_setting_is_strict_boolean_and_survives_export_import_payload():
    for invalid in (0, 1, "true", "false", None, []):
        with pytest.raises(ValueError):
            _validated_changes({"activity_trail_enabled": invalid})
    exported = public_personal_settings({"activity_trail_enabled": True})
    assert exported["activity_trail_enabled"] is True
    assert _validated_changes({key: value for key, value in exported.items() if key != "updated_at"})[
        "activity_trail_enabled"
    ] is True


@pytest.mark.asyncio
async def test_reactions_toggle_callback_is_owner_bound_and_one_shot(tmp_path):
    adapter = Adapter()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    ui = TelegramPersonalSettingsUI(adapter)
    owner = Source()
    state = ui._new_state(owner, tmp_path, message_id=55)
    state["user_name"] = owner.user_name
    state_id = state["id"]

    wrong_owner = Source(user_id="456")
    rejected = CallbackEvent(55)
    assert await ui.handle_callback(rejected, wrong_owner, f"ps:{state_id}:reactions-toggle")
    assert load_personal_settings(tmp_path, "123")["reactions_enabled"] is True
    assert load_personal_settings(tmp_path, "456")["reactions_enabled"] is True

    event = CallbackEvent(55)
    assert await ui.handle_callback(event, owner, f"ps:{state_id}:reactions-toggle")
    assert load_personal_settings(tmp_path, "123")["reactions_enabled"] is False
    edits_after_first = len(adapter.edits)
    replay = CallbackEvent(55)
    assert await ui.handle_callback(replay, owner, f"ps:{state_id}:reactions-toggle")
    assert load_personal_settings(tmp_path, "123")["reactions_enabled"] is False
    assert len(adapter.edits) == edits_after_first
    assert "expired" in replay.answers[-1].lower()


def test_personal_settings_view_labels_reactions_as_local_preference():
    text, _markup = personal_settings_view({"reactions_enabled": True}, "Alice", 9)
    assert "Message reactions" in text
    assert '<tg-button type="callback_data" style="danger" data="ps:9:reactions-toggle">Turn off</tg-button>' in text


def test_activity_trail_view_control_is_personal_and_visible():
    text, _markup = personal_settings_view({"activity_trail_enabled": True}, "Alice", 9)
    assert "Turn activity trail" in text and "<td><b>Turn activity trail</b></td><td>On</td>" in text
    assert '<tg-button type="callback_data" style="danger" data="ps:9:activity-trail-toggle">Turn off</tg-button>' in text
