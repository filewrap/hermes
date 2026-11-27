"""Tests for the F9a presence tool (live people/devices/recent activity).

Live-check shape, in the sandbox: real SessionDB rows on a temp home are
aggregated into people, the owner gate denies tenant scopes and non-owner
DMs (including a profiled user in their own DM — the launch owner is the
only owner), and the output carries no location or network data anywhere.
All run zero LLM calls and zero network fetches. Tests never write to
~/.hermes: every DB and state file lives under tmp_path.
"""
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
from hermes_state import SessionDB
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.presence_tool import presence
from tools.registry import registry

# Imported for its registration side effect.
import tools.presence_tool  # registration side effect

OWNER_ID = "8495104954"
OTHER_ID = "777"


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _local_agent(db):
    """CLI-surface agent: the operator at the keyboard is the owner."""
    return SimpleNamespace(platform="cli", _session_db=db, session_id="s_main")


def _ctx():
    return InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")


def _seed(db):
    """Telegram rows: Alice (2 DM sessions), Bob (1 group session), one
    /allowgc shared row (creator Alice, no user segment), one CLI row."""
    now = int(time.time())
    db.create_session("s_dm_alice_1", source="telegram", user_id=111,
                      session_key="agent:telegram:dm:111", chat_id=111,
                      chat_type="dm", display_name="Alice")
    db.create_session("s_dm_alice_2", source="telegram", user_id=111,
                      session_key="agent:telegram:dm:111", chat_id=111,
                      chat_type="dm", display_name="Alice")
    db.create_session("s_group_bob", source="telegram", user_id=222,
                      session_key="agent:telegram:group:-1001:222", chat_id=-1001,
                      chat_type="group", display_name="Bob")
    db.create_session("s_group_shared", source="telegram", user_id=111,
                      session_key="agent:telegram:group:-1001", chat_id=-1001,
                      chat_type="group", display_name="Shared")
    db.create_session("s_cli", source="cli", user_id=111)
    # last_active is a computed alias (freshest of last_activity_at /
    # started_at); seed the real column.
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (now - 600, "s_dm_alice_1"))
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (now - 3600, "s_dm_alice_2"))
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (now - 1800, "s_group_bob"))
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (now - 60, "s_group_shared"))
    db._conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (now - 60, "s_cli"))
    db._conn.commit()
    return now


def _call(agent, db, **args):
    return json.loads(registry.dispatch(
        "presence", args, db=db, agent=agent, current_session_id="s_main"))


def _assert_no_location_or_network(payload):
    """No location/network key anywhere, and no IPv4 literal in the text."""
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in {
                    "location", "network", "ip", "ip_address", "latitude",
                    "longitude", "city", "country", "region", "timezone",
                }, f"forbidden key {key!r} in presence output"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(payload)
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
                         json.dumps(payload))


# =========================================================================
# list: aggregation
# =========================================================================

class TestPresenceList:
    def test_aggregates_people_and_ignores_shared_and_cli_rows(self, home_env, db):
        _seed(db)
        result = _call(_local_agent(db), db)
        assert result["success"] is True
        assert result["status"] == "ok"
        assert result["totalPeople"] == 2
        assert result["truncated"] is False
        by_id = {p["id"]: p for p in result["people"]}
        alice = by_id["telegram:111"]
        assert alice["name"] == "Alice"
        assert alice["sessions"] == 2  # the shared row attributes to nobody
        assert alice["chats"] == ["dm"]
        assert alice["online"] is False
        assert alice["activity"]["source"] == "telegram-interaction"
        bob = by_id["telegram:222"]
        assert bob["name"] == "Bob"
        assert bob["sessions"] == 1
        assert bob["chats"] == ["group"]
        assert result["devices"] == []  # no dashboard client ever attached
        assert result["gateway"] == {"alive": False, "lastHeartbeatAt": None}
        _assert_no_location_or_network(result)

    def test_limit_truncates_and_reports(self, home_env, db):
        _seed(db)
        result = _call(_local_agent(db), db, limit=1)
        assert result["totalPeople"] == 2
        assert len(result["people"]) == 1
        assert result["truncated"] is True

    def test_include_devices_adds_telegram_devices(self, home_env, db):
        _seed(db)
        plain = _call(_local_agent(db), db)
        assert all(p["deviceCount"] == 0 and "devices" not in p
                   for p in plain["people"])
        with_devices = _call(_local_agent(db), db, include=["devices"])
        assert all(p["deviceCount"] == 1 for p in with_devices["people"])
        alice = next(p for p in with_devices["people"] if p["id"] == "telegram:111")
        assert alice["devices"] == [{
            "id": "telegram:111", "name": "Telegram", "kind": "client",
            "personIds": ["telegram:111"], "online": False,
            "activity": alice["activity"], "connections": [{"platform": "telegram"}],
        }]
        _assert_no_location_or_network(with_devices)

    def test_include_rejects_location_and_network(self, home_env, db):
        _seed(db)
        for bad in (["location"], ["network"], ["devices", "location"]):
            result = _call(_local_agent(db), db, include=bad)
            assert result["success"] is False
            assert "devices" in result["error"]

    def test_unknown_action_rejected(self, home_env, db):
        _seed(db)
        result = _call(_local_agent(db), db, action="bogus")
        assert result["success"] is False


