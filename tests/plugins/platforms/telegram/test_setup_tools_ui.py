from __future__ import annotations

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


@pytest.mark.asyncio
async def test_owner_private_gate_and_callbacks_are_bound_to_owner_card_profile_and_thread(tmp_path):
    home = tmp_path / "profile-a"
    _write_config(home, ["web"], ["terminal"], "provider/model-a")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)

    await ui.handle_command(_source(user_id="202"))
    await ui.handle_command(_source(chat_type="group"))
    assert len(adapter.sent) == 2
    assert not ui._states

    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    original_state_id = state["id"]
    event = _Event(state["message_id"])
    foreign = _Event(state["message_id"])
    assert await ui.handle_callback(foreign, _source(user_id="202"),
                                    f"hm:setuptools:{original_state_id}:toggle:browser")
    assert not adapter.edited
    assert state["draft"] == {"web"}

    # Even the owner cannot move a card's callback to a different topic.
    assert await ui.handle_callback(_Event(state["message_id"]), _source(thread_id="88"),
                                    f"hm:setuptools:{original_state_id}:toggle:browser")
    assert not adapter.edited
    assert await ui.handle_callback(_Event("other-message"), source,
                                    f"hm:setuptools:{original_state_id}:toggle:browser")
    assert not adapter.edited

    # Expired state is rejected and cannot be used to stage changes.
    state["created_at"] -= 16 * 60
    assert await ui.handle_callback(event, source,
                                    f"hm:setuptools:{original_state_id}:toggle:browser")
    assert not ui._states
    assert not adapter.edited


