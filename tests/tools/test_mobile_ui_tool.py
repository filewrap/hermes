"""Tests for the F9e mobile_ui tool (observe/act on a paired Android device).

Live-check shape, in the sandbox: observe/act run over real HTTP against a
fake Android node server on loopback (real transport, no transport mocks).
The snapshot/confirmation/re-observe flow is exercised end to end, including
the sensitive-effect enrichment (a "Buy now" button proposes a purchase) and
the single-use snapshot rule. A real Android device with Accessibility
Control is still needed on the host — that live check is parked. All run zero
LLM calls and zero off-machine network fetches. Tests never write to
~/.hermes: every DB and state file lives under tmp_path.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
from hermes_state import SessionDB
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.mobile_ui_tool import mobile_ui
from tools.registry import registry

# Imported for its registration side effect.
import tools.mobile_ui_tool  # registration side effect
import tools.nodes_tool  # registration side effect (pairing helper uses it)

OWNER_ID = "8495104954"
OTHER_ID = "777"
SESSION = "s_main"
NODE_TOKEN = "node-secret-token"


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
    return json.loads(registry.dispatch("mobile_ui", args, db=db, agent=agent))


def _snapshot_doc(snapshot_id, observe_count):
    return {
        "snapshotId": snapshot_id,
        "package": "com.example.shop",
        "windowTitle": "Shop",
        "nodes": [
            {
                "ref": "root",
                "role": "window",
                "bounds": [0, 0, 1080, 2400],
                "flags": {
                    "clickable": False,
                    "editable": False,
                    "scrollable": True,
                    "enabled": True,
                    "focused": False,
                },
                "actions": [],
            },
            {
                "ref": "buy-btn",
                "parentRef": "root",
                "role": "button",
                "text": "Buy now",
                "bounds": [100, 200, 300, 400],
                "flags": {
                    "clickable": True,
                    "editable": False,
                    "scrollable": False,
                    "enabled": True,
                    "focused": False,
                },
                "actions": ["click"],
            },
            {
                "ref": "stale-btn",
                "parentRef": "root",
                "role": "button",
                "text": "Stale",
                "bounds": [100, 500, 300, 600],
                "flags": {
                    "clickable": True,
                    "editable": False,
                    "scrollable": False,
                    "enabled": True,
                    "focused": False,
                },
                "actions": ["click"],
            },
            {
                "ref": "search-box",
                "parentRef": "root",
                "role": "edit",
                "contentDescription": "Search",
                "bounds": [100, 700, 900, 800],
                "flags": {
                    "clickable": True,
                    "editable": True,
                    "scrollable": False,
                    "enabled": True,
                    "focused": False,
                },
                "actions": [],
            },
        ],
    }


class _FakeAndroidHandler(BaseHTTPRequestHandler):
    """A fake Android node: observe returns a fresh snapshot id each time."""

    protocol_version = "HTTP/1.1"
    observe_count = 0
    act_requests: list = []
    fail_observe_after_first = False

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path != "/invoke":
            return self._reply(404, {"error": "not found"})
        if self.headers.get("Authorization") != f"Bearer {NODE_TOKEN}":
            return self._reply(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        command = request.get("command")
        if command == "mobile.ui.observe":
            type(self).observe_count += 1
            if self.fail_observe_after_first and type(self).observe_count > 1:
                return self._reply(500, {"error": "screen off"})
            return self._reply(
                200,
                _snapshot_doc(
                    f"snap-{type(self).observe_count}", type(self).observe_count
                ),
            )
        if command == "mobile.ui.act":
            type(self).act_requests.append(request)
            action = (request.get("params") or {}).get("action") or {}
            if action.get("ref") == "stale-btn":
                return self._reply(200, {"code": "target_stale", "message": "gone"})
            return self._reply(200, {"code": "ok", "message": "done"})
        return self._reply(400, {"error": f"unknown command {command}"})

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_android():
    """A fake Android node server on loopback; yields (base_url, handler_cls)."""
    handler_cls = type("ScopedAndroidHandler", (_FakeAndroidHandler,), {})
    handler_cls.observe_count = 0
    handler_cls.act_requests = []
    handler_cls.fail_observe_after_first = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{server.server_address[1]}", handler_cls)
    finally:
        server.shutdown()
        server.server_close()


def _pair_android(
    agent, db, endpoint, name="pixel-1", platform="android", caps=None, commands=None
):
    requested = json.loads(
        registry.dispatch(
            "nodes",
            {
                "action": "request",
                "name": name,
                "platform": platform,
                "endpoint": endpoint,
                "token": NODE_TOKEN,
                "caps": caps if caps is not None else ["mobileUI"],
                "commands": commands
                if commands is not None
                else ["mobile.ui.observe", "mobile.ui.act"],
            },
            db=db,
            agent=agent,
        )
    )
    assert requested["ok"] is True, requested
    approved = json.loads(
        registry.dispatch(
            "nodes",
            {"action": "approve", "requestId": requested["request"]["requestId"]},
            db=db,
            agent=agent,
        )
    )
    assert approved["ok"] is True, approved
    return approved["node"]


def _observe(agent, db):
    result = _call(agent, db, action="observe")
    assert result["ok"] is True, result
    return result["snapshot"]


# =========================================================================
# observe / act
# =========================================================================


class TestMobileUiObserveAct:
    def test_observe_returns_snapshot(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        assert snapshot["snapshotId"] == "snap-1"
        assert snapshot["package"] == "com.example.shop"
        refs = [n["ref"] for n in snapshot["nodes"]]
        assert refs == ["root", "buy-btn", "stale-btn", "search-box"]

    def test_act_without_confirmation_proposes_purchase_effect(
        self, home_env, db, fake_android
    ):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "activate", "ref": "buy-btn"},
        )
        assert result["ok"] is True, result
        assert result["code"] == "confirmation_required"
        assert result["target"] == "Buy now"
        assert "purchase" in result["proposedEffect"]

    def test_act_with_confirmation_reobserves(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "activate", "ref": "buy-btn"},
            confirmed=True,
        )
        assert result["ok"] is True, result
        assert result["outcome"] == {"code": "ok", "message": "done"}
        assert result["snapshot"]["snapshotId"] == "snap-2"
        assert len(cls.act_requests) == 1
        assert cls.act_requests[0]["params"]["action"] == {
            "type": "activate",
            "ref": "buy-btn",
        }

    def test_snapshot_is_single_use(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        first = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "activate", "ref": "buy-btn"},
            confirmed=True,
        )
        assert first["ok"] is True, first
        second = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "activate", "ref": "buy-btn"},
            confirmed=True,
        )
        assert second["ok"] is False
        assert "snapshotId must match the latest observation" in second["error"]

    def test_act_with_wrong_snapshot_fails(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId="snap-nope",
            mobileAction={"type": "activate", "ref": "buy-btn"},
            confirmed=True,
        )
        assert result["ok"] is False
        assert "snapshotId must match" in result["error"]

    def test_set_text_needs_confirmation(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "set_text", "ref": "search-box", "text": "shoes"},
        )
        assert result["code"] == "confirmation_required"
        assert result["target"] == "Search"

    def test_tap_needs_confirmation_scroll_and_wait_do_not(
        self, home_env, db, fake_android
    ):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        tap = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "tap", "x": 150, "y": 250},
        )
        assert tap["code"] == "confirmation_required"
        assert tap["target"] == "coordinates (150, 250)"
        snapshot = _observe(agent, db)
        scroll = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "scroll", "ref": "root", "direction": "forward"},
        )
        assert scroll["ok"] is True, scroll
        assert scroll["outcome"]["code"] == "ok"
        snapshot = _observe(agent, db)
        wait = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "wait", "ms": 50},
        )
        assert wait["ok"] is True, wait

    def test_swipe_validation(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        bad = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={
                "type": "swipe",
                "x1": 0,
                "y1": 0,
                "x2": 10,
                "y2": 10,
                "durationMs": 0,
            },
        )
        assert bad["ok"] is False
        assert "durationMs" in bad["error"]
        too_long = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={
                "type": "swipe",
                "x1": 0,
                "y1": 0,
                "x2": 10,
                "y2": 10,
                "durationMs": 60001,
            },
        )
        assert too_long["ok"] is False

    def test_reobserve_outcome_asks_for_fresh_snapshot(
        self, home_env, db, fake_android
    ):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "activate", "ref": "stale-btn"},
            confirmed=True,
        )
        assert result["ok"] is True, result
        assert result["outcome"]["code"] == "target_stale"
        assert result["requiresReobserve"] is True
        assert "fresh snapshot" in result["instruction"]
        assert result["snapshot"]["snapshotId"] == "snap-2"

    def test_observe_failure_after_act_is_reported(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, cls = fake_android
        cls.fail_observe_after_first = True
        _pair_android(agent, db, endpoint)
        snapshot = _observe(agent, db)
        result = _call(
            agent,
            db,
            action="act",
            snapshotId=snapshot["snapshotId"],
            mobileAction={"type": "scroll", "ref": "root", "direction": "forward"},
        )
        assert result["ok"] is True, result
        assert result["outcome"]["code"] == "ok"
        assert result["requiresReobserve"] is True
        assert result["postconditionVerification"]["code"] == "observe_failed"


# =========================================================================
# eligibility
# =========================================================================


class TestMobileUiEligibility:
    def test_no_eligible_node(self, home_env, db):
        result = _call(_local_agent(db), db, action="observe")
        assert result["ok"] is False
        assert "no mobile-UI-capable device" in result["error"]

    def test_non_android_node_is_ineligible(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint, name="desk-1", platform="linux")
        result = _call(agent, db, action="observe")
        assert result["ok"] is False
        assert (
            "not a mobile-UI-capable device" in result["error"]
            or "no mobile-UI-capable device" in result["error"]
        )

    def test_node_without_mobile_ui_cap_is_ineligible(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint, caps=["camera"])
        result = _call(agent, db, action="observe")
        assert result["ok"] is False
        assert "no mobile-UI-capable device" in result["error"]

    def test_multiple_eligible_nodes_need_explicit_node(
        self, home_env, db, fake_android
    ):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint, name="pixel-1")
        _pair_android(agent, db, endpoint, name="pixel-2")
        result = _call(agent, db, action="observe")
        assert result["ok"] is False
        assert "multiple mobile-UI-capable devices" in result["error"]
        named = _call(agent, db, action="observe", node="pixel-2")
        assert named["ok"] is True, named


# =========================================================================
# owner gate
# =========================================================================


class TestMobileUiOwnerGate:
    def test_tenant_scope_denied(self, home_env, db, fake_android):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="observe")
            assert "error" in via_dispatch
            assert via_dispatch.get("ok") is not True
            direct = json.loads(mobile_ui(db=db, agent=_local_agent(db)))
            assert direct["success"] is False
            assert "owner only" in direct["error"]
        finally:
            reset_tenant_scope(token)

    def _dm_context(self, home, user_id):
        from gateway.telegram_personal_settings import set_personal_memory_context

        return set_personal_memory_context(home, user_id)

    def test_dm_non_owner_denied(self, home_env, db, fake_android):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(mobile_ui(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db, fake_android):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OWNER_ID)
        try:
            endpoint, _cls = fake_android
            _pair_android(agent, db, endpoint)
            result = json.loads(mobile_ui(db=db, agent=agent, action="observe"))
            assert result["ok"] is True
            assert result["snapshot"]["snapshotId"] == "snap-1"
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestMobileUiRegistration:
    def test_registry_toolset_config(self, home_env, db):
        assert registry.get_entry("mobile_ui") is not None
        assert "mobile_ui" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["mobile_ui"]["tools"]) == {"mobile_ui"}
        assert any(key == "mobile_ui" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_inline_executor_parity(self, home_env, db, fake_android):
        agent = _local_agent(db)
        endpoint, _cls = fake_android
        _pair_android(agent, db, endpoint)
        direct = _call(agent, db, action="observe")
        via_executor = json.loads(
            INLINE_TOOL_EXECUTORS["mobile_ui"](agent, {"action": "observe"}, _ctx())
        )
        # Each observe mints a fresh snapshot id; the node trees must match.
        assert via_executor["snapshot"]["nodes"] == direct["snapshot"]["nodes"]
        assert via_executor["snapshot"]["package"] == direct["snapshot"]["package"]

    def test_adapter_hides_mobile_ui_from_non_owner(self, home_env):
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
