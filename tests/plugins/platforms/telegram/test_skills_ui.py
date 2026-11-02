from dataclasses import dataclass
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
import io
import re
import zipfile

import pytest
from telethon import types

from gateway.telegram_personal_settings import load_personal_settings
from gateway.telegram_skills import install_skill, list_personal_skills, prepare_skill, prepare_upload, read_personal_skill
from plugins.platforms.telegram.skills_ui import TelegramSkillsUI
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

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
    def __init__(self, text="", message_id=55):
        self.message_id = message_id
        self.message = SimpleNamespace(message=text, id=44, file=None)
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)


def state_for(ui, home, source):
    return ui._new_state(source, home, message_id=55)


def _buttons(markup):
    return [getattr(wrapped, "button", wrapped)
            for row in markup.rows for wrapped in row.buttons]


def _callback(markup, label):
    button = next(button for button in _buttons(markup) if button.text == label)
    return button.type.data.decode()


def _current_state(ui):
    return next(iter(ui._states.values()))


def _rich_text(request):
    rich = request.rich_message
    return getattr(rich, "html", None) or getattr(rich, "markdown", None) or getattr(request, "message", "")


@pytest.mark.asyncio
async def test_pasted_skill_detects_then_requires_one_shot_owner_confirmation(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    assert await ui.handle_message(Event(SKILL), source, SKILL)
    assert list_personal_skills(tmp_path, source.user_id) == []
    state = next(iter(ui._states.values()))
    callback = f"sk:{state['id']}:confirm"
    stranger = Source(user_id="456")
    other = Event()
    assert await ui.handle_callback(other, stranger, callback)
    assert "another account" in other.answers[-1]
    assert list_personal_skills(tmp_path, source.user_id) == []
    assert await ui.handle_callback(Event(), source, callback)
    assert list_personal_skills(tmp_path, source.user_id)[0]["name"] == "daily-notes"
    assert list_personal_skills(tmp_path, stranger.user_id) == []
    replay = Event()
    await ui.handle_callback(replay, source, callback)
    assert "expired" in replay.answers[-1]
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == []


@pytest.mark.asyncio
async def test_dangerous_scan_requires_prominent_explicit_owner_confirmation_not_denial(tmp_path, monkeypatch):
    from tools import skills_guard

    class Scan:
        verdict = "dangerous"
        summary = "deterministic dangerous test finding"
        findings = []

    monkeypatch.setattr(skills_guard, "scan_skill", lambda *args, **kwargs: Scan())
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()

    assert await ui.handle_message(Event(SKILL), source, SKILL)
    review = adapter.edits[-1]
    assert "DANGEROUS SCANNER VERDICT" in _rich_text(review)
    assert "deterministic dangerous test finding" in _rich_text(review)
    assert list_personal_skills(tmp_path, source.user_id) == []
    confirm = next(button for button in _buttons(review.reply_markup) if "I reviewed risks" in button.text)
    assert await ui.handle_callback(Event(), source, confirm.type.data.decode())
    assert [item["name"] for item in list_personal_skills(tmp_path, source.user_id)] == ["daily-notes"]


@pytest.mark.asyncio
async def test_skills_home_actions_and_private_library_pages_have_styles_and_totals(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)

    home_text, home_markup = await ui._home_view(state)
    assert home_text.startswith("# Skills")
    assert "Library and auto-load changes apply to a new session (/new); the current conversation's prompt stays unchanged." in home_text
    assert "• Library and auto-load changes" not in home_text
    assert "**Your skills / library:** **`0 skills`**" in home_text
    assert "**Auto-load selection:** **`0 selected`**" in home_text
    assert home_text.endswith("---\n\n__Page 1 of 1 • total 2__")
    buttons = {button.text: button for button in _buttons(home_markup)}
    rows = [[button.text for button in row.buttons] for row in home_markup.rows]
    assert rows[0] == ["Add a skill"]
    assert rows[1] == ["Overview", "Skill format / template"]
    assert rows[2] == ["Your skills", "Auto-load skills"]
    assert rows[3] == ["Personal settings", "Close"]
    assert not getattr(buttons["Add a skill"].style, "bg_primary", False)
    assert buttons["Overview"].style.bg_primary is True
    assert buttons["Close"].style.bg_danger is True
    assert [button.text for button in _buttons(home_markup)][-1] == "Close"
    assert "Personal settings" in buttons

    for index in range(10):
        install_skill(tmp_path, source.user_id, prepare_skill(
            SKILL.replace("daily-notes", f"daily-notes-{index}"),
        ))
    await ui._list(state, source, "1")
    request = adapter.edits[-1]
    assert "Page 2 of 2 • total 10" in _rich_text(request)
    buttons = _buttons(request.reply_markup)
    callbacks = [button.type.data.decode() for button in buttons]
    assert any(value.endswith(":detail:8") for value in callbacks)
    assert any(value.endswith(":detail:9") for value in callbacks)
    back = next(button for button in buttons if button.text == "Back to Skills")
    assert back.style.bg_primary is True


@pytest.mark.asyncio
async def test_add_skill_callback_flow_reviews_before_writing_private_library(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    await ui.open(source)

    state = _current_state(ui)
    assert await ui.handle_callback(Event(), source, _callback(adapter.edits[0].reply_markup, "Add a skill"))
    add_request = adapter.edits[-1]
    back = next(button for button in _buttons(add_request.reply_markup) if button.text == "Back to Skills")
    assert back.style.bg_primary is True

    state = _current_state(ui)
    input_data = f"sk:{state['id']}:input:text"
    assert await ui.handle_callback(Event(), source, input_data)
    assert state["pending_mode"] == "text"

    assert await ui.handle_message(Event(SKILL), source, SKILL)
    review_request = adapter.edits[-1]
    assert "Confirm new skill" in _rich_text(review_request)
    assert "Saved only to your account after confirmation" in _rich_text(review_request)
    assert list_personal_skills(tmp_path, source.user_id) == []

    state = _current_state(ui)
    confirm_data = _callback(review_request.reply_markup, "Add this skill")
    assert await ui.handle_callback(Event(), source, confirm_data)
    assert [item["name"] for item in list_personal_skills(tmp_path, source.user_id)] == ["daily-notes"]
    assert list_personal_skills(tmp_path, "456") == []


@pytest.mark.asyncio
async def test_autoload_picker_preselects_always_on_default_only_and_preserves_profile_opt_out(tmp_path, monkeypatch):
    from agent import skill_utils

    monkeypatch.setattr(skill_utils, "get_project_skills_dirs", list)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", list)
    skills = tmp_path / "skills"
    for name, directory in (
        ("telegram-rich-messages", skills / "telegram-rich-messages"),
        ("bundled-skill", skills / "bundled-skill"),
        ("custom-skill", skills / "custom-skill"),
        ("hub-skill", skills / ".hub" / "hub-skill"),
    ):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\n# {name}\n", encoding="utf-8"
        )
    (skills / ".bundled_manifest").write_text(
        "telegram-rich-messages:origin\nbundled-skill:origin\nhub-skill:origin\n", encoding="utf-8"
    )

    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = state_for(ui, tmp_path, source)
    await ui._autoload(state, source, "0")
    page = _rich_text(ui.adapter.edits[-1])
    labels = [button.text for button in _buttons(ui.adapter.edits[-1].reply_markup)]

    # Only the always-on default is preselected; other bundled skills stay
    # installed and listed, but load on demand instead of on every request.
    # Selected state is the green button style, not a check-mark character.
    assert "**1** selected." in page
    buttons = {button.text: button for button in _buttons(ui.adapter.edits[-1].reply_markup)}
    assert buttons["telegram-rich-messages"].style.bg_success is True
    assert "bundled-skill" in labels
    assert not getattr(buttons["bundled-skill"].style, "bg_success", False)
    assert "custom-skill" in labels
    assert not getattr(buttons["custom-skill"].style, "bg_success", False)
    assert not any("hub-skill" in label for label in labels)
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == []

    await ui._toggle(state, source, str(state["autoload_names"].index("telegram-rich-messages")))
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == []
    assert "read-only" in _rich_text(ui.adapter.edits[-1])

    await ui._toggle(state, source, str(state["autoload_names"].index("bundled-skill")))
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == ["bundled-skill"]

    (tmp_path / "config.yaml").write_text("skills:\n  auto_load: []\n", encoding="utf-8")
    await ui._autoload(state, source, "0")
    labels = [button.text for button in _buttons(ui.adapter.edits[-1].reply_markup)]
    buttons = {button.text: button for button in _buttons(ui.adapter.edits[-1].reply_markup)}
    assert not getattr(buttons["telegram-rich-messages"].style, "bg_success", False)
    assert "**1** selected." in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_autoload_drop_all_clears_personal_selections_but_keeps_profile_defaults(tmp_path, monkeypatch):
    from agent import skill_utils

    monkeypatch.setattr(skill_utils, "get_project_skills_dirs", list)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", list)
    skills = tmp_path / "skills"
    for name in ("telegram-rich-messages", "bundled-skill", "custom-skill"):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\n# {name}\n", encoding="utf-8"
        )
    (skills / ".bundled_manifest").write_text(
        "telegram-rich-messages:origin\nbundled-skill:origin\n", encoding="utf-8"
    )

    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = state_for(ui, tmp_path, source)
    await ui._autoload(state, source, "0")
    labels = [button.text for button in _buttons(ui.adapter.edits[-1].reply_markup)]
    assert "Drop all" in labels

    # Two personal selections (one bundled-but-not-default, one custom).
    await ui._toggle(state, source, str(state["autoload_names"].index("bundled-skill")))
    await ui._toggle(state, source, str(state["autoload_names"].index("custom-skill")))
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == ["bundled-skill", "custom-skill"]
    assert "**3** selected." in _rich_text(ui.adapter.edits[-1])

    await ui._drop_all(state, source, "0")
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == []
    page = _rich_text(ui.adapter.edits[-1])
    assert "Dropped 2 personal auto-load selection(s)." in page
    assert "Profile defaults stay as they are." in page
    assert "**1** selected." in page
    buttons = {button.text: button for button in _buttons(ui.adapter.edits[-1].reply_markup)}
    assert buttons["telegram-rich-messages"].style.bg_success is True
    assert not getattr(buttons["bundled-skill"].style, "bg_success", False)

    # Dropping again with nothing personal left is a no-op with a plain note.
    await ui._drop_all(state, source, "0")
    assert "No personal auto-load selections to drop" in _rich_text(ui.adapter.edits[-1])


