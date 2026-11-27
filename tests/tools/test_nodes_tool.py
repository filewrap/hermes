"""Tests for the F9e nodes tool (paired-device registry + invoke).

Live-check shape, in the sandbox: the pairing lifecycle runs against the
real registry on a temp home (state_meta write-through); invoke runs over
real HTTP against a fake node server on loopback (real transport, no
transport mocks) — including the ruled-out blocked command ``system.run``,
which must pass through, and a node refusal, which must surface as the
node's error. The host live check — a real paired node answering an invoke —
is parked: no Hermes node agent exists yet. All run zero LLM calls and zero
off-machine network fetches. Tests never write to ~/.hermes: every DB and
state file lives under tmp_path.
"""

import json
import threading
import time
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
from tools.nodes_tool import nodes
from tools.registry import registry

# Imported for its registration side effect.
import tools.nodes_tool  # registration side effect

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
    return json.loads(registry.dispatch("nodes", args, db=db, agent=agent))


class _FakeNodeHandler(BaseHTTPRequestHandler):
    """A node agent: bearer-checked, answers known commands, refuses fail.me."""

    protocol_version = "HTTP/1.1"
    EXPECTED_TOKEN = NODE_TOKEN
    FAIL_STATUS_COMMANDS = frozenset()

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
        if self.headers.get("Authorization") != f"Bearer {self.EXPECTED_TOKEN}":
            return self._reply(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        command = request.get("command")
        params = request.get("params") or {}
        answers = {
            "device.status": {"ok": True, "battery": 87},
            "device.info": {"model": "Test Phone", "os": "android"},
            "device.permissions": {"camera": "granted"},
            "device.health": {"ok": True},
            "camera.list": {"cameras": ["back", "front"]},
            "notifications.list": {"notifications": []},
            "location.get": {"lat": 1.0, "lon": 2.0},
            "system.notify": {"ok": True},
            "system.which": {
                "found": {name: "/bin/" + name for name in params.get("bins", [])}
            },
        }
        if command in self.FAIL_STATUS_COMMANDS:
            return self._reply(500, {"error": "boom"})
        if command == "fail.me":
            return self._reply(500, {"error": "boom"})
        if command in answers:
            return self._reply(200, answers[command])
        return self._reply(200, {"echo": command, "params": params})

    def log_message(self, *args):
        pass


class _WrongTokenNodeHandler(_FakeNodeHandler):
    """Same node, but it expects a token the registry never sends (401)."""

    EXPECTED_TOKEN = "other-token"


class _FailingStatusNodeHandler(_FakeNodeHandler):
    """Same node, but it refuses the diagnose probe itself (500)."""

    FAIL_STATUS_COMMANDS = frozenset({"device.status"})


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_node():
    """A fake node server on loopback; yields its base URL."""
    yield from _serve(_FakeNodeHandler)


@pytest.fixture
def fake_node_wrong_token():
    """A fake node that rejects the registry's token; yields its base URL."""
    yield from _serve(_WrongTokenNodeHandler)


@pytest.fixture
def fake_node_failing_status():
    """A fake node that refuses device.status; yields its base URL."""
    yield from _serve(_FailingStatusNodeHandler)


def _pair(
    agent, db, endpoint, name="phone-1", platform="android", caps=None, commands=None
):
    requested = _call(
        agent,
        db,
        action="request",
        name=name,
        platform=platform,
        endpoint=endpoint,
        token=NODE_TOKEN,
        caps=caps or ["mobileUI"],
        commands=commands or ["device.status"],
    )
    assert requested["ok"] is True, requested
    request = requested["request"]
    assert "token" not in request
    approved = _call(agent, db, action="approve", requestId=request["requestId"])
    assert approved["ok"] is True, approved
    return approved["node"]


# =========================================================================
# pairing lifecycle
# =========================================================================


class TestNodesPairing:
    def test_request_approve_status_describe_remove(self, home_env, db, fake_node):
        agent = _local_agent(db)
        assert _call(agent, db, action="status")["nodes"] == []
        assert _call(agent, db, action="pending")["pending"] == []
        node = _pair(agent, db, fake_node)
        assert node["pairingIdentity"]
        assert node["pairingGeneration"]
        assert node["connected"] is True
        assert "token" not in node
        status = _call(agent, db, action="status")
        assert [n["nodeId"] for n in status["nodes"]] == [node["nodeId"]]
        assert _call(agent, db, action="pending")["pending"] == []
        described = _call(agent, db, action="describe", node="phone-1")
        assert described["ok"] is True, described
        assert described["node"]["nodeId"] == node["nodeId"]
        assert "token" not in described["node"]
        removed = _call(agent, db, action="remove", node=node["nodeId"])
        assert removed["ok"] is True, removed
        assert _call(agent, db, action="status")["nodes"] == []

    def test_reject_removes_pending_request(self, home_env, db, fake_node):
        agent = _local_agent(db)
        requested = _call(
            agent,
            db,
            action="request",
            name="phone-2",
            platform="android",
            endpoint=fake_node,
            token=NODE_TOKEN,
        )
        request_id = requested["request"]["requestId"]
        rejected = _call(agent, db, action="reject", requestId=request_id)
        assert rejected["ok"] is True, rejected
        assert _call(agent, db, action="pending")["pending"] == []

    def test_request_validation(self, home_env, db, fake_node):
        agent = _local_agent(db)
        assert (
            _call(
                agent,
                db,
                action="request",
                name="x",
                endpoint="ftp://x",
                token=NODE_TOKEN,
            )["ok"]
            is False
        )
        assert (
            _call(agent, db, action="request", name="x", endpoint=fake_node, token="")[
                "ok"
            ]
            is False
        )
        assert (
            _call(
                agent,
                db,
                action="request",
                name="",
                endpoint=fake_node,
                token=NODE_TOKEN,
            )["ok"]
            is False
        )

    def test_approve_unknown_request_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="approve", requestId="pairreq_nope")
        assert result["ok"] is False
        assert "No pending pairing request" in result["error"]

    def test_connected_is_live_from_last_seen(self, home_env, db, fake_node):
        from gateway.gateway_services import read_service_json, write_service_json
        from gateway.node_registry import _NODES_KEY

        agent = _local_agent(db)
        node = _pair(agent, db, fake_node)
        stored = read_service_json(db, _NODES_KEY)
        stored["nodes"][node["nodeId"]]["lastSeenAt"] = (
            int(time.time() * 1000) - 601_000
        )
        write_service_json(db, _NODES_KEY, stored)
        status = _call(agent, db, action="status")
        assert status["nodes"][0]["connected"] is False

    def test_registry_survives_reload(self, home_env, db, fake_node):
        agent = _local_agent(db)
        node = _pair(agent, db, fake_node)
        # A fresh registry instance (new process) reads the same state_meta.
        status = _call(agent, db, action="status")
        assert [n["nodeId"] for n in status["nodes"]] == [node["nodeId"]]


