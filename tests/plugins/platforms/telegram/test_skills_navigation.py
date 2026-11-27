"""Behaviour-contract tests for Telegram /skills navigation, errors and markers.

Covers the Phase 3 skills audit: missing Back buttons, Back landing on the wrong
page, cryptic callback failures, the mislabelled error card, the check-mark dingbat,
the "Add all" label, and bold counts.
"""
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.telegram_skills import install_skill, prepare_skill
from plugins.platforms.telegram.skills_ui import TelegramSkillsUI

SKILL = "---\nname: daily-notes\ndescription: Use when writing daily notes.\n---\n# Instructions\nWrite a short daily note.\n"


@dataclass
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Tester"
    chat_type: str = "dm"
    thread_id: str | None = None


class Adapter:
    def __init__(self, home):
        self._client = SimpleNamespace(send_file=AsyncMock())
        self.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: home)
        self.send = AsyncMock()
        self.edits = []

    def _peer_for(self, chat):
        return chat

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return SimpleNamespace(id=55)

    def _sent_message_id(self, response):
        return str(response.id)


class Event:
    def __init__(self, message_id=55):
        self.message_id = message_id
        self.message = SimpleNamespace(message="", id=44, file=None)
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)


def _buttons(markup):
    return [getattr(wrapped, "button", wrapped)
            for row in markup.rows for wrapped in row.buttons]


def _callback(markup, label):
    button = next(button for button in _buttons(markup) if button.text == label)
    return button.type.data.decode()


def _rich_text(request):
    rich = request.rich_message
    return getattr(rich, "html", None) or getattr(rich, "markdown", None) or getattr(request, "message", "")


def _state_for(ui, home, source):
    return ui._new_state(source, home, message_id=55)


def _install_many(tmp_path, source, count, prefix="s"):
    for index in range(count):
        install_skill(tmp_path, source.user_id, prepare_skill(
            SKILL.replace("daily-notes", f"{prefix}-{index:02d}"),
        ))


def _stub_autoload(monkeypatch, names):
    from agent import skill_commands
    from tools import skills_tool
    monkeypatch.setattr(skill_commands, "scan_skill_commands", lambda: None)
    monkeypatch.setattr(skill_commands, "resolve_profile_auto_load_skill_names", list)
    monkeypatch.setattr(
        skills_tool, "skills_list",
        lambda: json.dumps({"skills": [{"name": name} for name in names]}),
    )