@pytest.mark.asyncio
async def test_autoload_page_12_renders_matching_names_and_global_toggle_indices(tmp_path, monkeypatch):
    import json
    from agent import skill_commands
    from tools import skills_tool

    names = [f"skill-{index:03d}" for index in range(413)]
    monkeypatch.setattr(skill_commands, "scan_skill_commands", lambda: None)
    monkeypatch.setattr(skill_commands, "resolve_profile_auto_load_skill_names", list)
    monkeypatch.setattr(
        skills_tool, "skills_list",
        lambda: json.dumps({"skills": [{"name": name} for name in names]}),
    )
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    state = state_for(ui, tmp_path, source)

    await ui._autoload(state, source, "0")
    for _ in range(11):
        request = ui.adapter.edits[-1]
        await ui.handle_callback(Event(), source, _callback(request.reply_markup, "Next"))

    request = ui.adapter.edits[-1]
    rendered = _rich_text(request)
    buttons = _buttons(request.reply_markup)
    expected = [f"skill-{index:03d}" for index in range(88, 96)]
    labels = [button.text for button in buttons]
    callbacks = [button.type.data.decode() for button in buttons]

    assert "__Page 12 of 52 • total 413__" in rendered
    assert [label for label in labels if label.startswith("skill-")] == expected
    assert any(value.endswith(":toggle:88") for value in callbacks)
    assert not any(value.endswith(":toggle:0") for value in callbacks)
    from telethon.extensions import markdown as telethon_markdown
    plain, entities = telethon_markdown.parse(rendered)
    assert plain.endswith("Page 12 of 52 • total 413")
    assert not plain.endswith("_")
    assert any(type(entity).__name__ == "MessageEntityItalic" for entity in entities)

    await ui.handle_callback(Event(), source, _callback(request.reply_markup, "skill-088"))
    rerendered = ui.adapter.edits[-1]
    rerendered_labels = [button.text for button in _buttons(rerendered.reply_markup)]
    assert "__Page 12 of 52 • total 413__" in _rich_text(rerendered)
    # skill-088 is now selected, so it moves to the front (selected-first) and page 12 shifts by one.
    assert [label for label in rerendered_labels if label.startswith("skill-")] == [
        "skill-087", "skill-089", "skill-090", "skill-091",
        "skill-092", "skill-093", "skill-094", "skill-095",
    ]
    assert "skill-088" in load_personal_settings(tmp_path, source.user_id)["selected_skills"]


