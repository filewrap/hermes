from pathlib import Path
from types import SimpleNamespace

from gateway.mongo_multiuser import enabled, owner_user_id, runtime_ready
from plugins.platforms.telegram.multiuser import admitted, handle_command


def _rich_block_text(node) -> str:
    """Plain text of a RichMessage text node (TextConcat/TextPlain/TextFixed)."""
    if isinstance(node, str):
        return node
    if hasattr(node, "texts"):  # TextConcat
        return "".join(_rich_block_text(t) for t in node.texts)
    if hasattr(node, "text"):  # TextPlain / TextFixed / TextBold / …
        return _rich_block_text(node.text)
    return ""


class FakeStore:
    owner_user_id = "100"
    def __init__(self):
        self.access = {"100": {"allowed": True, "banned": False}}
        self.calls = []
    def get_access(self, uid):
        return self.access.get(str(uid), {"allowed": False, "banned": False})
    def allow_user(self, actor, target, allowed):
        self.calls.append(("allow", actor.user_id, target, allowed))
    def ban_user(self, actor, target, banned):
        self.calls.append(("ban", actor.user_id, target, banned))
    def set_group_settings(self, actor, group, settings):
        self.calls.append(("gsetting", actor.user_id, group, settings))
    def get_group_settings(self, actor, group):
        return {"_id": group, "name": "test"}


def test_mode_is_opt_in_and_owner_is_explicit(tmp_path):
    assert runtime_ready() is False
    (tmp_path / ".env").write_text("MULTIUSER_ENABLED=true\nOWNER_TELEGRAM_ID=100\n")
    assert enabled(tmp_path)
    assert owner_user_id(tmp_path) == "100"
    (tmp_path / ".env").write_text("MULTIUSER_ENABLED=false\n")
    assert not enabled(tmp_path)
    try:
        owner_user_id(tmp_path)
    except RuntimeError as exc:
        assert "OWNER_TELEGRAM_ID" in str(exc)
    else:
        raise AssertionError("missing owner must fail closed")


def test_legacy_owner_variable_remains_readable(tmp_path):
    from gateway.mongo_multiuser import _profile_env, hf_runtime_environment
    (tmp_path / ".env").write_text("DEEP_OWNER_TELEGRAM_ID=100\n")
    assert _profile_env(tmp_path)["OWNER_TELEGRAM_ID"] == "100"
    assert owner_user_id(tmp_path) == "100"
    runtime = hf_runtime_environment(tmp_path, {"DEEP_OWNER_TELEGRAM_ID": "100"})
    assert runtime["_HERMES_HF_MULTIUSER_RUNTIME_POLICY"]


def test_mongo_admission_does_not_grant_owner_copy_or_bypass_ban():
    store = FakeStore()
    assert admitted(store, "100")
    assert not admitted(store, "200")
    store.access["200"] = {"allowed": True, "banned": True}
    assert not admitted(store, "200")


def test_allow_and_ban_are_owner_only_and_require_numeric_id():
    store = FakeStore()
    handled, _ = handle_command(store, "200", "/allow 300")
    assert handled and not store.calls
    handled, response = handle_command(store, "100", "/allow @alice")
    assert handled and "numeric" in response
    assert not store.calls
    assert handle_command(store, "100", "/allow 300")[0]
    assert handle_command(store, "100", "/ban 300")[0]
    assert store.calls == [("allow", "100", "300", True), ("ban", "100", "300", True)]


def test_gsetting_rejects_nonallowed_and_non_group_and_supports_allowed_group():
    store = FakeStore()
    assert "Access denied" in handle_command(store, "200", "/gsettings -10")[1]
    assert "group" in handle_command(store, "100", "/gsettings -10", is_group=False)[1]
    handled, _ = handle_command(store, "100", "/gsettings -10 name=Ops", is_group=True)
    assert handled
    assert store.calls[-1] == ("gsetting", "100", "-10", {"name": "Ops"})


def test_multiuser_callback_gate_never_uses_legacy_static_allowlist(monkeypatch, tmp_path):
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: tmp_path)
    adapter._is_sender_authorized = lambda *args, **kwargs: True
    owner = SimpleNamespace(user_id="100", chat_type="dm", chat_id="100", is_bot=False, thread_id=None)
    pending = SimpleNamespace(user_id="200", chat_type="dm", chat_id="200", is_bot=False, thread_id=None)
    assert adapter._authorized(owner) is True
    assert adapter._authorized(pending) is False


