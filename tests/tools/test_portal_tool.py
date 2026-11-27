"""Tests for the F9c portal tool (loopback portal allocation).

Live-check shape, in the sandbox: a real SessionDB on a temp home backs
the portal registry (``state_meta``); open/list/close round-trip; the
allocated URL is proven reachable by serving a real HTTP server on the
allocated port and fetching the bearer URL with urllib (zero mocks of
the transport); in-use ports and duplicate allocations are refused; the
owner gate denies tenant scopes and non-owner DMs. All run zero LLM calls
and no network beyond loopback. Tests never write to ~/.hermes: every DB
and state file lives under tmp_path.
"""

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope
from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
from hermes_state import SessionDB
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.portal_tool import portal
from tools.registry import registry

# Imported for its registration side effect.
import tools.portal_tool  # registration side effect

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


def _call(agent, db, **args):
    return json.loads(registry.dispatch("portal", args, db=db, agent=agent))


def _free_port():
    """An ephemeral port that is free right now (bind then release)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _BearerHandler(BaseHTTPRequestHandler):
    """Serves 200 only to the bearer token minted for the portal."""

    token = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if query.get("token") == [self.token]:
            body = b"dev server ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"forbidden"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


# =========================================================================
# open / list / close
# =========================================================================


class TestPortalLifecycle:
    def test_open_list_close_round_trip(self, home_env, db):
        agent = _local_agent(db)
        port = _free_port()
        opened = _call(
            agent,
            db,
            action="open",
            port=port,
            title="App",
            description="Test app",
            path="/app",
        )
        assert opened["success"] is True, opened
        route = opened["portal"]
        assert route["port"] == port
        assert route["url"] == f"http://127.0.0.1:{port}/app"
        assert route["publicUrl"] == route["url"]
        assert route["exposure"] == {"kind": "loopback-only"}
        assert route["bearerToken"]
        assert route["bearerUrl"].startswith(route["url"])
        assert route["bearerToken"] in route["bearerUrl"]
        assert "Loopback-only" in opened["text"]
        assert "PUBLIC_URL=" in opened["text"]
        listed = _call(agent, db, action="list")
        assert listed["success"] is True
        assert len(listed["portals"]) == 1
        assert listed["portals"][0]["id"] == route["id"]
        assert "1 active portal" in listed["text"]
        closed = _call(agent, db, action="close", id=route["id"])
        assert closed["success"] is True
        assert closed["closed"] is True
        assert _call(agent, db, action="list")["portals"] == []

    def test_open_url_is_reachable_with_bearer(self, home_env, db):
        """The live check: a real server on the allocated port answers the
        bearer URL and refuses the bare URL."""
        agent = _local_agent(db)
        port = _free_port()
        opened = _call(agent, db, action="open", port=port)
        assert opened["success"] is True, opened
        route = opened["portal"]
        _BearerHandler.token = route["bearerToken"]
        server = ThreadingHTTPServer(("127.0.0.1", port), _BearerHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(route["bearerUrl"], timeout=5) as resp:
                assert resp.status == 200
                assert resp.read() == b"dev server ok"
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(route["url"], timeout=5)
            assert exc.value.code == 403
        finally:
            server.shutdown()
            server.server_close()

    def test_open_refuses_bad_input_and_busy_ports(self, home_env, db):
        agent = _local_agent(db)
        for bad in (
            {"action": "open"},
            {"action": "open", "port": 0},
            {"action": "open", "port": 65536},
            {"action": "open", "port": "8080"},
            {"action": "open", "port": _free_port(), "path": "no-slash"},
            {"action": "close"},
            {"action": "close", "id": "portal_nope"},
            {"action": "bogus"},
        ):
            result = _call(agent, db, **bad)
            assert result["success"] is False, bad
        # A port with a live listener is refused, as is a second allocation.
        port = _free_port()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        try:
            assert _call(agent, db, action="open", port=port)["success"] is False
        finally:
            listener.close()
        opened = _call(agent, db, action="open", port=port)
        assert opened["success"] is True, opened
        assert _call(agent, db, action="open", port=port)["success"] is False
        assert len(_call(agent, db, action="list")["portals"]) == 1


# =========================================================================
# owner gate
# =========================================================================


class TestPortalOwnerGate:
    def test_tenant_scope_denied(self, home_env, db):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="list")
            assert "error" in via_dispatch
            assert via_dispatch.get("success") is not True
            direct = json.loads(portal(db=db, agent=_local_agent(db)))
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
        agent = SimpleNamespace(
            platform="telegram", _session_db=db, session_id="s_main"
        )
        token = self._dm_context(home_env, OTHER_ID)
        try:
            result = json.loads(portal(db=db, agent=agent))
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)

    def test_dm_owner_allowed(self, home_env, db):
        from gateway.telegram_personal_settings import reset_personal_memory_context

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(
            platform="telegram", _session_db=db, session_id="s_main"
        )
        token = self._dm_context(home_env, OWNER_ID)
        try:
            result = json.loads(portal(db=db, agent=agent))
            assert result["success"] is True
            assert result["portals"] == []
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestPortalRegistration:
    def test_registered_in_portal_toolset_and_core_tools(self):
        assert registry.get_entry("portal") is not None
        assert "portal" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["portal"]["tools"]) == {"portal"}
        assert any(key == "portal" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_same_result_via_function_registry_and_inline_executor(self, home_env, db):
        agent = _local_agent(db)
        port = _free_port()
        direct = json.loads(portal(db=db, agent=agent, action="open", port=port))
        via_registry = json.loads(
            registry.get_entry("portal").handler({"action": "list"}, db=db, agent=agent)
        )
        via_inline = json.loads(
            INLINE_TOOL_EXECUTORS["portal"](agent, {"action": "list"}, _ctx())
        )
        assert direct["portal"]["id"] == via_registry["portals"][0]["id"]
        assert via_registry["portals"] == via_inline["portals"]
        assert "portal" in INLINE_TOOL_EXECUTORS

    def test_adapter_hides_portal_from_non_owner(self, home_env):
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