@pytest.mark.asyncio
async def test_autoload_and_removal_are_personal_and_deletion_requires_confirmation(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    install_skill(tmp_path, source.user_id, prepare_skill(SKILL))
    state = state_for(ui, tmp_path, source)
    await ui._autoload(state, source, "0")
    index = state["autoload_names"].index("daily-notes")
    await ui._toggle(state, source, str(index))
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == ["daily-notes"]
    assert load_personal_settings(tmp_path, "456")["selected_skills"] == []
    await ui._list(state, source, "0")
    await ui._remove(state, source, "0")
    assert list_personal_skills(tmp_path, source.user_id)
    await ui._delete(state, source, "")
    assert list_personal_skills(tmp_path, source.user_id) == []
    assert load_personal_settings(tmp_path, source.user_id)["selected_skills"] == []


@pytest.mark.asyncio
async def test_edit_keeps_companion_files_and_requires_replacement_confirmation(tmp_path):
    ui = TelegramSkillsUI(Adapter(tmp_path))
    source = Source()
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("daily-notes/SKILL.md", SKILL)
        archive.writestr("daily-notes/references/example.md", "A harmless note example.")
    install_skill(tmp_path, source.user_id, prepare_upload(package.getvalue(), "skill.zip")[0])
    state = state_for(ui, tmp_path, source)
    state["skill_names"] = ["daily-notes"]
    await ui._edit_skill(state, source, "0")
    updated = SKILL.replace("Write a short daily note.", "Write a clear daily note.")
    assert await ui.handle_message(Event(updated), source, updated)
    assert b"short daily" in read_personal_skill(tmp_path, source.user_id, "daily-notes").files["SKILL.md"]
    assert state["replace_skill"]
    await ui._confirm(state, source, "")
    current = read_personal_skill(tmp_path, source.user_id, "daily-notes")
    assert b"clear daily" in current.files["SKILL.md"]
    assert current.files["references/example.md"] == b"A harmless note example."


@pytest.mark.asyncio
async def test_invalid_direct_writing_cancel_expiry_and_settings_entry(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    await ui._input(state, source, "text")
    assert await ui.handle_message(Event("an idea"), source, "an idea")
    assert list_personal_skills(tmp_path, source.user_id) == []
    assert ui._pending
    assert await ui.handle_message(Event("/cancel"), source, "/cancel")
    assert not ui._pending
    group = Source(chat_type="group")
    assert not await ui.handle_message(Event(SKILL), group, SKILL)
    adapter._skills_ui = lambda: SimpleNamespace(open=AsyncMock())
    opener = adapter._skills_ui()
    adapter._skills_ui = lambda: opener
    settings = TelegramPersonalSettingsUI(adapter)
    settings_state = settings._new_state(source, tmp_path, message_id=55)
    await settings._dispatch_action(settings_state, source, Event(), "skills")
    opener.open.assert_awaited_once_with(source, message_id="55")


@pytest.mark.asyncio
async def test_skills_close_deletes_only_attached_menu_and_exact_invocation_for_owner(tmp_path):
    from unittest.mock import AsyncMock

    adapter = Adapter(tmp_path)
    adapter._client.delete_messages = AsyncMock()
    ui = TelegramSkillsUI(adapter)
    source = Source()
    await ui.open(source, invocation_message_id="44")
    state = _current_state(ui)
    callback = f"sk:{state['id']}:close"

    wrong_user = Event()
    await ui.handle_callback(wrong_user, Source(user_id="456"), callback)
    wrong_chat = Event()
    await ui.handle_callback(wrong_chat, Source(chat_id="999"), callback)
    adapter._client.delete_messages.assert_not_awaited()

    assert await ui.handle_callback(Event(), source, callback)
    adapter._client.delete_messages.assert_awaited_once_with("123", [55, 44])
    assert not ui._states


@pytest.mark.asyncio
async def test_callback_failure_is_escaped_in_native_html_preformatted_block(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    await ui.open(source)
    state = _current_state(ui)
    payload = "<b>boom</b>\n```\nunsafe & raw\n_"
    ui._dispatch_action = AsyncMock(side_effect=RuntimeError(payload))

    assert await ui.handle_callback(Event(), source, f"sk:{state['id']}:trigger-error")
    rich = adapter.edits[-1].rich_message
    assert isinstance(rich, types.InputRichMessageHTML)
    assert "<p><b>Action failed</b></p>" in rich.html
    match = re.search(r"<pre>(.*?)</pre>", rich.html, re.DOTALL)
    assert match is not None
    assert unescape(match.group(1)) == payload
    assert "&lt;b&gt;boom&lt;/b&gt;" in match.group(1)
    assert "<b>boom</b>" not in match.group(1)


@pytest.mark.asyncio
async def test_invalid_skill_callback_input_is_not_installed_and_uses_wrapping_paragraph(tmp_path, monkeypatch):
    from gateway import telegram_skills

    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    await ui.open(source)
    state = _current_state(ui)
    assert await ui.handle_callback(Event(), source, f"sk:{state['id']}:add")
    state = _current_state(ui)
    assert await ui.handle_callback(Event(), source, f"sk:{state['id']}:input:text")

    invalid = "<b>not a skill</b>\n````\nmissing frontmatter"
    error = "<b>ZIP file</b>\nrea-HEAD/docs/assets/rea-hopper-analysis/very-long-name.zip: missing required skill metadata"

    def reject_skill(_text):
        raise ValueError(error)

    monkeypatch.setattr(telegram_skills, "prepare_skill", reject_skill)
    assert await ui.handle_message(Event(invalid), source, invalid)
    rich = adapter.edits[-1].rich_message
    assert isinstance(rich, types.InputRichMessageHTML)
    assert "<h1>Skill not accepted</h1>" in rich.html
    assert "<pre>" not in rich.html
    assert "Nothing was installed." in rich.html
    assert "&lt;b&gt;ZIP file&lt;/b&gt; rea-HEAD/docs/assets/rea-hopper-analysis/very-long-name.zip" in rich.html
    assert list_personal_skills(tmp_path, source.user_id) == []


@pytest.mark.asyncio
async def test_forwarded_attachment_is_staged_and_wrong_message_or_profile_cannot_confirm(tmp_path):
    home = tmp_path / "profile-a"
    adapter = Adapter(home)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    attachment = tmp_path / "forwarded.md"
    attachment.write_text(SKILL)
    adapter._cache_inbound_media = AsyncMock(return_value=([str(attachment)], ["document"], ""))
    event = Event()
    event.message.file = SimpleNamespace(name="forwarded.md", size=len(SKILL))
    event.message.fwd_from = SimpleNamespace(from_id=999)
    assert await ui.handle_message(event, source, "")
    state = next(iter(ui._states.values()))
    callback = f"sk:{state['id']}:confirm"
    wrong_message = Event(message_id=999)
    await ui.handle_callback(wrong_message, source, callback)
    assert "expired" in wrong_message.answers[-1]
    adapter.gateway_runner._resolve_profile_home_for_source = lambda source: tmp_path / "profile-b"
    wrong_profile = Event()
    await ui.handle_callback(wrong_profile, source, callback)
    assert "profile" in wrong_profile.answers[-1]
    assert list_personal_skills(home, source.user_id) == []
    adapter.gateway_runner._resolve_profile_home_for_source = lambda source: home
    await ui.handle_callback(Event(), source, callback)
    assert list_personal_skills(home, source.user_id)
    assert not attachment.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected_env"),
    [("Include environment files", True), ("Exclude environment files", False)],
)
async def test_uploaded_dotenv_requires_separate_explicit_choice_before_skill_confirmation(
    tmp_path, choice, expected_env,
):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("daily-notes/SKILL.md", SKILL)
        archive.writestr("daily-notes/.env.template", "API_KEY=publisher-value")
    attachment = tmp_path / "skill.zip"
    attachment.write_bytes(package.getvalue())
    adapter = Adapter(tmp_path)
    adapter._cache_inbound_media = AsyncMock(return_value=([str(attachment)], ["document"], ""))
    ui = TelegramSkillsUI(adapter)
    source = Source()
    event = Event()
    event.message.file = SimpleNamespace(name="skill.zip", size=attachment.stat().st_size)

    assert await ui.handle_message(event, source, "")
    prompt = adapter.edits[-1]
    assert "Environment files detected" in _rich_text(prompt)
    assert ".env.template" in _rich_text(prompt)
    assert list_personal_skills(tmp_path, source.user_id) == []

    assert await ui.handle_callback(
        Event(), source, _callback(prompt.reply_markup, choice),
    )
    review = adapter.edits[-1]
    assert "Confirm new skill" in _rich_text(review)
    if expected_env:
        assert "Environment files:** explicitly approved" in _rich_text(review)
        assert ".env.template" in _rich_text(review)
    else:
        assert "Environment files:** explicitly approved" not in _rich_text(review)
        assert ".env.template" not in _rich_text(review)
    assert list_personal_skills(tmp_path, source.user_id) == []

    assert await ui.handle_callback(
        Event(), source, _callback(review.reply_markup, "Add this skill"),
    )
    stored = read_personal_skill(tmp_path, source.user_id, "daily-notes")
    assert (".env.template" in stored.files) is expected_env


@pytest.mark.asyncio
async def test_multi_skill_root_dotenv_prompt_explains_copy_scope(tmp_path):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("skills/one/SKILL.md", SKILL.replace("daily-notes", "one"))
        archive.writestr("skills/two/SKILL.md", SKILL.replace("daily-notes", "two"))
        archive.writestr(".env.sample", "SHARED=value")
    attachment = tmp_path / "skills.zip"
    attachment.write_bytes(package.getvalue())
    adapter = Adapter(tmp_path)
    adapter._cache_inbound_media = AsyncMock(return_value=([str(attachment)], ["document"], ""))
    ui = TelegramSkillsUI(adapter)
    source = Source()
    event = Event()
    event.message.file = SimpleNamespace(name="skills.zip", size=attachment.stat().st_size)

    assert await ui.handle_message(event, source, "")

    prompt = _rich_text(adapter.edits[-1])
    assert "Environment files detected" in prompt
    assert "copy them into each of the 2 skills" in prompt
    assert "one" in prompt and "two" in prompt


@pytest.mark.asyncio
async def test_input_expiry_and_navigation_clear_pending_confirmation(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    await ui._input(state, source, "text")
    state["created_at"] -= 601
    assert await ui.handle_message(Event(SKILL), source, SKILL)
    assert not ui._pending
    assert not list_personal_skills(tmp_path, source.user_id)
    assert await ui.handle_message(Event(SKILL), source, SKILL)
    state = next(iter(ui._states.values()))
    callback = f"sk:{state['id']}:confirm"
    ui.cancel_for_source(source)
    event = Event()
    await ui.handle_callback(event, source, callback)
    assert "expired" in event.answers[-1]
    assert not list_personal_skills(tmp_path, source.user_id)


@pytest.mark.asyncio
async def test_multi_skill_pagination_and_same_name_private_install(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    state["skill_candidates"] = [prepare_skill(SKILL.replace("daily-notes", f"daily-notes-{index}")) for index in range(10)]
    await ui._dispatch_action(state, source, Event(), "candidates:1")
    request = adapter.edits[-1]
    callbacks = [getattr(button, "button", button).type.data.decode()
                 for row in request.reply_markup.rows for button in row.buttons]
    assert any(value.endswith(":choose:9") for value in callbacks)
    await ui._dispatch_action(state, source, Event(), "choose:9")
    assert state["skill_candidate"].name == "daily-notes-9"
    shared = tmp_path / "skills" / "daily-notes-9" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared_content = SKILL.replace("daily-notes", "daily-notes-9").replace(
        "Write a short daily note.", "PROFILE COPY ONLY")
    shared.write_text(shared_content)
    await ui._confirm(state, source, "")
    assert [item["name"] for item in list_personal_skills(tmp_path, source.user_id)] == ["daily-notes-9"]
    assert read_personal_skill(tmp_path, source.user_id, "daily-notes-9").files["SKILL.md"] != shared_content.encode()
    assert shared.read_text() == shared_content
    state["skill_names"] = ["daily-notes-9"]
    await ui._detail(state, source, "0")
    from agent.skill_commands import personal_skill_command_alias
    private_alias = personal_skill_command_alias("daily-notes-9").replace("-", "_")
    assert f"`/{private_alias} <task>`" in _rich_text(adapter.edits[-1])


@pytest.mark.asyncio
async def test_add_all_reviews_full_batch_and_preserves_global_page_indices(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    state["skill_candidates"] = [
        prepare_skill(SKILL.replace("daily-notes", f"daily-notes-{index}")) for index in range(10)
    ]
    state["skill_candidates"][9].warnings.append("deterministic scanner finding")
    state["skill_candidates"][9].dangerous = True

    await ui._candidates(state, source, "1")
    buttons = _buttons(adapter.edits[-1].reply_markup)
    callbacks = [button.type.data.decode() for button in buttons]
    assert any(value.endswith(":choose:8") for value in callbacks)
    assert any(value.endswith(":choose:9") for value in callbacks)
    assert any(value.endswith(":reviewall:1") for value in callbacks)

    await ui._review_all(state, source, "1")
    review = adapter.edits[-1]
    assert "Review all **10** skills" in _rich_text(review)
    assert "deterministic scanner finding" in _rich_text(review)
    assert "DANGEROUS SCANNER VERDICT" in _rich_text(review)
    assert list_personal_skills(tmp_path, source.user_id) == []
    back = next(button for button in _buttons(review.reply_markup) if button.text == "Back to detected skills")
    assert back.type.data.decode().endswith(":candidates:1")
    assert any("I reviewed risks" in button.text for button in _buttons(review.reply_markup))

    await ui._dispatch_action(state, source, Event(), "candidates:1")
    assert "Page 2 of 2" in _rich_text(adapter.edits[-1])
    await ui._review_all(state, source, "1")
    await ui._confirm_all(state, source, "")
    assert len(list_personal_skills(tmp_path, source.user_id)) == 10
    assert len(load_personal_settings(tmp_path, source.user_id)["personal_skills"]) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['leanrea', 'daily-notes'])
async def test_personal_skill_card_shows_real_telegram_invocation_and_separate_autoload(tmp_path, name):
    source = Source()
    install_skill(tmp_path, source.user_id, prepare_skill(SKILL.replace('daily-notes', name)))
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    await ui.open(source)
    assert await ui.handle_callback(Event(), source, _callback(adapter.edits[-1].reply_markup, 'Your skills'))
    assert await ui.handle_callback(Event(), source, _callback(adapter.edits[-1].reply_markup, name))
    rendered = _rich_text(adapter.edits[-1])
    from agent.skill_commands import personal_skill_command_alias
    command = personal_skill_command_alias(name).replace('-', '_')
    assert f'`/{command} <task>`' in rendered
    assert '`/reload-skills`' in rendered
    assert 'full skill instructions' in rendered
    assert 'No `/new` is needed for this explicit invocation.' in rendered
    assert 'Auto-load instead' in rendered
    assert 'then send `/new` to start a session' in rendered
    assert f'/skill {name}' not in rendered
    assert f'/skills {name}' not in rendered
    assert load_personal_settings(tmp_path, source.user_id)['selected_skills'] == []
    assert list_personal_skills(tmp_path, '456') == []


@pytest.mark.asyncio
async def test_skills_back_is_actor_bound_and_preserves_the_invoking_command(tmp_path):
    adapter = Adapter(tmp_path)
    adapter._client.delete_messages = AsyncMock()
    settings = TelegramPersonalSettingsUI(adapter)
    adapter._settings_ui = lambda: settings
    skills = TelegramSkillsUI(adapter)
    source = Source()
    await skills.open(source, invocation_message_id=44)
    back = _callback(adapter.edits[-1].reply_markup, "Personal settings")
    assert back.startswith("sk:")
    await skills.handle_callback(Event(), Source(user_id="456"), back)
    assert not settings._states
    await skills.handle_callback(Event(), source, back)
    assert not skills._states
    state = _current_state(settings)
    assert state["invocation_message_id"] == "44"
    assert state["message_id"] == "55"
    await settings.handle_callback(Event(), source, f"ps:{state['id']}:close")
    adapter._client.delete_messages.assert_awaited_once_with("123", [55, 44])


def _node_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if type(node).__name__ == "TextPlain":
        return node.text
    if type(node).__name__ == "TextConcat":
        return "".join(_node_text(child) for child in node.texts)
    inner = getattr(node, "text", None)
    return _node_text(inner) if inner is not None else ""


def _italic_spans(rich):
    """Plain text of every italic span in a blocks RichMessage."""
    spans = []

    def collect(node):
        if node is None:
            return
        if type(node).__name__ == "TextItalic":
            spans.append(_node_text(node.text))
            return
        children = []
        if isinstance(getattr(node, "texts", None), (list, tuple)):
            children.extend(node.texts)
        inner = getattr(node, "text", None)
        if inner is not None and not isinstance(inner, str):
            children.append(inner)
        for child in children:
            collect(child)

    for block in getattr(rich, "blocks", []):
        collect(getattr(block, "text", None))
    return spans


def _paragraph_texts(rich):
    """Plain text of each paragraph block in a blocks RichMessage."""
    return [_node_text(getattr(block, "text", None))
            for block in getattr(rich, "blocks", [])
            if type(block).__name__ == "PageBlockParagraph"]


def _copy_texts(rich):
    """Copy-text of every copy-action button in a blocks RichMessage."""
    copies = []

    def collect(node):
        if node is None:
            return
        if type(node).__name__ == "TextButton":
            button_type = getattr(node, "type", None)
            if type(button_type).__name__ == "InlineButtonTypeCopy":
                copies.append(getattr(button_type, "copy_text", ""))
        children = []
        if isinstance(getattr(node, "texts", None), (list, tuple)):
            children.extend(node.texts)
        inner = getattr(node, "text", None)
        if inner is not None and not isinstance(inner, str):
            children.append(inner)
        for child in children:
            collect(child)

    for block in getattr(rich, "blocks", []):
        collect(getattr(block, "text", None))
    return copies


@pytest.mark.asyncio
async def test_your_skills_list_puts_autoload_selected_first_highlighted_and_footer_italic(tmp_path):
    from gateway.telegram_personal_settings import save_personal_settings

    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    for name in ("alpha", "beta", "gamma"):
        install_skill(tmp_path, source.user_id, prepare_skill(SKILL.replace("daily-notes", name)))
    save_personal_settings(tmp_path, source.user_id, {"selected_skills": ["beta"]})
    state = state_for(ui, tmp_path, source)
    await ui._list(state, source, "0")
    request = adapter.edits[-1]
    buttons = _buttons(request.reply_markup)
    labels = [button.text for button in buttons if button.text in ("alpha", "beta", "gamma")]
    assert labels[0] == "beta"
    by_label = {button.text: button for button in buttons}
    assert by_label["beta"].style.bg_success is True
    assert not getattr(by_label["alpha"].style, "bg_success", False)
    assert not getattr(by_label["gamma"].style, "bg_success", False)
    rich = request.rich_message
    assert isinstance(rich, types.InputRichMessage)
    assert "Page 1 of 1 • total 3" in _italic_spans(rich)


@pytest.mark.asyncio
async def test_autoload_list_puts_selected_first_highlighted_with_divider_and_minimal_text(tmp_path, monkeypatch):
    import json

    from agent import skill_commands
    from gateway.telegram_personal_settings import save_personal_settings
    from tools import skills_tool

    monkeypatch.setattr(skill_commands, "scan_skill_commands", lambda: None)
    monkeypatch.setattr(skill_commands, "resolve_profile_auto_load_skill_names", list)
    names = ["alpha", "beta", "gamma", "zebra"]
    monkeypatch.setattr(
        skills_tool, "skills_list",
        lambda: json.dumps({"skills": [{"name": name} for name in names]}),
    )
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    save_personal_settings(tmp_path, source.user_id, {"selected_skills": ["zebra"]})
    state = state_for(ui, tmp_path, source)
    await ui._autoload(state, source, "0")
    request = adapter.edits[-1]
    page = _rich_text(request)
    buttons = _buttons(request.reply_markup)
    labels = [button.text for button in buttons if button.text in names]
    assert labels[0] == "zebra"
    by_label = {button.text: button for button in buttons}
    assert by_label["zebra"].style.bg_success is True
    assert not getattr(by_label["alpha"].style, "bg_success", False)
    assert "---\n\n__Page 1 of 1 • total 4__" in page
    assert "Profile defaults are read-only." in page
    assert "Toggle other skills to add them to your private sessions" not in page
    rich = request.rich_message
    assert isinstance(rich, types.InputRichMessage)
    assert "Page 1 of 1 • total 4" in _italic_spans(rich)


@pytest.mark.asyncio
async def test_skills_home_has_overview_button_add_skill_individual_no_bullet_and_italic_footer(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    home_text, home_markup = await ui._home_view(state)
    assert "• Library/auto-load changes" not in home_text
    assert "Library and auto-load changes apply to a new session (/new);" in home_text
    rows = [[button.text for button in row.buttons] for row in home_markup.rows]
    assert rows[0] == ["Add a skill"]
    assert rows[1] == ["Overview", "Skill format / template"]
    assert rows[2] == ["Your skills", "Auto-load skills"]
    assert rows[3] == ["Personal settings", "Close"]
    buttons = {button.text: button for button in _buttons(home_markup)}
    assert not getattr(buttons["Add a skill"].style, "bg_primary", False)
    assert buttons["Overview"].style.bg_primary is True
    assert buttons["Close"].style.bg_danger is True
    await ui._render_home(state)
    request = adapter.edits[-1]
    rich = request.rich_message
    assert isinstance(rich, types.InputRichMessage)
    assert "Page 1 of 1 • total 2" in _italic_spans(rich)


@pytest.mark.asyncio
async def test_skills_home_overview_has_one_divider_before_footer_and_counts_on_own_rows(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    home_text, _ = await ui._home_view(state)
    assert home_text.count("---") == 1
    assert "stays unchanged.\n\n**Your skills / library:**" in home_text
    assert "---\n\n__Page 1 of 1 • total 2__" in home_text
    assert "**Your skills / library:** **`0 skills`**\n\n**Auto-load selection:** **`0 selected`**" in home_text
    await ui._render_home(state)
    rich = adapter.edits[-1].rich_message
    assert isinstance(rich, types.InputRichMessage)
    paragraphs = _paragraph_texts(rich)
    assert "Your skills / library: 0 skills" in paragraphs
    assert "Auto-load selection: 0 selected" in paragraphs
    assert not any("Your skills / library:" in text and "Auto-load selection:" in text
                   for text in paragraphs)
    copies = _copy_texts(rich)
    assert "0 skills" in copies
    assert "0 selected" in copies


@pytest.mark.asyncio
async def test_skills_home_overview_status_note_is_its_own_row(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    home_text, _ = await ui._home_view(state, note="No change was applied.")
    assert "**Auto-load selection:** **`0 selected`**\n\n**Status:** No change was applied." in home_text
    await ui._render_home(state, note="No change was applied.")
    rich = adapter.edits[-1].rich_message
    assert isinstance(rich, types.InputRichMessage)
    assert "Status: No change was applied." in _paragraph_texts(rich)


@pytest.mark.asyncio
async def test_skills_home_error_overview_has_one_divider_before_footer_and_code_values(tmp_path):
    adapter = Adapter(tmp_path)
    ui = TelegramSkillsUI(adapter)
    source = Source()
    state = state_for(ui, tmp_path, source)
    error_text, _ = await ui._home_view(state, note="No change was applied.", error="boom")
    assert error_text.count("<hr />") == 1
    assert "<hr /><p><i>Page 1 of 1 • total 3</i></p>" in error_text
    assert "stays unchanged.</p><p><b>Your skills / library:</b>" in error_text
    assert "<p><b>Your skills / library:</b> <code>0 skills</code></p>" in error_text
    assert "<p><b>Auto-load selection:</b> <code>0 selected</code></p>" in error_text
    assert "<p><b>Status:</b> No change was applied.</p>" in error_text
