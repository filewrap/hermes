from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.telegram_personal_settings import save_personal_settings
from gateway.telegram_skills import install_skill, prepare_skill
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
from plugins.platforms.telegram.skills_ui import TelegramSkillsUI


@dataclass
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Tester"
    chat_type: str = "dm"
    thread_id: str | None = None
    profile: str = ""


class Adapter:
    def __init__(self, home):
        self._client = object()
        self.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
        self.edits = []
        self.frames = []

    def _peer_for(self, chat_id):
        return chat_id

    def _push_control_frame(self, source, message_id, frame):
        self.frames.append((source, str(message_id), dict(frame)))
        return True

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return SimpleNamespace(id=55)

    def _sent_message_id(self, response):
        return str(response.id)


@pytest.mark.asyncio
async def test_settings_to_skills_pushes_current_safe_personal_destination(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state.update(control_action="mcp", control_page=2)
    skills_ui = SimpleNamespace(open=AsyncMock())
    adapter._skills_ui = lambda: skills_ui

    await ui._dispatch_action(state, source, SimpleNamespace(), "skills")

    assert adapter.frames == [(source, "55", {
        "kind": "personal", "action": "mcp", "page": 2,
    })]
    skills_ui.open.assert_awaited_once_with(source, message_id="55")


@pytest.mark.asyncio
async def test_personal_frame_restore_rereads_settings_and_rejects_untrusted_destinations(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    save_personal_settings(tmp_path, source.user_id, {"default_model": "fresh-model"})

    assert await ui.restore_control_frame(source, "55", {
        "kind": "personal", "action": "home", "page": 0,
    })
    restored = next(iter(ui._states.values()))
    assert restored["pending_mode"] is None
    assert restored["message_id"] == "55"
    assert "fresh-model" in adapter.edits[-1].message
    assert not await ui.restore_control_frame(source, "55", {
        "kind": "personal", "action": "export-confirm:all", "page": 0,
    })
    assert len(adapter.edits) == 1


@pytest.mark.asyncio
async def test_skills_to_personal_pushes_semantic_skill_home_and_restores_fresh_library(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    settings_ui = SimpleNamespace(open=AsyncMock())
    adapter._settings_ui = lambda: settings_ui

    await ui._personal(state, source, "")
    assert adapter.frames == [(source, "55", {
        "kind": "skills", "view": "home", "action": "home", "page": 0,
    })]
    settings_ui.open.assert_awaited_once_with(source, message_id="55", invocation_message_id=None)

    skill = "---\nname: daily-notes\ndescription: Use for daily notes.\n---\n# Notes\nWrite a note.\n"
    install_skill(tmp_path, source.user_id, prepare_skill(skill))
    assert await ui.restore_control_frame(source, "55", {
        "kind": "skills", "view": "list", "action": "list", "page": 0,
    })
    restored_buttons = [getattr(button, "button", button)
                        for row in adapter.edits[-1].reply_markup.rows for button in row.buttons]
    assert any(button.text == "daily-notes" for button in restored_buttons)
    restored = next(iter(ui._states.values()))
    assert restored["pending_mode"] is None

    assert not await ui.restore_control_frame(source, "55", {
        "kind": "skills", "view": "confirm", "action": "confirm", "page": 0,
    })
