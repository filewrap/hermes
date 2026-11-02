from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.tenant_commands import (
    _run_isolated_worker,
    assert_tenant_policy_covers_registry,
    handle_tenant_command,
    tenant_command_home,
    tenant_command_policy,
)


class TenantStore:
    def __init__(self):
        self.data = {}

    def principal_for_scope(self, uid, chat_type, chat_id):
        return SimpleNamespace(user_id=str(uid))

    def access_for_scope(self, uid, chat_type, chat_id):
        return {"allowed": True, "banned": False, "revoked": False, "sudo": True}

    def authorization_generation(self, uid):
        return 0

    def group_authorization_generation(self, chat_id):
        return 0

    def principal_tenant(self, principal):
        return "tg-" + str(principal.user_id)

    def get(self, principal, kind, key):
        return self.data.get((str(principal.user_id), kind, key))

    def put(self, principal, kind, key, value):
        self.data[(str(principal.user_id), kind, key)] = value



def source(uid="200", chat_id=None, chat_type="dm", thread_id=None):
    uid = str(uid)
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(chat_id or uid),
        chat_type=chat_type,
        user_id=uid,
        user_name="Tenant",
        thread_id=thread_id,
    )


def event(src, text):
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        user_id=src.user_id,
        user_name=src.user_name,
        source=src,
    )


def test_every_gateway_command_has_an_explicit_tenant_classification():
    assert_tenant_policy_covers_registry()
    assert tenant_command_policy("model", "--global") == "tenant"
    assert tenant_command_policy("reasoning", "--global") == "tenant"
    assert tenant_command_policy("reload-skills") == "owner"
    assert tenant_command_policy("sethome") == "owner"
    assert tenant_command_policy("auto") == "owner"
    assert tenant_command_policy("goal", "gate add /restart") == "owner"
    assert tenant_command_policy("made-up-command") == "unavailable"


def test_command_home_is_private_and_per_user(tmp_path):
    first = tenant_command_home(tmp_path, "200")
    second = tenant_command_home(tmp_path, "201")
    assert first != second
    assert first.is_relative_to(tmp_path.resolve())
    assert second.is_relative_to(tmp_path.resolve())
    assert first.stat().st_mode & 0o077 == 0
    assert second.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_real_worker_dispatches_against_tenant_home_without_owner_runtime(tmp_path):
    src = source("200", "-100123", "group", "77")
    home = tenant_command_home(tmp_path, src.user_id)
    result = await _run_isolated_worker(home, src, "/whoami")
    assert result["handled"] is True
    assert "User ID: `200`" in result["result"]
    assert "100" not in result["result"]
    assert (home / "state.db").exists()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.asyncio
async def test_prompt_command_runs_in_tenant_runtime_and_keeps_raw_history(
    tmp_path, monkeypatch,
):
    store = TenantStore()
    src = source("200")
    request = event(src, "/plan write a project outline")
    worker = AsyncMock(return_value={"handled": False, "result": None, "text": request.text})
    tenant = AsyncMock(return_value={"final_response": "Here is an outline.", "messages": [], "usage": {}})
    monkeypatch.setattr("gateway.tenant_commands._run_isolated_worker", worker)
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)

    result = await handle_tenant_command(None, request, src, tmp_path, store=store)

    assert result["final_response"] == "Here is an outline."
    assert worker.await_count == 1
    dispatched = tenant.await_args.args[1]
    assert dispatched.text == request.text
    assert "/plan" in dispatched.metadata["tenant_command_prompt"]
    assert "only tools explicitly enabled by the tenant safety policy" in dispatched.metadata["tenant_command_prompt"]


@pytest.mark.asyncio
async def test_owner_and_mixed_scope_commands_never_start_worker(tmp_path, monkeypatch):
    store = TenantStore()
    src = source("200")
    worker = AsyncMock(side_effect=AssertionError("owner-only command reached worker"))
    monkeypatch.setattr("gateway.tenant_commands._run_isolated_worker", worker)
    for text in ("/reload-skills", "/restart", "/goal gate add /stop"):
        result = await handle_tenant_command(None, event(src, text), src, tmp_path, store=store)
        assert "Only the configured owner" in result["final_response"]
    worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_rotates_only_the_callers_tenant_history(tmp_path, monkeypatch):
    from gateway.tenant_runtime import room_key

    store = TenantStore()
    src = source("200", "-100123", "group", "77")
    key = room_key(src.user_id, src.chat_id, src.thread_id, profile=tmp_path.resolve())
    other_key = room_key("201", src.chat_id, src.thread_id, profile=tmp_path.resolve())
    store.data[("200", "profile", "history:" + key)] = [{"content": "old"}]
    store.data[("201", "profile", "history:" + other_key)] = [{"content": "other-user"}]
    monkeypatch.setattr(
        "gateway.tenant_commands._run_isolated_worker",
        AsyncMock(return_value={"handled": True, "result": "New tenant session started.", "text": "/new"}),
    )

    await handle_tenant_command(None, event(src, "/new"), src, tmp_path, store=store)

    assert store.data[("200", "profile", "history:" + key)] == []
    assert store.data[("201", "profile", "history:" + other_key)] == [{"content": "other-user"}]


