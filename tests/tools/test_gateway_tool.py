"""Tests for the F9d gateway tool (self-update, restart, config read).

Live-check shape, in the sandbox: config.get / config.schema.lookup run
against the real config loader on the temp home (a secret value written
through the real config writer comes back redacted, never in the clear);
update.run spawns a fake ``hermes`` executable on PATH and proves the real
detached-spawn path (marker file + log file, no network); update.cleanup
prunes real snapshot directories under the temp home; restart reports
unavailable because no gateway answers in the sandbox. The real update
(git fetch + apply) and the real gateway restart need a live host gateway and
are NOT run here. All run zero LLM calls and zero network fetches. Tests
never write to ~/.hermes: every DB and state file lives under tmp_path.
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
from tools.gateway_tool import gateway
from tools.registry import registry

# Imported for its registration side effect.
import tools.gateway_tool  # registration side effect

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
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _local_agent(db):
    """CLI-surface agent: the operator at the keyboard is the owner."""
    return SimpleNamespace(platform="cli", _session_db=db, session_id=SESSION)


def _ctx():
    return InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")


def _call(agent, db, **args):
    return json.loads(registry.dispatch("gateway", args, db=db, agent=agent))


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    """A fake ``hermes`` executable on PATH: records its argv, prints a line."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "hermes-argv.txt"
    script = bin_dir / "hermes"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\necho "fake hermes $@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + "/usr/bin:/bin")
    return marker


def _snapshot_dir(home, snap_id):
    snap = home / "state-snapshots" / snap_id
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text(json.dumps({"id": snap_id}) + "\n")
    return snap


# =========================================================================
# config.get / config.schema.lookup
# =========================================================================


class TestGatewayConfig:
    def test_config_get_root(self, home_env, db):
        result = _call(_local_agent(db), db, action="config.get")
        assert result["ok"] is True, result
        assert "model" in result["value"]

    def test_config_get_path(self, home_env, db):
        result = _call(_local_agent(db), db, action="config.get", path="model")
        assert result["ok"] is True, result
        assert result["path"] == "model"

    def test_config_get_unknown_path_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="config.get", path="no.such.path")
        assert result["ok"] is False
        assert "No config value" in result["error"]

    def test_config_get_redacts_secrets(self, home_env, db):
        from cli import save_config_value

        assert (
            save_config_value(
                "auxiliary.title_generation.api_key", "sk-live-abcdef1234567890"
            )
            is True
        )
        result = _call(
            _local_agent(db),
            db,
            action="config.get",
            path="auxiliary.title_generation.api_key",
        )
        assert result["ok"] is True, result
        assert "sk-live-abcdef1234567890" not in result["value"]

    def test_schema_lookup_known_path(self, home_env, db):
        result = _call(
            _local_agent(db), db, action="config.schema.lookup", path="model"
        )
        assert result["ok"] is True, result
        assert result["type"] == "str"
        assert "default" in result
        assert result["present"] is True

    def test_schema_lookup_unknown_path_fails(self, home_env, db):
        result = _call(
            _local_agent(db), db, action="config.schema.lookup", path="no.such.path"
        )
        assert result["ok"] is False
        assert "not a known config path" in result["error"]

    def test_unknown_action_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="config.set", path="model")
        assert result["ok"] is False
        assert "Unknown gateway action" in result["error"]


# =========================================================================
# update.run / restart / update.cleanup
# =========================================================================


class TestGatewayUpdateRestartCleanup:
    def test_update_run_missing_exe_fails(self, home_env, db, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
        result = _call(_local_agent(db), db, action="update.run")
        assert result["ok"] is False
        assert "not found on PATH" in result["error"]

    @pytest.mark.live_system_guard_bypass
    def test_update_run_spawns_hermes_update(self, home_env, db, fake_hermes):
        from tests.waiting import wait_until

        result = _call(_local_agent(db), db, action="update.run")
        assert result["ok"] is True, result
        assert result["status"] == "started"
        assert result["pid"]
        assert Path(result["log"]).is_file()
        wait_until(lambda: fake_hermes.exists() and "update" in fake_hermes.read_text())

    @pytest.mark.live_system_guard_bypass
    def test_update_run_dry_run_checks(self, home_env, db, fake_hermes):
        result = _call(_local_agent(db), db, action="update.run", dryRun=True)
        assert result["ok"] is True, result
        assert result["status"] == "checked"
        assert "update --check" in result["output"]
        assert "update --check" in fake_hermes.read_text()

    def test_restart_unavailable_without_gateway(self, home_env, db):
        result = _call(_local_agent(db), db, action="restart")
        assert result["ok"] is False
        assert "unavailable" in result["error"]

    def test_cleanup_dry_run_previews(self, home_env, db):
        _snapshot_dir(home_env, "20260101-000000-aaa")
        _snapshot_dir(home_env, "20260102-000000-bbb")
        result = _call(_local_agent(db), db, action="update.cleanup", dryRun=True)
        assert result["ok"] is True, result
        assert result["dryRun"] is True
        assert result["would_remove_snapshots"] == ["20260101-000000-aaa"]
        assert result["keep_snapshot"] == "20260102-000000-bbb"
        assert (home_env / "state-snapshots" / "20260101-000000-aaa").is_dir()

    def test_cleanup_prunes_older_snapshots(self, home_env, db):
        _snapshot_dir(home_env, "20260101-000000-aaa")
        kept = _snapshot_dir(home_env, "20260102-000000-bbb")
        result = _call(_local_agent(db), db, action="update.cleanup")
        assert result["ok"] is True, result
        assert result["removed_snapshots"] == 1
        assert not (home_env / "state-snapshots" / "20260101-000000-aaa").exists()
        assert kept.is_dir()

    def test_cleanup_empty_is_a_noop(self, home_env, db):
        result = _call(_local_agent(db), db, action="update.cleanup")
        assert result["ok"] is True, result
        assert result["removed_snapshots"] == 0


# =========================================================================
# owner gate
# =========================================================================


class TestGatewayOwnerGate:
    def test_tenant_scope_denied(self, home_env, db):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="config.get")
            assert "error" in via_dispatch
            assert via_dispatch.get("ok") is not True
            direct = json.loads(
                gateway(db=db, agent=_local_agent(db), action="config.get")
            )
            assert direct["success"] is False
            assert "owner only" in direct["error"]
        finally:
            reset_tenant_scope(token)

    def _dm_context(self, home, user_id):
        from gateway.telegram_personal_settings import set_personal_memory_context

        return set_personal_memory_context(home, user_id)

    def test_dm_non_owner_denied(self, home_env, db):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(gateway(db=db, agent=agent, action="config.get"))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OWNER_ID)
        try:
            result = json.loads(gateway(db=db, agent=agent, action="config.get"))
            assert result["ok"] is True
            assert "model" in result["value"]
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestGatewayRegistration:
    def test_registry_toolset_config(self, home_env, db):
        assert registry.get_entry("gateway") is not None
        assert "gateway" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["gateway"]["tools"]) == {"gateway"}
        assert any(key == "gateway" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_inline_executor_parity(self, home_env, db):
        agent = _local_agent(db)
        direct = _call(agent, db, action="config.get", path="model")
        via_executor = json.loads(
            INLINE_TOOL_EXECUTORS["gateway"](
                agent, {"action": "config.get", "path": "model"}, _ctx()
            )
        )
        assert via_executor["ok"] is True
        assert via_executor["value"] == direct["value"]

    def test_adapter_hides_gateway_from_non_owner(self, home_env):
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