def test_gsetting_uses_authenticated_current_group_without_explicit_id():
    store = FakeStore()
    store.access["200"] = {"allowed": True, "banned": False}
    assert handle_command(store, "200", "/gsettings name=Ops", is_group=True, current_group_id="-10")[0]
    assert store.calls[-1] == ("gsetting", "200", "-10", {"name": "Ops"})
    before = list(store.calls)
    assert "only target this group" in handle_command(store, "200", "/gsettings -20 name=Other", is_group=True, current_group_id="-10")[1]
    assert store.calls == before


def test_route_failure_cannot_reenable_legacy_allowlist(tmp_path, monkeypatch):
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    def failed_route(_source):
        raise RuntimeError("unavailable route")
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=failed_route)
    adapter._is_sender_authorized = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy gate must not run"))
    assert adapter._authorized(SimpleNamespace(user_id="100")) is False


def test_unready_multiuser_transport_cannot_connect_or_read_credentials(monkeypatch):
    import asyncio
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: True)
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._credentials = lambda: (_ for _ in ()).throw(AssertionError("credentials must not be loaded"))
    assert asyncio.run(adapter.connect()) is False


def test_transport_start_is_private_idempotent_and_notifies_owner(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from telethon import types
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    store = FakeStore()
    starts = []
    def start(uid):
        starts.append(uid)
        return {"first_start": len(starts) == 1, "start_count": len(starts)}
    store.start_user = start
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: True)
    monkeypatch.setattr("gateway.mongo_multiuser.store_for_home", lambda _home: store)
    monkeypatch.setattr("gateway.mongo_multiuser.owner_user_id", lambda _home: "100")
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._inbound_tails = {}
    adapter._telegram_pending_receipts = {}
    adapter._client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value=types.InputUser(200, 123456)),
    )
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: tmp_path)
    source = SimpleNamespace(user_id="200", chat_type="dm", chat_id="200", thread_id=None)
    adapter._source = AsyncMock(return_value=source)
    adapter._bot_username = "thisbot"
    adapter.send = AsyncMock()
    # This policy-only fixture bypasses __init__; mock native transport and
    # profile peer resolution rather than requiring a real client/session cache.
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(100, 987654))
    adapter._send_start_welcome = AsyncMock()
    adapter._send_rich_ui = AsyncMock()
    adapter.build_source = lambda chat, **kwargs: SimpleNamespace(chat_id=chat, **kwargs)
    adapter.send_reaction = AsyncMock(return_value=True)
    event = SimpleNamespace(message=SimpleNamespace(message="/start", id=1))
    asyncio.run(adapter._on_new_message(event))
    asyncio.run(adapter._on_new_message(event))
    assert starts == ["200", "200"]
    assert adapter._send_start_welcome.await_count == 2
    assert adapter.send_reaction.await_count == 2
    adapter.send_reaction.assert_awaited_with("200", "1", "🥴")
    owner_notices = adapter._send_rich_ui.await_args_list
    assert len(owner_notices) == 2
    for call in owner_notices:
        owner_source, notice, markup = call.args
        assert owner_source.chat_id == "100" and owner_source.chat_type == "dm"
        assert markup is None
        # The owner notice is a native RichMessage now (not the old HTML
        # string): same content as blocks — user id + "started Naina." text
        # plus real Ban/Allow callback buttons.
        assert isinstance(notice, types.InputRichMessage)
        texts, callbacks = [], []
        for block in notice.blocks:
            if isinstance(block, types.PageBlockParagraph):
                texts.append(_rich_block_text(block.text))
            elif isinstance(block, types.PageBlockButtonRow):
                for button in block.buttons:
                    callbacks.append(button.type.data.decode("ascii"))
        assert "200" in "".join(texts) and "started Naina." in "".join(texts)
        assert "hm:admission:ban:200" in callbacks
        assert "hm:admission:allow:200" in callbacks
    source.chat_type, source.chat_id = "group", "-10"
    asyncio.run(adapter._on_new_message(event))
    assert len(starts) == 2
    source.chat_type, source.chat_id = "dm", "200"
    store.access["200"] = {"allowed": True, "banned": True}
    asyncio.run(adapter._on_new_message(event))
    assert len(starts) == 2
    store.access["200"] = {"allowed": False, "banned": False}
    event.message.message = "/start@otherbot"
    asyncio.run(adapter._on_new_message(event))
    assert len(starts) == 2


def test_revoke_and_unban_are_explicit_owner_only_actions():
    store = FakeStore()
    for command in ("/revoke 200", "/unban 200", "/allow 200 off", "/ban 200 off"):
        assert "Only the configured owner" in handle_command(store, "200", command)[1]
    assert store.calls == []
    for command in ("/revoke 200", "/unban 200", "/allow 200 off", "/ban 200 off"):
        assert handle_command(store, "100", command)[0]
    assert store.calls == [("allow", "100", "200", False), ("ban", "100", "200", False)] * 2