@pytest.mark.asyncio
async def test_disabled_group_rejects_tenant_command_before_worker(tmp_path, monkeypatch):
    class DisabledGroupStore(TenantStore):
        def get_group_settings(self, principal, chat_id):
            return {"enabled": False}

    store = DisabledGroupStore()
    src = source("200", "-100123", "group", "77")
    worker = AsyncMock(side_effect=AssertionError("disabled group reached worker"))
    monkeypatch.setattr("gateway.tenant_commands._run_isolated_worker", worker)

    result = await handle_tenant_command(None, event(src, "/whoami"), src, tmp_path, store=store)

    assert "disabled in this group" in result["final_response"]
    worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_revocation_during_worker_discards_result(tmp_path, monkeypatch):
    from gateway.tenant_runtime import TenantRuntimeDenied

    class RevocableStore(TenantStore):
        def __init__(self):
            super().__init__()
            self.generation = 0

        def authorization_generation(self, uid):
            return self.generation

    store = RevocableStore()
    src = source("200")

    async def revoke_before_return(*args, **kwargs):
        store.generation += 1
        return {"handled": True, "result": "should not be delivered", "text": "/whoami"}

    monkeypatch.setattr("gateway.tenant_commands._run_isolated_worker", revoke_before_return)
    with pytest.raises(TenantRuntimeDenied):
        await handle_tenant_command(None, event(src, "/whoami"), src, tmp_path, store=store)


@pytest.mark.asyncio
async def test_global_setting_writes_are_confined_to_each_users_profile(tmp_path):
    import yaml

    first = source("200")
    second = source("201")
    first_home = tenant_command_home(tmp_path, first.user_id)
    second_home = tenant_command_home(tmp_path, second.user_id)
    first_result = await _run_isolated_worker(first_home, first, "/footer on")
    second_result = await _run_isolated_worker(second_home, second, "/footer off")

    assert first_result["handled"] is True
    assert second_result["handled"] is True
    first_config = yaml.safe_load((first_home / "config.yaml").read_text())
    second_config = yaml.safe_load((second_home / "config.yaml").read_text())
    assert first_config["display"]["runtime_footer"]["enabled"] is True
    assert second_config["display"]["runtime_footer"]["enabled"] is False
    assert not (tmp_path / "config.yaml").exists()


def test_each_worker_home_has_only_its_users_scoped_owner_identity(tmp_path):
    from gateway.mongo_multiuser import owner_user_id
    from gateway.tenant_commands import _ensure_tenant_owner_identity

    host_home = tmp_path / "host"
    host_home.mkdir()
    (host_home / ".env").write_text("OWNER_TELEGRAM_ID=99\n")
    first = tenant_command_home(host_home, "200")
    second = tenant_command_home(host_home, "201")

    _ensure_tenant_owner_identity(first, "200")
    _ensure_tenant_owner_identity(second, "201")

    assert owner_user_id(first) == "200"
    assert owner_user_id(second) == "201"
    assert owner_user_id(host_home) == "99"
    assert (first / ".env").read_text() == "OWNER_TELEGRAM_ID=200\n"
    assert (second / ".env").read_text() == "OWNER_TELEGRAM_ID=201\n"
    assert (first / ".env").stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_reasoning_global_setting_is_private_to_each_user_home(tmp_path):
    import yaml
    from gateway.tenant_commands import _ensure_tenant_owner_identity

    first = source("200")
    second = source("201")
    first_home = tenant_command_home(tmp_path, first.user_id)
    second_home = tenant_command_home(tmp_path, second.user_id)
    _ensure_tenant_owner_identity(first_home, first.user_id)
    _ensure_tenant_owner_identity(second_home, second.user_id)

    first_result = await _run_isolated_worker(first_home, first, "/reasoning high --global")
    second_result = await _run_isolated_worker(second_home, second, "/reasoning low --global")

    assert first_result["handled"] and second_result["handled"]
    assert yaml.safe_load((first_home / "config.yaml").read_text())["agent"]["reasoning_effort"] == "high"
    assert yaml.safe_load((second_home / "config.yaml").read_text())["agent"]["reasoning_effort"] == "low"
    assert not (tmp_path / "config.yaml").exists()
