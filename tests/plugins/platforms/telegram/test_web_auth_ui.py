from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.web_auth_ui import TelegramWebAuthUI


class _Client:
    def __init__(self):
        self.deleted = []
        self.fail_delete = False

    async def delete_messages(self, peer, ids):
        if self.fail_delete:
            raise RuntimeError("delete denied")
        self.deleted.append((peer, list(ids)))


class _Adapter:
    def __init__(self, homes: dict[str, Path]):
        self.homes = homes
        self._client = _Client()
        self.requests = []
        self.sent = []

    def _settings_home(self, source):
        return self.homes.get(source.profile)

    def _authorized(self, source):
        return not getattr(source, "denied", False)

    def _is_owner_user(self, user_id, *, source=None):
        return str(user_id) == "101" and source is not None

    def _peer_for(self, chat_id):
        return str(chat_id)

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=800 + len(self.requests))

    def _sent_message_id(self, result, **_kwargs):
        return result.id

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Event:
    def __init__(self, message_id="901"):
        self.message_id = str(message_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _NewMessageEvent:
    """Match Telethon NewMessage: ID lives on event.message, not event.message_id."""
    def __init__(self, message_id):
        self.message = SimpleNamespace(id=int(message_id))
        self.id = int(message_id)


def _source(*, profile="alpha", chat_type="dm", user_id="101", chat_id="101", thread_id=None):
    return SimpleNamespace(profile=profile, chat_type=chat_type, user_id=user_id,
                           chat_id=chat_id, thread_id=thread_id)


async def _open_configure(ui, source):
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    event = _Event(state["message_id"])
    assert await ui.handle_callback(event, source, f"hm:webauth:{state['id']}:configure")
    return state, event


async def _input(ui, adapter, source, state, text, message_id):
    event = _NewMessageEvent(message_id)
    handled = await ui.handle_message(event, source, text)
    assert handled is True
    assert (str(source.chat_id), [int(message_id)]) in adapter._client.deleted
    return event


async def _setup_and_save(ui, source, username, password, first_id, current_password=None):
    await ui.handle_command(source)
    state = max(ui._states.values(), key=lambda item: item["id"])
    configured = _Event(state["message_id"])
    assert await ui.handle_callback(configured, source, f"hm:webauth:{state['id']}:configure")
    message_id = first_id
    if current_password is not None:
        await _input(ui, ui.adapter, source, state, current_password, message_id)
        message_id += 1
    await _input(ui, ui.adapter, source, state, username, message_id)
    await _input(ui, ui.adapter, source, state, password, message_id + 1)
    await _input(ui, ui.adapter, source, state, password, message_id + 2)
    assert state["stage"] == "confirm_save"
    save = _Event(state["message_id"])
    assert await ui.handle_callback(save, source, f"hm:webauth:{state['id']}:save")
    return state


@pytest.mark.asyncio
async def test_owner_private_gate_rich_card_and_callback_bound_to_exact_card(tmp_path):
    adapter = _Adapter({"alpha": tmp_path / "alpha"})
    ui = TelegramWebAuthUI(adapter)

    await ui.handle_command(_source(chat_type="group"))
    await ui.handle_command(_source(user_id="202"))
    assert not ui._states
    assert len(adapter.sent) == 2

    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    html = adapter.requests[-1].rich_message.html
    assert "<h1>Web UI authentication</h1>" in html
    assert "<table>" in html and "Page 1 of 1 • total" in html
    assert html.rstrip().endswith("</p>")
    assert "HTTP Basic" in html
    assert "Loopback login" in html
    assert "does not change the dashboard bind" in html

    foreign = _Event("902")
    assert await ui.handle_callback(foreign, source, f"hm:webauth:{state['id']}:configure")
    assert foreign.answers[-1][1].get("alert") is True
    assert state["stage"] == "idle"


@pytest.mark.asyncio
async def test_new_credentials_deleted_hashed_saved_and_login_through_existing_provider(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  disabled:\n    - basic\n", encoding="utf-8")
    launch_home = tmp_path / "launch-home"
    launch_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    assert state["stage"] == "username"

    await _input(ui, adapter, source, state, "profile-admin", 201)
    password = "  sufficiently-long-password  "
    await _input(ui, adapter, source, state, password, 202)
    await _input(ui, adapter, source, state, password, 203)
    assert state["stage"] == "confirm_save"
    assert "sufficiently-long-password" not in repr(state)
    assert "sufficiently-long-password" not in adapter.requests[-1].rich_message.html

    callback = _Event(state["message_id"])
    save_data = f"hm:webauth:{state['id']}:save"
    from unittest.mock import Mock
    saved = Mock(wraps=ui._save_credentials)
    monkeypatch.setattr(ui, "_save_credentials", saved)
    assert await ui.handle_callback(callback, source, save_data)
    assert "Working" in adapter.requests[-2].rich_message.html
    assert "not forcibly revoked" in adapter.requests[-1].rich_message.html
    replay = _Event(state["message_id"])
    assert await ui.handle_callback(replay, source, save_data)
    assert replay.answers[-1][1].get("alert") is True
    saved.assert_called_once()

    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config
    with _profile_runtime_scope(home):
        config = load_config()
    basic = config["dashboard"]["basic_auth"]
    assert basic["username"] == "profile-admin"
    assert basic["password_hash"].startswith("scrypt$")
    assert basic["password"] == ""
    assert config["dashboard"]["require_auth_on_loopback"] is True
    assert "sufficiently-long-password" not in (home / "config.yaml").read_text(encoding="utf-8")
    assert "basic" not in config.get("plugins", {}).get("disabled", [])

    from plugins.dashboard_auth.basic import BasicAuthProvider
    provider = BasicAuthProvider(username=basic["username"], password_hash=basic["password_hash"],
                                 secret=basic["secret"].encode())
    session = provider.complete_password_login(username="profile-admin", password=password)
    assert session
    assert str(launch_home) == __import__("os").environ["HERMES_HOME"]


@pytest.mark.asyncio
async def test_existing_credentials_require_old_password_and_confirm_before_overwrite(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    from plugins.dashboard_auth.basic import hash_password
    old_hash = hash_password("original-long-password")
    (home / "config.yaml").write_text(
        "dashboard:\n  basic_auth:\n    username: current-user\n    password_hash: " + old_hash + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    assert state["stage"] == "current_password"

    await _input(ui, adapter, source, state, "wrong-current-password", 301)
    assert state["stage"] == "current_password"
    assert "wrong-current-password" not in repr(state)
    await _input(ui, adapter, source, state, "original-long-password", 302)
    assert state["stage"] == "username"
    await _input(ui, adapter, source, state, "new-user", 303)
    await _input(ui, adapter, source, state, "replacement-long-password", 304)
    await _input(ui, adapter, source, state, "replacement-long-password", 305)
    assert state["stage"] == "confirm_save"

    # No config mutation occurs before the separate one-shot save confirmation.
    assert "current-user" in (home / "config.yaml").read_text(encoding="utf-8")
    callback = _Event(state["message_id"])
    assert await ui.handle_callback(callback, source, f"hm:webauth:{state['id']}:save")
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config
    with _profile_runtime_scope(home):
        updated = load_config()["dashboard"]["basic_auth"]
    assert updated["username"] == "new-user"
    assert updated["password_hash"] != old_hash


@pytest.mark.asyncio
async def test_profile_env_override_is_detected_and_never_shadowed_by_yaml(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=profile-env-secret\n", encoding="utf-8")
    (home / "config.yaml").write_text("dashboard:\n  basic_auth:\n    username: existing\n    password_hash: scrypt$bad\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, callback = await _open_configure(ui, source)
    assert state["stage"] == "idle"
    assert callback.answers[-1][1].get("alert") is True
    content = (home / "config.yaml").read_text(encoding="utf-8")
    assert "profile-env-secret" not in content
    assert "profile-env-secret" not in repr(state)


@pytest.mark.asyncio
async def test_profile_scoped_config_write_alternates_a_b_a(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home in (launch, home_a, home_b):
        home.mkdir()
    (home_a / "config.yaml").write_text("compression:\n  threshold: 0.31\n", encoding="utf-8")
    (home_b / "config.yaml").write_text("compression:\n  threshold: 0.72\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    adapter = _Adapter({"alpha": home_a, "beta": home_b})
    ui = TelegramWebAuthUI(adapter)

    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config
    set_multiplex_active(True)
    try:
        await _setup_and_save(ui, _source(profile="alpha"), "alice", "alpha-password-long", 401)
        with _profile_runtime_scope(home_a):
            a_before_overwrite = load_config()
        await _setup_and_save(ui, _source(profile="beta"), "bob", "bravo-password-long", 411)
        await _setup_and_save(ui, _source(profile="alpha"), "alice-final", "again-password-long", 421,
                              current_password="alpha-password-long")
        with _profile_runtime_scope(home_a):
            a_after_overwrite = load_config()
        with _profile_runtime_scope(home_b):
            b = load_config()
    finally:
        set_multiplex_active(False)
    assert a_before_overwrite["dashboard"]["basic_auth"]["username"] == "alice"
    assert b["dashboard"]["basic_auth"]["username"] == "bob"
    assert a_after_overwrite["dashboard"]["basic_auth"]["username"] == "alice-final"
    assert a_before_overwrite["compression"]["threshold"] == pytest.approx(0.31)
    assert b["compression"]["threshold"] == pytest.approx(0.72)
    assert a_before_overwrite["dashboard"]["basic_auth"]["password_hash"] != a_after_overwrite["dashboard"]["basic_auth"]["password_hash"]
    assert __import__("os").environ["HERMES_HOME"] == str(launch)


@pytest.mark.asyncio
async def test_duplicate_new_password_update_cannot_confirm_and_confirm_save_text_is_consumed(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    await _input(ui, adapter, source, state, "admin", 501)

    duplicate = _NewMessageEvent(502)
    assert await ui.handle_message(duplicate, source, "new-password-with-enough-length")
    assert state["stage"] == "confirm_password"
    assert await ui.handle_message(duplicate, source, "new-password-with-enough-length")
    assert state["stage"] == "confirm_password"
    assert ("101", [502]) in adapter._client.deleted

    await _input(ui, adapter, source, state, "new-password-with-enough-length", 503)
    assert state["stage"] == "confirm_save"
    await _input(ui, adapter, source, state, "typed-password-must-not-confirm", 504)
    assert state["stage"] == "confirm_save"
    assert "use the Save button" in adapter.requests[-1].rich_message.html
    assert "Save credentials" in repr(adapter.requests[-1].reply_markup.to_dict())


@pytest.mark.asyncio
async def test_expired_secret_is_deleted_and_expiry_warning_is_visible(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    ui._pending[state["identity"]]["expires_at"] = 0

    event = _NewMessageEvent(511)
    assert await ui.handle_message(event, source, "late-secret-password") is True
    assert ("101", [511]) in adapter._client.deleted
    assert "Credential input expired" in adapter.requests[-1].rich_message.html
    assert state["identity"] in ui._expired


@pytest.mark.asyncio
async def test_unknown_slash_prefixed_password_is_deleted_and_hashed_not_forwarded(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    await _input(ui, adapter, source, state, "admin", 521)
    secret = "/not_a_real_command-but-a-secret!"

    await _input(ui, adapter, source, state, secret, 522)
    assert state["stage"] == "confirm_password"
    from plugins.dashboard_auth.basic import _verify_password
    assert _verify_password(secret, state["draft_hash"])
    assert all(secret not in repr(item) for item in adapter.sent)
    assert all(secret not in request.rich_message.html for request in adapter.requests)


@pytest.mark.asyncio
async def test_delete_failure_cancels_setup_and_shows_owner_warning(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)
    adapter._client.fail_delete = True

    assert await ui.handle_message(_NewMessageEvent(531), source, "sensitive-input") is True
    assert state["stage"] == "idle"
    assert state["identity"] not in ui._pending
    assert "could not be deleted" in adapter.requests[-1].rich_message.html
    assert "sensitive-input" not in adapter.requests[-1].rich_message.html


@pytest.mark.asyncio
async def test_password_hash_env_override_is_read_only(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    env_content = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$profile-only\n"
    (home / ".env").write_text(env_content, encoding="utf-8")
    (home / "config.yaml").write_text("dashboard:\n  basic_auth:\n    username: old\n", encoding="utf-8")
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    state, callback = await _open_configure(ui, _source())

    assert state["stage"] == "idle"
    assert callback.answers[-1][1].get("alert") is True
    assert (home / ".env").read_text(encoding="utf-8") == env_content
    assert "require_auth_on_loopback" not in (home / "config.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_routed_profile_ignores_root_dashboard_env_credentials(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    profile = tmp_path / "profile-b"
    launch.mkdir()
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "root-profile-secret")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "scrypt$root-profile-hash")
    before = {name: __import__("os").environ[name] for name in (
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH")}
    adapter = _Adapter({"beta": profile})
    ui = TelegramWebAuthUI(adapter)

    await _setup_and_save(ui, _source(profile="beta"), "beta-user", "beta-profile-password", 601)
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config
    with _profile_runtime_scope(profile):
        basic = load_config()["dashboard"]["basic_auth"]
    assert basic["username"] == "beta-user"
    assert basic["password_hash"]
    assert {name: __import__("os").environ[name] for name in before} == before


@pytest.mark.asyncio
async def test_message_source_permission_and_thread_binding_are_exact(tmp_path):
    home = tmp_path / "profile"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    adapter = _Adapter({"alpha": home, "beta": other})
    ui = TelegramWebAuthUI(adapter)
    source = _source(thread_id="44")
    state, _ = await _open_configure(ui, source)

    assert await ui.handle_message(_NewMessageEvent(611), _source(profile="beta"), "not-for-this-profile") is False
    assert await ui.handle_message(_NewMessageEvent(612), _source(user_id="202"), "not-owner") is False
    assert await ui.handle_message(_NewMessageEvent(613), _source(chat_type="group"), "group") is False
    assert state["stage"] == "username"

    # A missing thread is not recovered to thread 44: reject and delete the input.
    assert await ui.handle_message(_NewMessageEvent(614), _source(), "wrong-thread-secret") is True
    assert ("101", [614]) in adapter._client.deleted
    assert state["stage"] == "username"
    assert "different thread" in adapter.requests[-1].rich_message.html
    await _input(ui, adapter, source, state, "right-thread-user", 615)
    assert state["stage"] == "new_password"


@pytest.mark.asyncio
async def test_cancel_at_bot_username_is_recognized_during_secret_input(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)
    source = _source()
    state, _ = await _open_configure(ui, source)

    assert await ui.handle_message(_NewMessageEvent(621), source, "/cancel@hermesbot") is True
    assert state["stage"] == "idle"
    assert state["identity"] not in ui._pending


@pytest.mark.asyncio
async def test_existing_plaintext_and_hash_unchanged_credentials_can_be_overwritten(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    from plugins.dashboard_auth.basic import hash_password
    current = "current-password-long"
    (home / "config.yaml").write_text(
        "dashboard:\n  basic_auth:\n    username: old-user\n    password_hash: "
        + hash_password(current) + "\n    password: " + current + "\n", encoding="utf-8")
    adapter = _Adapter({"alpha": home})
    ui = TelegramWebAuthUI(adapter)

    await _setup_and_save(ui, _source(), "new-user", "new-password-long", 631,
                          current_password=current)
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config
    with _profile_runtime_scope(home):
        basic = load_config()["dashboard"]["basic_auth"]
    assert basic["username"] == "new-user"
    assert basic["password"] == ""


@pytest.mark.asyncio
async def test_auth_configure_invalidates_same_source_setup_edits_only(tmp_path):
    from plugins.platforms.telegram.setup_ui import TelegramSetupUI
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    adapter = _Adapter({"alpha": home_a, "beta": home_b})
    setup = adapter._telegram_setup_ui = TelegramSetupUI(adapter)
    source = _source()
    active = setup._new_state(source, home_a, "terminal")
    sibling = setup._new_state(_source(profile="beta"), home_b, "terminal")
    setup._pending[active["identity"]] = {"state_id": active["id"], "field": "terminal.cwd"}
    ui = TelegramWebAuthUI(adapter)
    state, _ = await _open_configure(ui, source)
    assert state["stage"] == "username"
    assert active["id"] not in setup._states
    assert active["identity"] not in setup._pending
    assert sibling["id"] in setup._states
