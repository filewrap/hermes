"""Invariant tests for the Worker B (tools/MCP) fixes in the Telegram toolset UI.

Covers: Apply surfacing the specific save reason (B3), open failures surfacing
the specific load reason (B5), bold staged counts/labels (A5), and the
post-Apply toggle flip.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from plugins.platforms.telegram.setup_tools_ui import TelegramSetupToolsUI


class _Adapter:
    def __init__(self, homes: dict[str, Path], owner="101"):
        self.homes = homes
        self.owner = str(owner)
        self.sent = []
        self.edited = []

    def _settings_home(self, source):
        return self.homes[str(source.profile)]

    def _authorized(self, source):
        return str(source.user_id) == self.owner

    def _is_owner_user(self, user_id, *, source=None):
        return str(user_id) == self.owner

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self.sent.append((source, text, markup, reply_to))
        return SimpleNamespace(id=f"message-{len(self.sent)}")

    async def _edit_rich_ui_message(self, event, source, text, markup=None):
        self.edited.append((event, source, text, markup))


class _Event:
    def __init__(self, message_id):
        self.message_id = str(message_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def _source(profile="alpha", user_id="101", chat_type="dm", thread_id=None):
    return SimpleNamespace(chat_type=chat_type, user_id=str(user_id), chat_id=str(user_id),
                           profile=profile, thread_id=thread_id)


def _write_config(home: Path, telegram: list[str], other: list[str], marker: str):
    home.mkdir(parents=True)
    config = {
        "platform_toolsets": {"telegram": telegram, "discord": other},
        "model": {"default": marker},
        "display": {"timestamps": True},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _button_label(markup, label):
    for row in markup.rows:
        for wrapped in row.buttons:
            button = getattr(wrapped, "button", wrapped)
            if getattr(button, "text", "") == label:
                return wrapped
    return None


def _callback_payload(markup, label):
    wrapped = _button_label(markup, label)
    assert wrapped is not None
    button = getattr(wrapped, "button", wrapped)
    return bytes(button.type.data).decode()


def _labels(markup):
    return [getattr(getattr(item, "button", item), "text", "")
            for row in markup.rows for item in row.buttons]


@pytest.mark.asyncio
async def test_apply_failure_shows_the_specific_policy_reason(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    raw = yaml.safe_load((home / "config.yaml").read_text())
    raw["agent"] = {"disabled_toolsets": ["browser"]}
    (home / "config.yaml").write_text(yaml.safe_dump(raw))
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)
    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    event = _Event(state["message_id"])
    payload = _callback_payload(ui._list_view(state)[1], "Enable Browser Automation")
    assert await ui.handle_callback(event, source, payload)
    apply_payload = _callback_payload(adapter.edited[-1][3], "Apply for next turn")
    assert await ui.handle_callback(event, source, apply_payload)
    status = adapter.edited[-1][2]
    assert "global disabled-toolset policy requires review" in status
    assert "could not be verified" not in status


@pytest.mark.asyncio
async def test_open_failure_shows_the_specific_load_reason(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)

    def _broken_home(source):
        raise RuntimeError("The active Telegram profile could not be resolved")

    monkeypatch.setattr(adapter, "_settings_home", _broken_home)
    await ui.handle_command(_source())
    assert len(adapter.sent) == 1
    assert "The active Telegram profile could not be resolved" in adapter.sent[0][1]
    assert not ui._states


def test_staged_count_and_change_labels_are_bold(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)
    state = ui._new_state(_source(), home,
                          {"platform_toolsets": {"telegram": ["web"]}}, {"web"})
    state["draft"].add("browser")
    text, _markup = ui._list_view(state)
    assert "<b>2</b> standard toolsets enabled" in text
    assert "<b>Added:</b>" in text


@pytest.mark.asyncio
async def test_apply_success_re_renders_list_with_flipped_toggle(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)
    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    event = _Event(state["message_id"])
    payload = _callback_payload(ui._list_view(state)[1], "Enable Browser Automation")
    assert await ui.handle_callback(event, source, payload)
    apply_payload = _callback_payload(adapter.edited[-1][3], "Apply for next turn")
    assert await ui.handle_callback(event, source, apply_payload)
    assert "Saved and verified for the next Telegram message turn" in adapter.edited[-1][2]
    assert "Disable Browser Automation" in _labels(adapter.edited[-1][3])
    assert "Enable Browser Automation" not in _labels(adapter.edited[-1][3])