@pytest.mark.asyncio
async def test_selector_pages_and_staged_change_requires_explicit_apply(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)
    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))

    catalog_count = len(state["catalog"])
    catalog_keys = {key for key, _label, _description in state["catalog"]}
    assert not catalog_keys.intersection({"computer_use", "connections", "context_engine", "homeassistant", "spotify"})
    pages = (catalog_count + 5) // 6
    state["page"] = pages
    text, markup = ui._list_view(state)
    assert f"Page {pages} of {pages} • total {catalog_count}" in text
    if pages > 1:
        previous_labels = [getattr(getattr(item, "button", item), "text", "")
                           for row in markup.rows for item in row.buttons]
        assert f"Previous · {pages - 1}/{pages}" in previous_labels

    first_page_state_id = state["id"]
    state["page"] = 1
    event = _Event(state["message_id"])
    payload = _callback_payload(ui._list_view(state)[1], "Enable Browser Automation")
    assert payload.endswith(":toggle:browser")
    # Preview is edited in place; no config write occurs before Apply.
    before = (home / "config.yaml").read_text(encoding="utf-8")
    assert await ui.handle_callback(event, source, payload)
    state = next(iter(ui._states.values()))
    assert state["id"] != first_page_state_id
    assert "access external data or perform actions" in adapter.edited[-1][2]
    assert "Nothing has been saved yet" in adapter.edited[-1][2]
    assert (home / "config.yaml").read_text(encoding="utf-8") == before

    # Returning to review keeps the draft, but still does not persist it.
    review_payload = _callback_payload(adapter.edited[-1][3], "Review toolsets")
    assert await ui.handle_callback(event, source, review_payload)
    assert state["draft"] == {"web", "browser"}
    assert (home / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_apply_uses_profile_scoped_tools_config_and_a_b_a_readback(tmp_path):
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    _write_config(home_a, ["web"], ["terminal", "file"], "provider/model-a")
    _write_config(home_b, ["terminal"], ["web"], "provider/model-b")
    adapter = _Adapter({"alpha": home_a, "beta": home_b})
    ui = TelegramSetupToolsUI(adapter)

    async def enable_and_apply(source, key):
        await ui.handle_command(source)
        state = next(s for s in ui._states.values() if s["profile"] == source.profile)
        event = _Event(state["message_id"])
        markup = ui._list_view(state)[1]
        label = next(label for candidate, label, _description in state["catalog"] if candidate == key)
        payload = _callback_payload(markup, f"Enable {label}")
        assert await ui.handle_callback(event, source, payload)
        confirm_markup = adapter.edited[-1][3]
        apply_payload = _callback_payload(confirm_markup, "Apply for next turn")
        assert await ui.handle_callback(event, source, apply_payload)
        return adapter.edited[-1][2]

    status_a = await enable_and_apply(_source("alpha"), "browser")
    assert "Saved and verified for the next Telegram message turn" in status_a
    raw_a = yaml.safe_load((home_a / "config.yaml").read_text(encoding="utf-8"))
    assert "browser" in raw_a["platform_toolsets"]["telegram"]
    assert raw_a["platform_toolsets"]["discord"] == ["terminal", "file"]
    assert raw_a["model"]["default"] == "provider/model-a"

    status_b = await enable_and_apply(_source("beta"), "file")
    assert "Saved and verified for the next Telegram message turn" in status_b
    raw_b = yaml.safe_load((home_b / "config.yaml").read_text(encoding="utf-8"))
    assert "file" in raw_b["platform_toolsets"]["telegram"]
    assert raw_b["platform_toolsets"]["discord"] == ["web"]
    assert raw_b["model"]["default"] == "provider/model-b"

    # A → B → A confirms scope restoration and the edit path's readback.
    await ui.handle_command(_source("alpha"))
    state_a = next(s for s in ui._states.values() if s["profile"] == "alpha")
    assert {"web", "browser"}.issubset(state_a["draft"])
    assert state_a["message_id"] is not None
    assert len(adapter.edited) >= 4
    assert "Browser Automation" in adapter.edited[-1][2]
    final_a = yaml.safe_load((home_a / "config.yaml").read_text(encoding="utf-8"))
    final_b = yaml.safe_load((home_b / "config.yaml").read_text(encoding="utf-8"))
    assert "browser" in final_a["platform_toolsets"]["telegram"]
    assert "browser" not in final_b["platform_toolsets"]["telegram"]


@pytest.mark.asyncio
async def test_toolset_apply_preserves_global_policy_and_unlisted_integrations(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web", "no_mcp", "private_integration"], ["terminal"], "test/model")
    raw = yaml.safe_load((home / "config.yaml").read_text())
    raw["agent"] = {"disabled_toolsets": ["browser"]}
    (home / "config.yaml").write_text(yaml.safe_dump(raw))
    ui = TelegramSetupToolsUI(_Adapter({"alpha": home}))
    await ui.handle_command(_source())
    state = next(iter(ui._states.values()))
    state["draft"].add("file")
    assert "Saved and verified" in await ui._apply(state)
    actual = yaml.safe_load((home / "config.yaml").read_text())
    assert {"no_mcp", "private_integration", "web", "file"}.issubset(actual["platform_toolsets"]["telegram"])
    assert actual["agent"]["disabled_toolsets"] == ["browser"]
    assert actual["platform_toolsets"]["discord"] == ["terminal"]
    before = (home / "config.yaml").read_bytes()
    state["draft"].add("browser")
    with pytest.raises(RuntimeError, match="global disabled-toolset policy"):
        await ui._apply(state)
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.asyncio
async def test_review_changes_opens_summary_without_applying_or_discarding(tmp_path):
    home = tmp_path / "profile"
    _write_config(home, ["web"], ["terminal"], "provider/model")
    adapter = _Adapter({"alpha": home})
    ui = TelegramSetupToolsUI(adapter)
    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    event = _Event(state["message_id"])
    before = (home / "config.yaml").read_bytes()
    key, label, _description = next(row for row in state["catalog"] if row[0] == "video_gen")
    state["page"] = next(index // 6 + 1 for index, row in enumerate(state["catalog"]) if row[0] == key)
    await ui.handle_callback(event, source, _callback_payload(ui._list_view(state)[1], f"Enable {label}"))
    await ui.handle_callback(event, source, _callback_payload(adapter.edited[-1][3], "Review toolsets"))
    listing = adapter.edited[-1]
    assert "Pending changes" in listing[2]
    await ui.handle_callback(event, source, _callback_payload(listing[3], "Review changes"))
    summary = adapter.edited[-1]
    assert "Confirm toolset changes" in summary[2] and f"Enable {label}" in summary[2]
    assert "Nothing has been saved yet" in summary[2]
    assert "No gateway restart" in summary[2]
    assert "On the next message turn" in summary[2]
    assert "including in this conversation" in summary[2]
    assert "cached agent and its tool schemas" in summary[2]
    assert _button_label(summary[3], "Apply for next turn")
    assert _button_label(summary[3], "Discard changes")
    assert state["draft"] == {"web", key}
    assert (home / "config.yaml").read_bytes() == before
    assert len(adapter.sent) == 1
    await ui.handle_callback(event, source, _callback_payload(summary[3], "Review toolsets"))
    await ui.handle_callback(event, source, _callback_payload(adapter.edited[-1][3], f"Disable {label}"))
    assert "There are no pending changes" in adapter.edited[-1][2]
    assert "no longer available" not in adapter.edited[-1][2]
    assert state["draft"] == state["original"]
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.asyncio
async def test_leaving_toolset_card_retains_pending_draft_scoped_to_identity(tmp_path):
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    _write_config(home_a, ["web"], ["terminal"], "provider/model-a")
    _write_config(home_b, ["terminal"], ["web"], "provider/model-b")
    adapter = _Adapter({"alpha": home_a, "beta": home_b})
    ui = TelegramSetupToolsUI(adapter)
    source = _source(thread_id="7")
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    state["draft"].add("video_gen")
    before = (home_a / "config.yaml").read_bytes()
    old_id = state["id"]
    ui._drop_state(old_id)
    assert old_id not in ui._states
    assert ui.pending_notice(source)
    assert not ui.pending_notice(_source("beta", thread_id="7"))
    assert not ui.pending_notice(_source(thread_id="8"))
    assert not ui.pending_notice(_source(user_id="202", thread_id="7"))
    await ui.handle_command(source)
    resumed = next(iter(ui._states.values()))
    assert resumed["id"] != old_id and resumed["draft"] == {"web", "video_gen"}
    assert "Confirm toolset changes" in adapter.sent[-1][1]
    assert (home_a / "config.yaml").read_bytes() == before
    event = _Event(resumed["message_id"])
    await ui.handle_callback(event, source, _callback_payload(adapter.sent[-1][2], "Discard changes"))
    assert not ui.pending_notice(source)
    assert (home_a / "config.yaml").read_bytes() == before
