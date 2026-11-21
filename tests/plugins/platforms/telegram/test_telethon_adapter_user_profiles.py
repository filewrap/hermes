"""Invariants for universal profiles in the Telegram adapter: admission is answered
by the AUTHORIZATION home (the transport bot's home), never by the runtime home.

A profiled user's own home holds a tenant store of its own (the user is that
store's owner), so reading admission from the runtime home would silently grant
(or deny) from the wrong store. The launch store allows the user while the user
home's store does not — the tests below only pass when the adapter reads the
launch store.
"""

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.mongo_multiuser import principal, store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER_ID = "8495104954"
USER_ID = "123456789"
OTHER_OWNER_ID = "999999999"


@pytest.fixture
def stores(tmp_path):
    """Launch store allows the user; the user home's store does not."""
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    launch_store = store_for_home(launch)
    launch_store.allow_user(principal(OWNER_ID), USER_ID, True)

    user_home = tmp_path / "profiles" / f"tg-{USER_ID}"
    user_home.mkdir(parents=True)
    (user_home / ".env").write_text(f"OWNER_TELEGRAM_ID={OTHER_OWNER_ID}\n", encoding="utf-8")
    store_for_home(user_home)  # creates the store; the user is never allowed in it
    return SimpleNamespace(launch=launch, user_home=user_home)


def _adapter(*, launch=None, user_home=None, authorization=True):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    runner = SimpleNamespace()
    if authorization:
        runner._authorization_home_for_source = lambda source: launch
    runner._resolve_profile_home_for_source = lambda source: user_home
    adapter.gateway_runner = runner
    return adapter


def _source(profile=None):
    return SimpleNamespace(chat_id=USER_ID, user_id=USER_ID, chat_type="dm", is_bot=False,
                           profile=profile)


def test_multiuser_home_reads_the_authorization_home(stores):
    adapter = _adapter(launch=stores.launch, user_home=stores.user_home)

    assert adapter._multiuser_home(_source()) == stores.launch


def test_authorized_answers_from_the_launch_store_for_a_profiled_dm(stores):
    adapter = _adapter(launch=stores.launch, user_home=stores.user_home)

    assert adapter._authorized(_source()) is True


def test_authorized_denies_when_only_the_runtime_home_is_available(stores):
    # A runner predating the authorization seam falls back to the runtime home —
    # the user home's store, where the user is not allowed — and denies.
    adapter = _adapter(launch=stores.launch, user_home=stores.user_home, authorization=False)

    assert adapter._multiuser_home(_source()) == stores.user_home
    assert adapter._authorized(_source()) is False


def test_multiuser_home_is_none_without_a_configured_owner(tmp_path):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _authorization_home_for_source=lambda source: tmp_path / "no-owner",
        _resolve_profile_home_for_source=lambda source: tmp_path / "no-owner",
    )
    (tmp_path / "no-owner").mkdir()

    assert adapter._multiuser_home(_source()) is None


def test_multiuser_home_is_none_without_a_runner():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = None

    # No runner: the ambient (test-isolated) home has no owner configured, so the
    # multiuser path stays off rather than re-enabling legacy access.
    assert adapter._multiuser_home(_source()) is None


# --- Backfill (first DM from a pre-existing admitted user) ---------------------