@pytest.mark.asyncio
async def test_detail_and_remove_cancel_return_to_the_list_page_they_came_from(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    _install_many(tmp_path, source, 10)
    state = _state_for(ui, tmp_path, source)
    await ui._list(state, source, "1")
    await ui._detail(state, source, "9")
    back = _callback(ui.adapter.edits[-1].reply_markup, "Your skills")
    assert back.endswith(":list:1")
    await ui.handle_callback(Event(), source, back)
    assert "Page 2 of 2" in _rich_text(ui.adapter.edits[-1])

    await ui._detail(state, source, "9")
    await ui._remove(state, source, "9")
    cancel = _callback(ui.adapter.edits[-1].reply_markup, "Cancel")
    assert cancel.endswith(":list:1")
    await ui.handle_callback(Event(), source, cancel)
    assert "Page 2 of 2" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_autoload_back_returns_to_the_skill_detail_it_came_from(tmp_path, monkeypatch):
    _stub_autoload(monkeypatch, ["daily-notes"])
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    install_skill(tmp_path, source.user_id, prepare_skill(SKILL))
    state = _state_for(ui, tmp_path, source)
    state["skill_names"] = ["daily-notes"]
    await ui._detail(state, source, "0")
    await ui.handle_callback(
        Event(), source, _callback(ui.adapter.edits[-1].reply_markup, "Auto-load selection"),
    )
    assert "Auto-load skills" in _rich_text(ui.adapter.edits[-1])
    back = _callback(ui.adapter.edits[-1].reply_markup, "Back to skill")
    assert back.endswith(":detail:0")
    await ui.handle_callback(Event(), source, back)
    assert "# daily-notes" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_help_back_returns_to_add_when_entered_from_add(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    await ui._add(state, source, "")
    await ui.handle_callback(
        Event(), source, _callback(ui.adapter.edits[-1].reply_markup, "Skill format / template"),
    )
    assert "Skill format" in _rich_text(ui.adapter.edits[-1])
    back = _callback(ui.adapter.edits[-1].reply_markup, "Back to add")
    assert back.endswith(":add")
    await ui.handle_callback(Event(), source, back)
    assert "Choose how to add it" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_edit_input_cancel_returns_to_the_skill_detail(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    install_skill(tmp_path, source.user_id, prepare_skill(SKILL))
    state = _state_for(ui, tmp_path, source)
    state["skill_names"] = ["daily-notes"]
    state["edit_name"] = "daily-notes"
    await ui._input(state, source, "text")
    back = next(button for button in _buttons(ui.adapter.edits[-1].reply_markup)
                if button.text == "Back to skill")
    assert back.type.data.decode().endswith(":detail:0")
    await ui.handle_callback(Event(), source, back.type.data.decode())
    assert "# daily-notes" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_single_skill_review_has_back_to_detected_skills(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    state["skill_candidates"] = [prepare_skill(SKILL.replace("daily-notes", f"c-{index}"))
                                 for index in range(3)]
    await ui._candidates(state, source, "0")
    await ui._choose(state, source, "0")
    assert "Confirm new skill" in _rich_text(ui.adapter.edits[-1])
    back = _callback(ui.adapter.edits[-1].reply_markup, "Back to detected skills")
    assert back.endswith(":candidates:0")
    await ui.handle_callback(Event(), source, back)
    assert "Detected skills" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_preview_has_back_to_the_full_review(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    state["skill_candidates"] = [prepare_skill(SKILL)]
    await ui._choose(state, source, "0")
    assert "Confirm new skill" in _rich_text(ui.adapter.edits[-1])
    await ui._preview(state, source, "")
    assert "sent as a file" in _rich_text(ui.adapter.edits[-1])
    back = _callback(ui.adapter.edits[-1].reply_markup, "Back to review")
    await ui.handle_callback(Event(), source, back)
    assert "Confirm new skill" in _rich_text(ui.adapter.edits[-1])
    assert "Security review" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_callback_failure_names_the_action_not_an_import(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    await ui.open(source)
    state = next(iter(ui._states.values()))
    ui._dispatch_action = AsyncMock(side_effect=RuntimeError("toggle exploded"))

    assert await ui.handle_callback(Event(), source, f"sk:{state['id']}:trigger-error")
    rich = adapter.edits[-1].rich_message
    assert "<b>Action failed</b>" in rich.html
    assert "Import error" not in rich.html
    assert "No change was applied." in rich.html
    assert "toggle exploded" in rich.html


@pytest.mark.asyncio
async def test_stale_skill_and_autoload_choices_fail_with_actionable_reasons(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    state["skill_names"] = ["daily-notes"]
    with pytest.raises(ValueError, match="no longer in your library"):
        ui._name(state, "5")
    state["autoload_names"] = ["alpha-skill"]
    with pytest.raises(ValueError, match="no longer available"):
        await ui._toggle(state, source, "7")
    with pytest.raises(ValueError, match="removal expired"):
        await ui._delete(state, source, "")
    state["skill_candidates"] = [prepare_skill(SKILL)]
    with pytest.raises(ValueError, match="no longer available"):
        await ui._choose(state, source, "4")


@pytest.mark.asyncio
async def test_selected_autoload_skills_are_green_without_a_check_mark(tmp_path, monkeypatch):
    _stub_autoload(monkeypatch, ["alpha-skill", "beta-skill"])
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    await ui._autoload(state, source, "0")
    await ui._toggle(state, source, "0")
    buttons = {button.text: button for button in _buttons(ui.adapter.edits[-1].reply_markup)}
    assert not any("✓" in label for label in buttons)
    assert buttons["alpha-skill"].style.bg_success is True
    assert not getattr(buttons["beta-skill"].style, "bg_success", False)
    assert "**1** selected." in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_batch_button_offers_review_not_a_direct_add(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = _state_for(ui, tmp_path, source)
    state["skill_candidates"] = [prepare_skill(SKILL.replace("daily-notes", f"c-{index}"))
                                 for index in range(3)]
    await ui._candidates(state, source, "0")
    labels = [button.text for button in _buttons(ui.adapter.edits[-1].reply_markup)]
    assert "Review all 3" in labels
    assert not any("Add all" in label for label in labels)
    assert "**3** skills found." in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_list_and_review_counts_are_highlighted(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    _install_many(tmp_path, source, 10)
    state = _state_for(ui, tmp_path, source)
    await ui._list(state, source, "0")
    assert "**10** private skills." in _rich_text(ui.adapter.edits[-1])
    state["skill_candidates"] = [prepare_skill(SKILL.replace("daily-notes", f"c-{index}"))
                                 for index in range(10)]
    await ui._review_all(state, source, "0")
    assert "# Review all **10** skills" in _rich_text(ui.adapter.edits[-1])