# =========================================================================
# person / device actions
# =========================================================================

class TestPresencePersonDevice:
    def test_person_by_id_name_and_me(self, home_env, db):
        _seed(db)
        agent = _local_agent(db)
        by_id = _call(agent, db, action="person", person="telegram:111")
        assert by_id["status"] == "ok"
        assert by_id["people"][0]["name"] == "Alice"
        assert by_id["people"][0]["devices"][0]["name"] == "Telegram"
        by_name = _call(agent, db, action="person", person="Bob")
        assert by_name["status"] == "ok"
        assert by_name["people"][0]["id"] == "telegram:222"
        missing = _call(agent, db, action="person", person="Nobody")
        assert missing["status"] == "not-found"
        assert missing["people"] == []
        _assert_no_location_or_network(by_id)

    def test_person_ambiguous_on_duplicate_display_name(self, home_env, db):
        _seed(db)
        db.create_session("s_dm_sam_1", source="telegram", user_id=333,
                          session_key="agent:telegram:dm:333", chat_id=333,
                          chat_type="dm", display_name="Sam")
        db.create_session("s_dm_sam_2", source="telegram", user_id=444,
                          session_key="agent:telegram:dm:444", chat_id=444,
                          chat_type="dm", display_name="Sam")
        db._conn.commit()
        result = _call(_local_agent(db), db, action="person", person="Sam")
        assert result["status"] == "ambiguous"
        assert result["people"] == []

    def test_person_me_without_identity_is_unavailable(self, home_env, db):
        # Local surface, no OWNER_TELEGRAM_ID in the temp home: the
        # requester cannot be resolved to a Telegram id.
        _seed(db)
        result = _call(_local_agent(db), db, action="person", person="me")
        assert result["status"] == "identity-unavailable"

    def test_device_by_id(self, home_env, db):
        _seed(db)
        agent = _local_agent(db)
        found = _call(agent, db, action="device", deviceId="telegram:222")
        assert found["status"] == "ok"
        assert found["devices"][0]["name"] == "Telegram"
        assert found["devices"][0]["personIds"] == ["telegram:222"]
        missing = _call(agent, db, action="device", deviceId="telegram:999")
        assert missing["status"] == "not-found"
        assert missing["devices"] == []
        _assert_no_location_or_network(found)


# =========================================================================
# live signals: swarm cards, dashboard, gateway heartbeat
# =========================================================================

class TestPresenceLiveSignals:
    def test_swarm_card_marks_person_online(self, home_env, db, monkeypatch):
        now = _seed(db)
        card = SimpleNamespace(state="running",
                               route=SimpleNamespace(user_id="222"),
                               started_at=now - 120)
        observer = SimpleNamespace(cards={"c1": card})
        monkeypatch.setattr("gateway.swarm_observer.get_active_observer",
                            lambda: observer)
        result = _call(_local_agent(db), db)
        bob = next(p for p in result["people"] if p["id"] == "telegram:222")
        assert bob["online"] is True
        assert bob["onlineSince"] == (now - 120) * 1000
        alice = next(p for p in result["people"] if p["id"] == "telegram:111")
        assert alice["online"] is False
        assert "onlineSince" not in alice

    def test_stalled_swarm_card_is_not_live(self, home_env, db, monkeypatch):
        _seed(db)
        card = SimpleNamespace(state="stalled",
                               route=SimpleNamespace(user_id="222"),
                               started_at=time.time() - 10)
        monkeypatch.setattr("gateway.swarm_observer.get_active_observer",
                            lambda: SimpleNamespace(cards={"c1": card}))
        result = _call(_local_agent(db), db)
        bob = next(p for p in result["people"] if p["id"] == "telegram:222")
        assert bob["online"] is False

    def test_dashboard_device_online_then_stale(self, home_env, db):
        from gateway.scale_to_zero import (
            dashboard_client_heartbeat_path, touch_dashboard_client_heartbeat,
        )
        _seed(db)
        assert touch_dashboard_client_heartbeat() is True
        fresh = _call(_local_agent(db), db)
        assert fresh["devices"] == [{
            "id": "dashboard", "name": "Dashboard", "kind": "client",
            "personIds": [], "online": True,
            "activity": {"at": fresh["devices"][0]["activity"]["at"],
                         "source": "dashboard"},
            "connections": [{"platform": "web"}],
        }]
        os.utime(dashboard_client_heartbeat_path(), (time.time() - 600, time.time() - 600))
        stale = _call(_local_agent(db), db)
        assert stale["devices"][0]["online"] is False
        assert stale["devices"][0]["activity"]["source"] == "dashboard"

    def test_gateway_heartbeat_alive_then_stale(self, home_env, db):
        from datetime import UTC, datetime
        from gateway.shutdown_watchdog import get_loop_heartbeat_path
        from hermes_cli.profiles import get_profile_dir
        _seed(db)
        path = get_loop_heartbeat_path(get_profile_dir("default"))
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(time.time() - 30, tz=UTC).isoformat()
        path.write_text(json.dumps({"updated_at": stamp}))
        fresh = _call(_local_agent(db), db)
        assert fresh["gateway"]["alive"] is True
        assert fresh["gateway"]["lastHeartbeatAt"] is not None
        stamp = datetime.fromtimestamp(time.time() - 600, tz=UTC).isoformat()
        path.write_text(json.dumps({"updated_at": stamp}))
        stale = _call(_local_agent(db), db)
        assert stale["gateway"]["alive"] is False