@pytest.fixture
def backfill_env(tmp_path, monkeypatch):
    """Launch home that IS the hermes home, with the user started + allowed."""
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    launch = tmp_path / ".hermes"
    launch.mkdir()
    (launch / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    store = store_for_home(launch)
    store.start_user(USER_ID)
    store.allow_user(principal(OWNER_ID), USER_ID, True)
    return SimpleNamespace(launch=launch, store=store)


def test_backfill_creates_the_profile_on_a_first_dm(backfill_env):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    access = backfill_env.store.access_for_scope(USER_ID, "dm", USER_ID)
    assert access.get("allowed") and access.get("started")

    home = adapter._maybe_backfill_user_profile(backfill_env.store, _source(), access)

    assert home is not None
    assert home == backfill_env.launch / "profiles" / f"tg-{USER_ID}"
    assert (home / ".env").read_text(encoding="utf-8").count(f"OWNER_TELEGRAM_ID={USER_ID}") == 1


def test_backfill_skips_groups_unstarted_and_revoked_users(backfill_env):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    access = backfill_env.store.access_for_scope(USER_ID, "dm", USER_ID)

    group = SimpleNamespace(chat_id="-1001", user_id=USER_ID, chat_type="group", is_bot=False)
    assert adapter._maybe_backfill_user_profile(backfill_env.store, group, access) is None

    unstarted = {**access, "started": False}
    assert adapter._maybe_backfill_user_profile(backfill_env.store, _source(), unstarted) is None

    revoked = {**access, "allowed": False}
    assert adapter._maybe_backfill_user_profile(backfill_env.store, _source(), revoked) is None

    assert adapter._maybe_backfill_user_profile(None, _source(), access) is None
    assert adapter._maybe_backfill_user_profile(backfill_env.store, _source(), None) is None


# --- Fall-through (D7): profiled DMs run the normal turn ----------------------


def test_is_profiled_dm_reads_the_pinned_identity_only():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    assert adapter._is_profiled_dm(_source(profile=f"tg-{USER_ID}")) is True
    assert adapter._is_profiled_dm(_source(profile=None)) is False
    assert adapter._is_profiled_dm(_source(profile="default")) is False
    group = SimpleNamespace(chat_id="-1001", user_id=USER_ID, chat_type="group",
                            profile=f"tg-{USER_ID}", is_bot=False)
    assert adapter._is_profiled_dm(group) is False


def test_profiled_dm_memory_dir_is_inside_the_user_home(stores):
    # D8: set_personal_memory_context keeps being called with the RUNTIME home,
    # so a profiled DM's memory dir is the user home's personal overlay — the
    # DM personal scope is now the profile's own memory.
    from pathlib import Path

    from gateway.telegram_personal_settings import (
        current_personal_memory_context,
        reset_personal_memory_context,
        set_personal_memory_context,
    )
    from tools.memory_tool import get_memory_dir

    adapter = _adapter(launch=stores.launch, user_home=stores.user_home)
    source = _source(profile=f"tg-{USER_ID}")
    home = adapter._settings_ui()._home(source)
    assert Path(home) == stores.user_home.resolve()

    token = set_personal_memory_context(home, USER_ID)
    try:
        context = current_personal_memory_context()
        expected = stores.user_home.resolve() / "personal" / "telegram" / "users" / USER_ID / "memory"
        assert context["home"] == str(stores.user_home.resolve())
        assert Path(context["memory_dir"]) == expected
        assert get_memory_dir() == expected
    finally:
        reset_personal_memory_context(token)
    # Nothing was written into the launch home's personal area.
    assert not (stores.launch / "personal").exists()


def test_unprofiled_dm_memory_dir_stays_in_the_launch_home(stores):
    # Contrast: the same seam with an unprofiled DM (runtime home = launch)
    # keeps the memory dir in the launch home's personal overlay.
    from pathlib import Path

    from gateway.telegram_personal_settings import (
        reset_personal_memory_context,
        set_personal_memory_context,
    )
    from tools.memory_tool import get_memory_dir

    adapter = _adapter(launch=stores.launch, user_home=stores.launch)
    home = adapter._settings_ui()._home(_source(profile=None))
    assert Path(home) == stores.launch.resolve()
    token = set_personal_memory_context(home, USER_ID)
    try:
        assert get_memory_dir() == stores.launch.resolve() / "personal" / "telegram" / "users" / USER_ID / "memory"
    finally:
        reset_personal_memory_context(token)


def test_profiled_dm_personal_skills_resolve_under_the_user_home(stores):
    # D11: under a profiled turn the personal skills dir is the user home's
    # migrated overlay, and it stays LAST in the search order — profile skills
    # first, so a personal skill can never shadow a shared profile skill.
    from pathlib import Path

    from agent.skill_utils import get_all_skills_dirs, get_personal_skills_dir
    from gateway.run import _profile_runtime_scope
    from gateway.telegram_personal_settings import (
        reset_personal_memory_context,
        set_personal_memory_context,
    )

    personal_skills = stores.user_home / "personal" / "telegram" / "users" / USER_ID / "skills"
    personal_skills.mkdir(parents=True)
    (stores.user_home / "skills").mkdir(exist_ok=True)
    adapter = _adapter(launch=stores.launch, user_home=stores.user_home)
    home = adapter._settings_ui()._home(_source(profile=f"tg-{USER_ID}"))
    token = set_personal_memory_context(home, USER_ID)
    try:
        with _profile_runtime_scope(stores.user_home):
            assert get_personal_skills_dir() == personal_skills.resolve()
            dirs = get_all_skills_dirs()
            assert dirs[0] == stores.user_home.resolve() / "skills"
            assert dirs.index(personal_skills.resolve()) == len(dirs) - 1
    finally:
        reset_personal_memory_context(token)


def test_host_scoped_commands_are_the_launch_owner_only_ones():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    # Host scope: gateway lifecycle, admission, global policy, host diagnostics,
    # web-auth pairing — the full explicit set. Telegram client/bot operations
    # are NOT host-scoped: they run for any admitted user.
    for name in ("update", "restart", "sync", "allow", "allowlist", "approve",
                 "approvals", "debug", "deny", "disallow", "egress", "guestbot",
                 "login", "pause", "platform", "reload-mcp", "reload-skills",
                 "sethome", "thread", "topic", "topup", "yolo", "auto",
                 "logs", "hf", "webauth"):
        assert adapter._is_host_scoped_command(name, "") is True, name
    for name in ("poll", "tgmessages", "tgfiles", "tginfo", "tgadmin", "tgprofile",
                 "managedbots", "botpeers", "b2b"):
        assert adapter._is_host_scoped_command(name, "") is False, name
    assert adapter._is_host_scoped_command("goal", "gate add") is True
    assert adapter._is_host_scoped_command("goal", "list") is False
    # Profile scope: the user's own skills, memory, model settings, own run.
    for name in ("skills", "memory", "memories", "msettings", "stop"):
        assert adapter._is_host_scoped_command(name, "") is False, name
    # Ordinary commands and plain text are not host-scoped.
    assert adapter._is_host_scoped_command("model", "") is False
    assert adapter._is_host_scoped_command(None, "") is False


def test_launch_owner_is_the_authorization_home_owner(stores):
    adapter = _adapter(launch=stores.launch, user_home=stores.user_home)
    assert adapter._is_launch_owner_user(OWNER_ID, _source()) is True
    assert adapter._is_launch_owner_user(USER_ID, _source()) is False
    assert adapter._is_launch_owner_user("not-an-id", _source()) is False
    assert adapter._is_launch_owner_user(OWNER_ID, None) is False


def _fallthrough_adapter(monkeypatch, backfill_env):
    """Bare adapter driving _process_inbound_event through the multiuser branch."""
    from unittest.mock import AsyncMock

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _authorization_home_for_source=lambda _source: backfill_env.launch,
        _resolve_profile_home_for_source=lambda _source: backfill_env.launch,
    )
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: backfill_env.launch)
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda *_args: None)
    monkeypatch.setattr(adapter, "_refresh_command_menu", AsyncMock())
    monkeypatch.setattr(adapter, "_prepare_attachment_intake", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    monkeypatch.setattr(adapter, "_cache_inbound_media", AsyncMock(return_value=([], [], "")))
    monkeypatch.setattr(adapter, "send", AsyncMock())
    monkeypatch.setattr(adapter, "send_reaction", AsyncMock())
    monkeypatch.setattr(adapter, "handle_message", AsyncMock())
    return adapter


def _fallthrough_source(profile):
    return SimpleNamespace(chat_id=USER_ID, user_id=USER_ID, user_name="Test User",
                           chat_type="dm", thread_id=None, profile=profile, is_bot=False)


@pytest.mark.asyncio
async def test_profiled_dm_text_falls_through_to_the_normal_turn(monkeypatch, backfill_env):
    from unittest.mock import AsyncMock

    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: backfill_env.store)
    adapter = _fallthrough_adapter(monkeypatch, backfill_env)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=_fallthrough_source(f"tg-{USER_ID}")))
    tenant = AsyncMock(return_value={"final_response": "tenant"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", tenant)
    for ui in ("_setup_ui", "_model_settings_ui", "_provider_ui", "_mcp_ui",
               "_plugin_ui", "_skills_ui"):
        monkeypatch.setattr(adapter, ui, lambda: SimpleNamespace(handle_message=AsyncMock(return_value=False)))
    monkeypatch.setattr(adapter, "_settings_ui", lambda: SimpleNamespace(
        handle_message=AsyncMock(return_value=False),
        _home=lambda _source: backfill_env.launch,
    ))

    message = SimpleNamespace(message="hello", id=1)
    await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    tenant.assert_not_called()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unprofiled_dm_text_stays_in_the_tenant_runtime(monkeypatch, backfill_env):
    from unittest.mock import AsyncMock

    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: backfill_env.store)
    adapter = _fallthrough_adapter(monkeypatch, backfill_env)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=_fallthrough_source(None)))
    tenant = AsyncMock(return_value={"final_response": "tenant"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", tenant)

    message = SimpleNamespace(message="hello", id=1)
    await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    tenant.assert_awaited_once()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_profiled_dm_host_command_is_denied_silently(monkeypatch, backfill_env):
    from unittest.mock import AsyncMock

    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: backfill_env.store)
    adapter = _fallthrough_adapter(monkeypatch, backfill_env)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=_fallthrough_source(f"tg-{USER_ID}")))
    tenant = AsyncMock(return_value={"final_response": "tenant"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", tenant)

    message = SimpleNamespace(message="/update", id=1)
    await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    tenant.assert_not_called()
    adapter.handle_message.assert_not_called()
    adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_profiled_dm_guestbot_stays_adapter_side(monkeypatch, backfill_env):
    from unittest.mock import AsyncMock

    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: backfill_env.store)
    adapter = _fallthrough_adapter(monkeypatch, backfill_env)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=_fallthrough_source(f"tg-{USER_ID}")))
    guestbot = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_guestbot_command", guestbot)
    tenant = AsyncMock(return_value={"final_response": "tenant"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", tenant)

    message = SimpleNamespace(message="/guestbot hello", id=1)
    await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    guestbot.assert_awaited_once()
    tenant.assert_not_called()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_attachment_follow_up_follows_the_profiled_dm(monkeypatch, backfill_env):
    from unittest.mock import AsyncMock

    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: backfill_env.store)
    adapter = _fallthrough_adapter(monkeypatch, backfill_env)
    tenant = AsyncMock(return_value={"final_response": "tenant"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    event = SimpleNamespace(text="attachment", message_id="1")

    await adapter._dispatch_authorized_event(event, _fallthrough_source(f"tg-{USER_ID}"))

    tenant.assert_not_called()
    adapter.handle_message.assert_awaited_once()

    await adapter._dispatch_authorized_event(event, _fallthrough_source(None))

    tenant.assert_awaited_once()
    assert adapter.handle_message.await_count == 1