# =========================================================================
# invoke
# =========================================================================


class TestNodesInvoke:
    def test_read_wrappers_hit_the_node(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        status = _call(agent, db, action="device_status", node="phone-1")
        assert status["ok"] is True, status
        assert status["result"] == {"ok": True, "battery": 87}
        info = _call(agent, db, action="device_info", node="phone-1")
        assert info["result"]["model"] == "Test Phone"

    def test_generic_invoke_echoes(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        result = _call(
            agent,
            db,
            action="invoke",
            node="phone-1",
            invokeCommand="custom.ping",
            params={"n": 1},
        )
        assert result["ok"] is True, result
        assert result["result"] == {"echo": "custom.ping", "params": {"n": 1}}

    def test_system_run_is_not_blocked(self, home_env, db, fake_node):
        # User ruling 2026-11-11: no hard-blocked invoke commands.
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        result = _call(
            agent,
            db,
            action="invoke",
            node="phone-1",
            invokeCommand="system.run",
            params={"argv": ["echo", "hi"]},
        )
        assert result["ok"] is True, result
        assert result["result"]["echo"] == "system.run"

    def test_node_refusal_surfaces_as_node_error(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        result = _call(
            agent, db, action="invoke", node="phone-1", invokeCommand="fail.me"
        )
        assert result["ok"] is False
        assert "HTTP 500" in result["error"]
        assert "boom" in result["error"]

    def test_invoke_unknown_node_fails(self, home_env, db, fake_node):
        result = _call(
            _local_agent(db),
            db,
            action="invoke",
            node="ghost",
            invokeCommand="device.status",
        )
        assert result["ok"] is False
        assert "No paired node" in result["error"]

    def test_invoke_offline_node_fails(self, home_env, db):
        agent = _local_agent(db)
        _pair(agent, db, "http://127.0.0.1:1")
        result = _call(
            agent, db, action="invoke", node="phone-1", invokeCommand="device.status"
        )
        assert result["ok"] is False
        assert "did not answer" in result["error"]

    def test_notify_requires_title_or_body(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        result = _call(
            agent, db, action="notify", node="phone-1", title="Hi", body="there"
        )
        assert result["ok"] is True, result
        assert _call(agent, db, action="notify", node="phone-1")["ok"] is False

    def test_which_and_location_wrappers(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        which = _call(agent, db, action="which", node="phone-1", bins=["ls"])
        assert which["result"] == {"found": {"ls": "/bin/ls"}}
        location = _call(agent, db, action="location_get", node="phone-1")
        assert location["result"] == {"lat": 1.0, "lon": 2.0}

    def test_unknown_action_fails(self, home_env, db):
        result = _call(_local_agent(db), db, action="camera_snap", node="phone-1")
        assert result["ok"] is False
        assert "Unknown nodes action" in result["error"]


# =========================================================================
# diagnose: one probe classified across route/auth/pairing/reconnect
# =========================================================================


def _steps_by_name(result):
    assert [s["name"] for s in result["steps"]] == [
        "route",
        "auth",
        "pairing",
        "reconnect",
    ]
    return {s["name"]: s for s in result["steps"]}


class TestNodesDiagnose:
    def test_healthy_node(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["ok"] is True, result
        assert result["verdict"] == "healthy"
        steps = _steps_by_name(result)
        assert all(s["state"] == "ok" for s in steps.values())
        assert result["probe"]["answered"] is True
        assert result["probe"]["http_status"] == 200
        assert result["probe"]["payload"] == {"ok": True, "battery": 87}
        assert "token" not in result["node"]
        assert result["next_step"]

    def test_diagnose_resolves_by_id(self, home_env, db, fake_node):
        agent = _local_agent(db)
        node = _pair(agent, db, fake_node)
        result = _call(agent, db, action="diagnose", node=node["nodeId"])
        assert result["verdict"] == "healthy"

    def test_stale_node_reconnected_by_probe(self, home_env, db, fake_node):
        from gateway.gateway_services import read_service_json, write_service_json
        from gateway.node_registry import _NODES_KEY

        agent = _local_agent(db)
        node = _pair(agent, db, fake_node)
        stored = read_service_json(db, _NODES_KEY)
        stored["nodes"][node["nodeId"]]["lastSeenAt"] = (
            int(time.time() * 1000) - 601_000
        )
        write_service_json(db, _NODES_KEY, stored)
        assert _call(agent, db, action="status")["nodes"][0]["connected"] is False
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["verdict"] == "healthy", result
        assert _steps_by_name(result)["reconnect"]["state"] == "ok"
        assert "stale" in _steps_by_name(result)["reconnect"]["detail"]
        assert result["node"]["connected"] is True
        assert _call(agent, db, action="status")["nodes"][0]["connected"] is True

    def test_unreachable_node(self, home_env, db):
        agent = _local_agent(db)
        _pair(agent, db, "http://127.0.0.1:1")
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["ok"] is True, result
        assert result["verdict"] == "unreachable"
        steps = _steps_by_name(result)
        assert steps["route"]["state"] == "failed"
        assert steps["auth"]["state"] == "unknown"
        assert steps["pairing"]["state"] == "ok"
        assert steps["reconnect"]["state"] == "failed"
        assert result["probe"]["answered"] is False
        assert result["probe"]["error"]

    def test_auth_failed_node(self, home_env, db, fake_node_wrong_token):
        agent = _local_agent(db)
        _pair(agent, db, fake_node_wrong_token)
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["verdict"] == "auth_failed", result
        steps = _steps_by_name(result)
        assert steps["route"]["state"] == "ok"
        assert steps["auth"]["state"] == "failed"
        assert "HTTP 401" in steps["auth"]["detail"]
        assert steps["pairing"]["state"] == "ok"
        assert steps["reconnect"]["state"] == "skipped"
        assert result["probe"]["http_status"] == 401

    def test_node_error(self, home_env, db, fake_node_failing_status):
        agent = _local_agent(db)
        _pair(agent, db, fake_node_failing_status)
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["verdict"] == "node_error", result
        steps = _steps_by_name(result)
        assert steps["route"]["state"] == "ok"
        assert steps["auth"]["state"] == "ok"
        assert steps["reconnect"]["state"] == "failed"
        assert "HTTP 500" in steps["reconnect"]["detail"]
        assert result["probe"]["http_status"] == 500

    def test_pairing_pending(self, home_env, db, fake_node):
        agent = _local_agent(db)
        requested = _call(
            agent,
            db,
            action="request",
            name="phone-1",
            platform="android",
            endpoint=fake_node,
            token=NODE_TOKEN,
        )
        request_id = requested["request"]["requestId"]
        result = _call(agent, db, action="diagnose", node="phone-1")
        assert result["ok"] is True, result
        assert result["verdict"] == "pairing_pending"
        assert result["requestId"] == request_id
        steps = _steps_by_name(result)
        assert steps["pairing"]["state"] == "pending"
        assert request_id in steps["pairing"]["detail"]
        assert steps["reconnect"]["state"] == "skipped"
        assert result["probe"] is None

    def test_not_paired(self, home_env, db):
        result = _call(_local_agent(db), db, action="diagnose", node="ghost")
        assert result["ok"] is True, result
        assert result["verdict"] == "not_paired"
        assert result["requestId"] is None
        steps = _steps_by_name(result)
        assert steps["pairing"]["state"] == "failed"
        assert steps["reconnect"]["state"] == "skipped"
        assert result["probe"] is None

    def test_diagnose_requires_node(self, home_env, db):
        result = _call(_local_agent(db), db, action="diagnose")
        assert result["ok"] is False
        assert "node is required" in result["error"]


# =========================================================================
# owner gate
# =========================================================================


class TestNodesOwnerGate:
    def test_tenant_scope_denied(self, home_env, db, fake_node):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="status")
            assert "error" in via_dispatch
            assert via_dispatch.get("ok") is not True
            direct = json.loads(nodes(db=db, agent=_local_agent(db)))
            assert direct["success"] is False
            assert "owner only" in direct["error"]
        finally:
            reset_tenant_scope(token)

    def _dm_context(self, home, user_id):
        from gateway.telegram_personal_settings import set_personal_memory_context

        return set_personal_memory_context(home, user_id)

    def test_dm_non_owner_denied(self, home_env, db, fake_node):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(nodes(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db, fake_node):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = self._dm_context(home_env, OWNER_ID)
        try:
            result = json.loads(nodes(db=db, agent=agent))
            assert result["ok"] is True
            assert result["nodes"] == []
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestNodesRegistration:
    def test_registry_toolset_config(self, home_env, db):
        assert registry.get_entry("nodes") is not None
        assert "nodes" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["nodes"]["tools"]) == {"nodes"}
        assert any(key == "nodes" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_inline_executor_parity(self, home_env, db, fake_node):
        agent = _local_agent(db)
        _pair(agent, db, fake_node)
        direct = _call(agent, db, action="status")
        via_executor = json.loads(
            INLINE_TOOL_EXECUTORS["nodes"](agent, {"action": "status"}, _ctx())
        )
        assert via_executor["nodes"] == direct["nodes"]

    def test_adapter_hides_nodes_from_non_owner(self, home_env):
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
