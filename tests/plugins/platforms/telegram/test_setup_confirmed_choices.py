"""Impactful profile choices require an explicit confirm and preserve sensitive state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from plugins.platforms.telegram.setup_ui import TelegramSetupUI


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["terminal.backend", "tts.provider"])
async def test_confirmed_choice_changes_only_target_profile_key(tmp_path, monkeypatch, key):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    original = {
        "terminal": {"backend": "local"}, "tts": {"provider": "edge"},
        "model": {"default": "preserve/model", "provider": "preserve-provider"},
        "dashboard": {"basic_auth": {"username": "owner", "password_hash": "test-hash"},
                      "require_auth_on_loopback": True},
    }
    for home in homes.values():
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump(original))
    source = SimpleNamespace(chat_type="dm", user_id="101", chat_id="101", profile="alpha", thread_id=None)
    adapter = SimpleNamespace(
        _settings_home=lambda src: homes[src.profile],
        _authorized=lambda _source: True, _is_owner_user=lambda _user, source=None: True,
        _client=object(), _peer_for=lambda _chat: "peer",
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(id=501)),
    )
    ui = TelegramSetupUI(adapter)
    state = ui._new_state(source, homes["alpha"], "terminal" if key.startswith("terminal") else "voice")
    state["message_id"] = "501"
    event = SimpleNamespace(message_id=501, answer=AsyncMock())
    await ui.handle_callback(event, source, f"hm:setup:{state['id']}:cycle:{key}")
    assert yaml.safe_load((homes["alpha"] / "config.yaml").read_text()) == original
    await ui.handle_callback(event, source, f"hm:setup:{state['id']}:select:1")
    assert "Confirm selection" in adapter._mtproto_rich_request.await_args.args[0].rich_message.html
    assert yaml.safe_load((homes["alpha"] / "config.yaml").read_text()) == original
    old_confirm = f"hm:setup:{state['id']}:apply:selection"
    await ui.handle_callback(event, source, old_confirm)
    alpha = yaml.safe_load((homes["alpha"] / "config.yaml").read_text())
    assert yaml.safe_load((homes["beta"] / "config.yaml").read_text()) == original
    section, name = key.split(".")
    assert alpha[section][name] != original[section][name]
    assert alpha["model"] == original["model"]
    assert alpha["dashboard"] == original["dashboard"]
    saved_bytes = (homes["alpha"] / "config.yaml").read_bytes()
    await ui.handle_callback(event, source, old_confirm)
    assert event.answer.await_args.kwargs.get("alert") is True
    assert (homes["alpha"] / "config.yaml").read_bytes() == saved_bytes