# =========================================================================
# owner gate
# =========================================================================

class TestPresenceOwnerGate:
    def test_tenant_scope_denied_at_dispatch_and_handler(self, home_env, db):
        _seed(db)
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            # The tenant policy's dispatch denial is an {"error": ...} body
            # (no "success" key); the handler's own denial adds success=False.
            via_dispatch = _call(_local_agent(db), db)
            assert "error" in via_dispatch
            assert via_dispatch.get("success") is not True
            direct = json.loads(presence(db=db, agent=_local_agent(db)))
            assert direct["success"] is False
            assert "owner only" in direct["error"]
        finally:
            reset_tenant_scope(token)

    def _dm_context(self, home, user_id):
        from gateway.telegram_personal_settings import set_personal_memory_context
        return set_personal_memory_context(home, user_id)

    def test_dm_non_owner_denied(self, home_env, db):
        from gateway.telegram_personal_settings import reset_personal_memory_context
        _seed(db)
        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id="s_main")
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(presence(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db):
        from gateway.telegram_personal_settings import reset_personal_memory_context
        _seed(db)
        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id="s_main")
        token = self._dm_context(home_env, OWNER_ID)
        try:
            result = json.loads(presence(db=db, agent=agent))
            assert result["success"] is True
            assert result["status"] == "ok"
            owner = next(p for p in result["people"] if p["id"] == f"telegram:{OWNER_ID}")
            assert owner["online"] is True  # the requester's own turn is live
        finally:
            reset_personal_memory_context(token)

    def test_profiled_non_owner_denied_despite_own_profile_owner(self, home_env, db):
        """Regression: every tg-<id> profile stamps OWNER_TELEGRAM_ID=<its own
        user>, so comparing the DM user against the CONTEXT home's owner would
        pass any profiled user. The gate compares against the LAUNCH home."""
        from gateway.telegram_personal_settings import reset_personal_memory_context
        _seed(db)
        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        profile_home = home_env / "profiles" / f"tg-{OTHER_ID}"
        profile_home.mkdir(parents=True)
        (profile_home / ".env").write_text(f"OWNER_TELEGRAM_ID={OTHER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id="s_main")
        token = self._dm_context(profile_home, OTHER_ID)
        try:
            result = json.loads(presence(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================

class TestPresenceRegistration:
    def test_registered_in_presence_toolset_and_core_tools(self):
        assert registry.get_entry("presence") is not None
        assert "presence" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["presence"]["tools"]) == {"presence"}
        assert any(key == "presence" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_same_result_via_function_registry_and_inline_executor(self, home_env, db):
        _seed(db)
        agent = _local_agent(db)
        direct = json.loads(presence(db=db, agent=agent, current_session_id="s_main"))
        via_registry = json.loads(registry.get_entry("presence").handler(
            {}, db=db, agent=agent, current_session_id="s_main"))
        via_inline = json.loads(INLINE_TOOL_EXECUTORS["presence"](agent, {}, _ctx()))
        assert direct["people"] == via_registry["people"] == via_inline["people"]
        assert direct["totalPeople"] == via_registry["totalPeople"] == via_inline["totalPeople"]
        assert "presence" in INLINE_TOOL_EXECUTORS

    def test_adapter_hides_presence_from_non_owner(self, home_env):
        # Telethon lives in the messaging group, which the test env omits.
        pytest.importorskip("telethon")
        from gateway.config import PlatformConfig
        from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        adapter = TelethonAdapter(PlatformConfig())
        owner_source = SimpleNamespace(user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm")
        assert adapter.disabled_tools_for_source(owner_source) is None
        other_source = SimpleNamespace(user_id=OTHER_ID, chat_id=OTHER_ID, chat_type="dm")
        assert adapter.disabled_tools_for_source(other_source) == ["presence", "dashboard", "portal", "plugins", "gateway", "nodes", "mobile_ui", "telegram_trigger", "telegram_allow"]
        group_source = SimpleNamespace(user_id=OTHER_ID, chat_id="-1001", chat_type="group")
        assert adapter.disabled_tools_for_source(group_source) == ["presence", "dashboard", "portal", "plugins", "gateway", "nodes", "mobile_ui", "telegram_trigger", "telegram_allow"]
        no_sender = SimpleNamespace(user_id=None, chat_id="-1001", chat_type="group")
        assert adapter.disabled_tools_for_source(no_sender) == ["presence", "dashboard", "portal", "plugins", "gateway", "nodes", "mobile_ui", "telegram_trigger", "telegram_allow"]