def test_invalid_group_boolean_cannot_silently_disable_group():
    store = FakeStore()
    response = handle_command(store, "100", "/gsettings enabled=nonsense", is_group=True, current_group_id="-10")[1]
    assert "enabled=true" in response
    assert not store.calls


def test_runtime_readiness_names_the_failure_stage(tmp_path, monkeypatch):
    """The old bare ``except Exception: return False`` made a dead Mongo
    store, an unsafe activation marker, and a missing owner all look like
    "multiuser simply isn't ready". runtime_readiness names the stage."""
    from gateway.mongo_multiuser import runtime_readiness

    ready, reason = runtime_readiness(tmp_path)
    assert ready is False
    assert "OWNER_TELEGRAM_ID" in reason

    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    ready, reason = runtime_readiness(tmp_path)
    assert ready is True
    assert reason == ""

    from agent.mongo_tenants import TenantStoreError
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda _home: True)
    monkeypatch.setattr(
        "gateway.mongo_multiuser.store_for_home",
        lambda _home: (_ for _ in ()).throw(TenantStoreError("Cannot initialize tenant storage")),
    )
    ready, reason = runtime_readiness(tmp_path)
    assert ready is False
    assert "Mongo tenant store unavailable" in reason
    assert "Cannot initialize tenant storage" in reason

    monkeypatch.setattr(
        "agent.storage_mode.mongo_primary_enabled",
        lambda _home: (_ for _ in ()).throw(RuntimeError("marker unsafe")),
    )
    ready, reason = runtime_readiness(tmp_path)
    assert ready is False
    assert "activation marker" in reason
    assert "RuntimeError" in reason


def test_store_for_home_logs_mongo_construction_failure(tmp_path, monkeypatch, caplog):
    """Every tenant consumer funnels through store_for_home; a Mongo
    construction failure there must leave a breadcrumb in the logs, not
    surface only as a silent False or a bare raise elsewhere."""
    import logging

    from agent.mongo_tenants import TenantStoreError

    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda _home: True)

    def dead_init(self, *args, **kwargs):
        raise TenantStoreError("Cannot initialize tenant storage")

    monkeypatch.setattr("agent.mongo_tenants.MongoTenantStore.__init__", dead_init)
    with caplog.at_level(logging.WARNING, logger="gateway.mongo_multiuser"):
        try:
            from gateway.mongo_multiuser import store_for_home
            store_for_home(tmp_path)
        except TenantStoreError:
            pass
        else:
            raise AssertionError("dead Mongo store must still raise")
    assert "Mongo tenant store unavailable (Cannot initialize tenant storage)" in caplog.text


def test_connect_block_logs_the_readiness_reason(monkeypatch, caplog):
    """The boot gate used to log only 'isolated runtime integration is
    incomplete'. A blocked connect now says WHY the store is not ready."""
    import asyncio
    import logging

    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: True)
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._credentials = lambda: (_ for _ in ()).throw(AssertionError("credentials must not be loaded"))
    with caplog.at_level(logging.ERROR, logger="plugins.platforms.telegram.telethon_adapter"):
        assert asyncio.run(adapter.connect()) is False
    assert "tenant store not ready" in caplog.text
    assert "OWNER_TELEGRAM_ID" in caplog.text


def test_connect_logs_the_isolation_backend(monkeypatch, caplog):
    """Boot now logs the activation signal: which backend serves tenant
    isolation, or that it is inactive because no owner is configured."""
    import asyncio
    import logging

    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    for primary, label in ((True, "MongoDB primary"), (False, "local store")):
        monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: True)
        monkeypatch.setattr("gateway.mongo_multiuser.runtime_readiness", lambda _home: (True, ""))
        monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda _home: primary)
        adapter = TelethonAdapter.__new__(TelethonAdapter)
        adapter._connect_lock = asyncio.Lock()
        adapter._client = None
        adapter._credentials = lambda: (_ for _ in ()).throw(RuntimeError("stop here"))
        with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.telethon_adapter"):
            # connect() reports the credential failure and returns False; the
            # gate was passed and the activation line is what this asserts.
            assert asyncio.run(adapter.connect()) is False
        assert f"isolation active ({label})" in caplog.text

    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: False)
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._connect_lock = asyncio.Lock()
    adapter._client = None
    adapter._credentials = lambda: (_ for _ in ()).throw(RuntimeError("stop here"))
    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.telethon_adapter"):
        assert asyncio.run(adapter.connect()) is False
    assert "isolation inactive: no OWNER_TELEGRAM_ID configured" in caplog.text
