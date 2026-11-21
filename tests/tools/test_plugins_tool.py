"""Tests for the F9d plugins tool (plugin lifecycle over the Hermes plugin machinery).

Live-check shape, in the sandbox: a real manifest-only plugin under the temp
home's plugins dir backs list/inspect/enable/disable/reload/remove (config
writes go through the real ``save_config_value`` seam; activation runs the
real ``activate_plugin_now``, which reports a restart is required because no
gateway answers in the sandbox); search runs against the real in-tree
catalog; install of an unknown name fails at the catalog lookup. The real
git-clone install and the real git-pull update need network and are NOT run
here — install is covered to the catalog seam with the installer stubbed at
the binding the tool reads, and update is covered for its not-installed and
no-source failure reporting. All run zero LLM calls and zero network fetches.
Tests never write to ~/.hermes: every DB and state file lives under tmp_path.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
from hermes_state import SessionDB
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.plugins_tool import plugins
from tools.registry import registry

# Imported for its registration side effect.
import tools.plugins_tool  # registration side effect

OWNER_ID = "8495104954"
OTHER_ID = "777"
SESSION = "s_main"


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def home_only_env(tmp_path, monkeypatch):
    """HERMES_HOME only, Path.home untouched (see test_remove_deletes_plugin_tree)."""
    home = tmp_path / ".hermes-remove"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


@pytest.fixture
def fake_plugin(home_env):
    """A real installed plugin (manifest only) for lifecycle checks."""
    plugin_dir = home_env / "plugins" / "test-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: test-plugin\nversion: 1.2.3\ndescription: Test plugin for the plugins tool\n"
    )
    return plugin_dir


def _local_agent(db):
    """CLI-surface agent: the operator at the keyboard is the owner."""
    return SimpleNamespace(platform="cli", _session_db=db, session_id=SESSION)


def _ctx():
    return InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")


def _call(agent, db, **args):
    return json.loads(registry.dispatch("plugins", args, db=db, agent=agent))


# =========================================================================
# list / inspect / search
# =========================================================================


class TestPluginsListInspect:
    def test_list_shows_installed_plugin(self, home_env, db, fake_plugin):
        # Query-filtered: discovery lists every manifest (bundled + home) and
        # the list page is capped, so the home plugin needs its own filter.
        result = _call(_local_agent(db), db, action="list", query="test-plugin")
        assert result["ok"] is True, result
        assert result["total"] == 1
        entry = result["plugins"][0]
        assert entry["version"] == "1.2.3"
        assert entry["key"] == "test-plugin"
        assert entry["enabled"] is False

    def test_list_query_filters(self, home_env, db, fake_plugin):
        assert (
            _call(_local_agent(db), db, action="list", query="test-plugin")["total"]
            == 1
        )
        assert (
            _call(_local_agent(db), db, action="list", query="no-such-plugin")["total"]
            == 0
        )

    def test_inspect_returns_manifest(self, home_env, db, fake_plugin):
        result = _call(_local_agent(db), db, action="inspect", pluginId="test-plugin")
        assert result["ok"] is True, result
        assert result["installed"]["name"] == "test-plugin"
        assert result["installed"]["version"] == "1.2.3"

    def test_inspect_unknown_fails(self, home_env, db):
        result = _call(
            _local_agent(db), db, action="inspect", pluginId="no-such-plugin"
        )
        assert result["ok"] is False
        assert "not installed" in result["error"]

    def test_search_runs_against_real_catalog(self, home_env, db):
        result = _call(_local_agent(db), db, action="search", query="")
        assert result["ok"] is True, result
        assert result["total"] >= 1
        assert all("name" in r and "repo" in r for r in result["results"])

    def test_search_query_filters(self, home_env, db):
        result = _call(_local_agent(db), db, action="search", query="memory")
        assert result["ok"] is True, result
        for entry in result["results"]:
            haystack = (
                entry["name"] + entry["description"] + " ".join(entry["provides_tools"])
            ).lower()
            assert "memory" in haystack


# =========================================================================
# enable / disable / reload (real config writes, real activation)
# =========================================================================


class TestPluginsEnableDisableReload:
    def test_enable_writes_allow_list(self, home_env, db, fake_plugin):
        from hermes_cli.plugins_discovery import (
            _get_disabled_plugins,
            _get_enabled_plugins,
        )

        result = _call(_local_agent(db), db, action="enable", pluginId="test-plugin")
        assert result["ok"] is True, result
        assert result["plugin"] == "test-plugin"
        # No gateway answers in the sandbox, so activation reports a restart.
        assert result["restart_required"] is True
        assert "test-plugin" in (_get_enabled_plugins() or set())
        assert "test-plugin" not in _get_disabled_plugins()

    def test_disable_writes_deny_list(self, home_env, db, fake_plugin):
        from hermes_cli.plugins_discovery import (
            _get_disabled_plugins,
            _get_enabled_plugins,
        )

        _call(_local_agent(db), db, action="enable", pluginId="test-plugin")
        result = _call(_local_agent(db), db, action="disable", pluginId="test-plugin")
        assert result["ok"] is True, result
        assert result["gateway_reloaded"] is False
        assert result["hint"]
        assert "test-plugin" not in (_get_enabled_plugins() or set())
        assert "test-plugin" in _get_disabled_plugins()

    def test_reload_reports_restart_without_gateway(self, home_env, db, fake_plugin):
        result = _call(_local_agent(db), db, action="reload", pluginId="test-plugin")
        assert result["ok"] is True, result
        assert result["restart_required"] is True

    def test_enable_unknown_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="enable", pluginId="no-such-plugin")
        assert result["ok"] is False
        assert "not installed" in result["error"]


# =========================================================================
# install / update / remove
# =========================================================================


class TestPluginsInstallUpdateRemove:
    def test_install_unknown_name_fails_at_catalog(self, home_env, db):
        result = _call(
            _local_agent(db), db, action="install", pluginId="no-such-plugin"
        )
        assert result["ok"] is False
        assert "not in the Hermes plugin catalog" in result["error"]

    def test_install_resolves_catalog_entry(self, home_env, db, monkeypatch):
        """Catalog lookup + activation shape, with the clone itself stubbed at
        the binding the tool reads (a real clone needs network)."""
        import hermes_cli.plugins_cmd_catalog as catalog_mod

        from hermes_cli.plugin_catalog import load_catalog

        entries = load_catalog()
        assert entries, "in-tree catalog is empty"
        seen = {}

        def fake_install(entry, *, force, **kwargs):
            seen["entry"] = entry.name
            return (Path("/tmp/fake"), SimpleNamespace(name=entry.name), entry.name)

        monkeypatch.setattr(catalog_mod, "install_catalog_entry", fake_install)
        result = _call(_local_agent(db), db, action="install", pluginId=entries[0].name)
        assert result["ok"] is True, result
        assert seen["entry"] == entries[0].name
        assert result["plugin"] == entries[0].name

    def test_update_unknown_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="update", pluginId="no-such-plugin")
        assert result["ok"] is False
        assert "not installed" in result["error"]

    def test_update_manifest_only_plugin_reports_failure(
        self, home_env, db, fake_plugin
    ):
        # No git source behind the manifest: the real updater fails and the
        # tool reports it instead of raising.
        result = _call(_local_agent(db), db, action="update", pluginId="test-plugin")
        assert result["ok"] is False
        assert result["error"]

    def test_remove_deletes_plugin_tree(self, home_only_env, db):
        # HERMES_HOME only (no Path.home patch): the remove path's auth-store
        # seat-belt compares against the real Path.home, so patching it trips it.
        plugin_dir = home_only_env / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            "name: test-plugin\nversion: 1.2.3\ndescription: Test plugin for the plugins tool\n"
        )
        result = _call(_local_agent(db), db, action="remove", pluginId="test-plugin")
        assert result["ok"] is True, result
        assert not plugin_dir.exists()
        names = [
            e["name"] for e in _call(_local_agent(db), db, action="list")["plugins"]
        ]
        assert "test-plugin" not in names

    def test_remove_unknown_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="remove", pluginId="no-such-plugin")
        assert result["ok"] is False
        assert result["error"]

    def test_unknown_action_names_reload_and_resume_gap(self, home_env, db):
        result = _call(
            _local_agent(db), db, action="reloadAndResume", pluginId="test-plugin"
        )
        assert result["ok"] is False
        assert "reloadAndResume" in result["error"]


# =========================================================================
# owner gate
# =========================================================================


class TestPluginsOwnerGate:
    def test_tenant_scope_denied(self, home_env, db, fake_plugin):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="list")
            assert "error" in via_dispatch
            assert via_dispatch.get("ok") is not True
            direct = json.loads(plugins(db=db, agent=_local_agent(db)))
            assert direct["success"] is False
            assert "owner only" in direct["error"]
        finally:
            reset_tenant_scope(token)

    def _dm_context(self, home, user_id):
        from gateway.telegram_personal_settings import set_personal_memory_context

        return set_personal_memory_context(home, user_id)

    def test_dm_non_owner_denied(self, home_env, db, fake_plugin):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(plugins(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db, fake_plugin):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OWNER_ID)
        try:
            result = json.loads(plugins(db=db, agent=agent, query="test-plugin"))
            assert result["ok"] is True
            assert result["total"] == 1
            assert result["plugins"][0]["name"] == "test-plugin"
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestPluginsRegistration:
    def test_registry_toolset_config(self, home_env, db):
        assert registry.get_entry("plugins") is not None
        assert "plugins" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["plugins"]["tools"]) == {"plugins"}
        assert any(key == "plugins" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_inline_executor_parity(self, home_env, db, fake_plugin):
        agent = _local_agent(db)
        args = {"action": "list", "query": "test-plugin"}
        direct = _call(agent, db, **args)
        via_executor = json.loads(INLINE_TOOL_EXECUTORS["plugins"](agent, args, _ctx()))
        assert via_executor["total"] == direct["total"] == 1
        assert via_executor["plugins"][0]["name"] == "test-plugin"

    def test_adapter_hides_plugins_from_non_owner(self, home_env):
        pytest.importorskip("telethon")
        from gateway.config import PlatformConfig
        from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        adapter = TelethonAdapter(PlatformConfig())
        owner_source = SimpleNamespace(
            user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm"
        )
        assert adapter.disabled_tools_for_source(owner_source) is None
        other_source = SimpleNamespace(
            user_id=OTHER_ID, chat_id=OTHER_ID, chat_type="dm"
        )
        assert adapter.disabled_tools_for_source(other_source) == [
            "presence",
            "dashboard",
            "portal",
            "plugins",
            "gateway",
            "nodes",
            "mobile_ui",
        ]
