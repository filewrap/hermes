"""Tests for the F9b dashboard tool (session boards: tabs + plugin widgets)
and the F10 dashboard half (widget_put of pre-made template widgets +
widget_list).

Live-check shape, in the sandbox: a real SessionDB on a temp home backs the
board store (``state_meta``); tab/widget ops round-trip with revision bumps;
invalid ops fail without writing; widget_put trusts only installed,
enabled plugins (a real manifest under the temp home's plugins dir) and
only kit-validated template data; the owner gate denies tenant scopes and
non-owner DMs; focus_tab / set_presentation report unavailable (no
dashboard push channel). All run zero LLM calls and zero network fetches.
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
from tools.dashboard_tool import dashboard
from tools.registry import registry

# Imported for its registration side effect.
import tools.dashboard_tool  # registration side effect

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


@pytest.fixture
def widget_plugin(home_env):
    """A real installed plugin (manifest only) for widget_put trust checks."""
    plugin_dir = home_env / "plugins" / "test-widget"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: test-widget\nversion: 1.0.0\ndescription: Test widget plugin\n"
    )
    return plugin_dir


def _local_agent(db):
    """CLI-surface agent: the operator at the keyboard is the owner."""
    return SimpleNamespace(platform="cli", _session_db=db, session_id=SESSION)


def _ctx():
    return InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")


def _call(agent, db, **args):
    return json.loads(
        registry.dispatch(
            "dashboard", args, db=db, agent=agent, current_session_id=SESSION
        )
    )


def _board(agent, db, **args):
    result = _call(agent, db, **args)
    assert result["success"] is True, result
    return result["snapshot"]


# =========================================================================
# read: empty board, default presentation
# =========================================================================


class TestDashboardRead:
    def test_fresh_session_reads_empty_board(self, home_env, db):
        snap = _board(_local_agent(db), db, action="read")
        assert snap == {
            "sessionKey": SESSION,
            "revision": 0,
            "tabs": [],
            "widgets": [],
            "defaultPresentation": "split",
        }

    def test_read_is_scoped_per_session(self, home_env, db):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        other = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "read"},
                db=db,
                agent=agent,
                current_session_id="s_other",
            )
        )
        assert other["snapshot"]["tabs"] == []
        assert other["snapshot"]["revision"] == 0

    def test_unknown_action_rejected(self, home_env, db):
        result = _call(_local_agent(db), db, action="bogus")
        assert result["success"] is False


# =========================================================================
# tabs
# =========================================================================


class TestDashboardTabs:
    def test_tab_lifecycle_and_revision(self, home_env, db):
        agent = _local_agent(db)
        snap = _board(agent, db, action="tab_create", tabId="main", title="Main")
        assert snap["revision"] == 1
        assert snap["tabs"] == [{"tabId": "main", "title": "Main", "position": 0}]
        snap = _board(agent, db, action="tab_create", tabId="logs", title="Logs")
        assert snap["revision"] == 2
        assert [t["tabId"] for t in snap["tabs"]] == ["main", "logs"]
        snap = _board(
            agent, db, action="tab_update", tabId="logs", title="Logs!", position=0
        )
        assert [t["tabId"] for t in snap["tabs"]] == ["logs", "main"]
        assert snap["tabs"][0]["title"] == "Logs!"
        snap = _board(agent, db, action="tabs_reorder", tabIds=["main", "logs"])
        assert [t["tabId"] for t in snap["tabs"]] == ["main", "logs"]
        snap = _board(agent, db, action="tab_delete", tabId="logs")
        assert [t["tabId"] for t in snap["tabs"]] == ["main"]
        assert snap["revision"] == 5

    def test_tab_errors_do_not_write(self, home_env, db):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        for bad in (
            {"action": "tab_create", "tabId": "Main", "title": "Bad slug"},
            {"action": "tab_create", "tabId": "main", "title": "Duplicate"},
            {"action": "tab_create", "tabId": "x", "title": ""},
            {"action": "tab_update", "tabId": "nope", "title": "Ghost"},
            {"action": "tab_update", "tabId": "main"},
            {"action": "tab_delete", "tabId": "nope"},
            {"action": "tabs_reorder", "tabIds": ["main", "main"]},
            {"action": "tabs_reorder", "tabIds": ["main", "nope"]},
        ):
            result = _call(agent, db, **bad)
            assert result["success"] is False, bad
        snap = _board(agent, db, action="read")
        assert snap["revision"] == 1  # only the first create landed
        assert [t["tabId"] for t in snap["tabs"]] == ["main"]


# =========================================================================
# widgets
# =========================================================================


class TestDashboardWidgets:
    def _tabbed(self, agent, db):
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        _board(agent, db, action="tab_create", tabId="side", title="Side")

    def test_widget_put_create_update_move_resize_remove(
        self, home_env, db, widget_plugin
    ):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="report-a",
            pluginKind="test-widget:report",
            tabId="main",
            size="md",
            props={"title": "Weekly"},
        )
        assert snap["revision"] == 3
        widget = snap["widgets"][0]
        assert widget["name"] == "report-a"
        assert widget["tabId"] == "main"
        assert widget["contentKind"] == "plugin"
        assert widget["contentOwner"] == "plugin"
        assert widget["pluginKind"] == "test-widget:report"
        assert widget["sizeW"] == 6 and widget["sizeH"] == 4
        assert widget["grantState"] == "none"
        assert widget["revision"] == 1
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="report-b",
            pluginKind="test-widget:report",
            tabId="main",
            size="sm",
            after="report-a",
        )
        assert [w["name"] for w in snap["widgets"]] == ["report-a", "report-b"]
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="report-a",
            pluginKind="test-widget:report",
            tabId="main",
            size="lg",
        )
        updated = next(w for w in snap["widgets"] if w["name"] == "report-a")
        assert updated["revision"] == 2
        assert (updated["sizeW"], updated["sizeH"]) == (8, 6)
        snap = _board(agent, db, action="widget_move", name="report-b", tabId="side")
        moved = next(w for w in snap["widgets"] if w["name"] == "report-b")
        assert moved["tabId"] == "side"
        snap = _board(
            agent, db, action="widget_resize", name="report-b", sizeW=12, sizeH=8
        )
        resized = next(w for w in snap["widgets"] if w["name"] == "report-b")
        assert (resized["sizeW"], resized["sizeH"]) == (12, 8)
        snap = _board(agent, db, action="widget_remove", name="report-a")
        assert [w["name"] for w in snap["widgets"]] == ["report-b"]
        snap = _board(agent, db, action="tab_delete", tabId="side")
        assert snap["widgets"] == []  # deleting a tab drops its widgets

    def test_widget_put_trust_checks(self, home_env, db, widget_plugin):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        for bad in (
            {"action": "widget_put", "name": "w", "pluginKind": "nope"},
            {"action": "widget_put", "name": "w", "pluginKind": "ghost:report"},
            {
                "action": "widget_put",
                "name": "Bad Name",
                "pluginKind": "test-widget:report",
            },
            {
                "action": "widget_put",
                "name": "w",
                "pluginKind": "test-widget:report",
                "size": "huge",
            },
            {
                "action": "widget_put",
                "name": "w",
                "pluginKind": "test-widget:report",
                "props": {"blob": "x" * 9000},
            },
            {
                "action": "widget_put",
                "name": "w",
                "pluginKind": "test-widget:report",
                "tabId": "ghost",
            },
            {
                "action": "widget_put",
                "name": "w",
                "pluginKind": "test-widget:report",
                "after": "ghost",
            },
        ):
            result = _call(agent, db, **bad)
            assert result["success"] is False, bad
        snap = _board(agent, db, action="read")
        assert snap["widgets"] == []

    def test_widget_put_needs_a_tab(self, home_env, db, widget_plugin):
        result = _call(
            _local_agent(db),
            db,
            action="widget_put",
            name="w",
            pluginKind="test-widget:report",
        )
        assert result["success"] is False
        assert "tabId" in result["error"]

    def test_widget_move_position_or_after_not_both(self, home_env, db, widget_plugin):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        _board(
            agent,
            db,
            action="widget_put",
            name="w",
            pluginKind="test-widget:report",
            tabId="main",
        )
        result = _call(agent, db, action="widget_move", name="w", position=0, after="w")
        assert result["success"] is False


# =========================================================================
# template widgets (F10 dashboard half)
# =========================================================================


TEMPLATE_CASES = (
    (
        "metric",
        "Users",
        {
            "label": "Active users",
            "value": "1,204",
            "delta": "+8%",
            "note": "this week",
        },
    ),
    (
        "progress",
        "Migration",
        {
            "label": "Migration",
            "done": 3,
            "total": 5,
            "steps": [
                {"label": "Export", "done": True},
                {"label": "Import", "done": False},
            ],
        },
    ),
    (
        "checklist",
        "Launch",
        {
            "label": "Launch",
            "items": [
                {"label": "Tests", "done": True},
                {"label": "Docs", "done": False},
            ],
        },
    ),
    ("flow", "Flow", {"mermaid": "flowchart TD\n    A[Start] --> B[Done]"}),
    (
        "chart",
        "Signups",
        {"label": "Signups", "kind": "bar", "labels": ["Mon", "Tue"], "values": [3, 5]},
    ),
    (
        "status",
        "Services",
        {"label": "Services", "rows": [{"name": "api", "state": "ok"}]},
    ),
)


class TestTemplateWidgets:
    def _tabbed(self, agent, db):
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        _board(agent, db, action="tab_create", tabId="side", title="Side")

    def test_template_put_round_trips_every_template(self, home_env, db):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        for template, title, data in TEMPLATE_CASES:
            snap = _board(
                agent,
                db,
                action="widget_put",
                name=f"w-{template}",
                template=template,
                title=title,
                props=data,
                tabId="main",
                size="md",
            )
            widget = next(w for w in snap["widgets"] if w["name"] == f"w-{template}")
            assert widget["contentKind"] == "template"
            assert widget["contentOwner"] == "agent"
            assert widget["template"] == template
            assert widget["tabId"] == "main"
            assert widget["title"] == title
            assert widget["revision"] == 1
            assert "pluginKind" not in widget
        snap = _board(agent, db, action="read")
        assert len(snap["widgets"]) == len(TEMPLATE_CASES)
        assert snap["revision"] == 2 + len(TEMPLATE_CASES)

    def test_stored_template_spec_renders_like_the_dm_widget(self, home_env, db):
        from plugins.platforms.telegram.widget_kit import build_widget_html

        agent = _local_agent(db)
        self._tabbed(agent, db)
        for template, title, data in TEMPLATE_CASES:
            _board(
                agent,
                db,
                action="widget_put",
                name=f"w-{template}",
                template=template,
                title=title,
                props=data,
                tabId="main",
            )
        snap = _board(agent, db, action="read")
        for widget in snap["widgets"]:
            html = build_widget_html(
                widget["template"],
                widget.get("title") or widget["name"],
                widget["props"],
            )
            assert len(html) > 0
            assert widget["title"] in html

    def test_template_put_update_in_place_and_kind_switch(
        self, home_env, db, widget_plugin
    ):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="swap",
            template="metric",
            title="Users",
            props={"label": "Active users", "value": "10"},
            tabId="main",
        )
        assert snap["widgets"][0]["revision"] == 1
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="swap",
            template="progress",
            title="Migration",
            props={"label": "Migration", "done": 1, "total": 2},
            tabId="side",
            size="lg",
        )
        updated = snap["widgets"][0]
        assert updated["revision"] == 2
        assert updated["template"] == "progress"
        assert updated["tabId"] == "side"
        assert (updated["sizeW"], updated["sizeH"]) == (8, 6)
        assert updated["props"] == {
            "label": "Migration",
            "done": 1,
            "total": 2,
            "steps": [],
        }
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="swap",
            pluginKind="test-widget:report",
            tabId="main",
        )
        replugged = snap["widgets"][0]
        assert replugged["revision"] == 3
        assert replugged["contentKind"] == "plugin"
        assert replugged["pluginKind"] == "test-widget:report"
        assert "template" not in replugged
        snap = _board(
            agent,
            db,
            action="widget_put",
            name="swap",
            template="metric",
            title="Users",
            props={"label": "Active users", "value": "11"},
            tabId="main",
        )
        retoggled = snap["widgets"][0]
        assert retoggled["revision"] == 4
        assert retoggled["contentKind"] == "template"
        assert "pluginKind" not in retoggled
        snap = _board(agent, db, action="widget_remove", name="swap")
        assert snap["widgets"] == []

    def test_template_put_invalid_data_writes_nothing(
        self, home_env, db, widget_plugin
    ):
        agent = _local_agent(db)
        self._tabbed(agent, db)
        for bad in (
            {"action": "widget_put", "name": "w", "template": "bogus", "props": {}},
            {
                "action": "widget_put",
                "name": "w",
                "template": "metric",
                "props": {"label": "No value"},
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "progress",
                "props": {"label": "Over", "done": 6, "total": 5},
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "chart",
                "props": {
                    "label": "Bad",
                    "kind": "bar",
                    "labels": ["A"],
                    "values": [1, 2],
                },
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "flow",
                "props": {"mermaid": "hello world"},
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "flow",
                "props": {"mermaid": "x" * 4001},
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "metric",
                "pluginKind": "test-widget:report",
                "props": {"label": "Both", "value": "1"},
            },
            {
                "action": "widget_put",
                "name": "w",
                "props": {"label": "Neither", "value": "1"},
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "metric",
                "props": {"label": "Ghost tab", "value": "1"},
                "tabId": "ghost",
            },
            {
                "action": "widget_put",
                "name": "w",
                "template": "metric",
                "props": {"label": "Ghost anchor", "value": "1"},
                "tabId": "main",
                "after": "ghost",
            },
        ):
            result = _call(agent, db, **bad)
            assert result["success"] is False, bad
        snap = _board(agent, db, action="read")
        assert snap["revision"] == 2  # only the two tab creates landed
        assert snap["widgets"] == []

    def test_template_put_needs_a_tab(self, home_env, db):
        result = _call(
            _local_agent(db),
            db,
            action="widget_put",
            name="w",
            template="metric",
            props={"label": "No tab", "value": "1"},
        )
        assert result["success"] is False
        assert "tabId" in result["error"]

    def test_template_put_reaches_tool_via_inline_executor(self, home_env, db):
        # The inline executor forwards args by name: a template put through
        # it must land the template widget, not a bare plugin put or an error.
        agent = _local_agent(db)
        self._tabbed(agent, db)
        via_inline = json.loads(
            INLINE_TOOL_EXECUTORS["dashboard"](
                agent,
                {
                    "action": "widget_put",
                    "name": "inline-metric",
                    "template": "metric",
                    "title": "Users",
                    "props": {"label": "Active users", "value": "42"},
                    "tabId": "main",
                },
                _ctx(),
            )
        )
        assert via_inline["success"] is True, via_inline
        widget = via_inline["snapshot"]["widgets"][0]
        assert widget["contentKind"] == "template"
        assert widget["template"] == "metric"
        assert widget["props"]["value"] == "42"

    def test_template_put_denied_for_non_owner_dm(self, home_env, db):
        from gateway.telegram_personal_settings import (
            reset_personal_memory_context,
            set_personal_memory_context,
        )

        (home_env / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
        agent = SimpleNamespace(platform="telegram", _session_db=db, session_id=SESSION)
        token = set_personal_memory_context(home_env, OTHER_ID)
        try:
            result = json.loads(
                dashboard(
                    db=db,
                    agent=agent,
                    current_session_id=SESSION,
                    action="widget_put",
                    name="w",
                    template="metric",
                    props={"label": "Nope", "value": "1"},
                )
            )
            assert result["success"] is False
            assert "owner only" in result["error"]
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# widget_list
# =========================================================================


class TestWidgetList:
    def test_widget_list_by_tab_and_all(self, home_env, db, widget_plugin):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        _board(agent, db, action="tab_create", tabId="side", title="Side")
        _board(
            agent,
            db,
            action="widget_put",
            name="m-one",
            template="metric",
            props={"label": "A", "value": "1"},
            tabId="main",
        )
        _board(
            agent,
            db,
            action="widget_put",
            name="s-one",
            template="status",
            props={"label": "B", "rows": [{"name": "api", "state": "ok"}]},
            tabId="side",
        )
        _board(
            agent,
            db,
            action="widget_put",
            name="s-two",
            pluginKind="test-widget:report",
            tabId="side",
        )
        main = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list", "tabId": "main"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        assert main["success"] is True
        assert [w["name"] for w in main["widgets"]] == ["m-one"]
        side = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list", "tabId": "side"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        assert [w["name"] for w in side["widgets"]] == ["s-one", "s-two"]
        all_widgets = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        assert [w["name"] for w in all_widgets["widgets"]] == [
            "m-one",
            "s-one",
            "s-two",
        ]

    def test_widget_list_unknown_tab_and_empty_board(self, home_env, db):
        agent = _local_agent(db)
        empty = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        assert empty["success"] is True
        assert empty["widgets"] == []
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        ghost = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list", "tabId": "ghost"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        assert ghost["success"] is False

    def test_widget_list_same_via_inline_executor(self, home_env, db):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        _board(
            agent,
            db,
            action="widget_put",
            name="m-one",
            template="metric",
            props={"label": "A", "value": "1"},
            tabId="main",
        )
        via_registry = json.loads(
            registry.dispatch(
                "dashboard",
                {"action": "widget_list", "tabId": "main"},
                db=db,
                agent=agent,
                current_session_id=SESSION,
            )
        )
        via_inline = json.loads(
            INLINE_TOOL_EXECUTORS["dashboard"](
                agent,
                {"action": "widget_list", "tabId": "main"},
                _ctx(),
            )
        )
        assert via_inline["success"] is True
        assert via_inline["widgets"] == via_registry["widgets"]


# =========================================================================
# presentation + focus (no push channel)
# =========================================================================


class TestDashboardPresentation:
    def test_set_default_presentation_persists(self, home_env, db):
        agent = _local_agent(db)
        snap = _board(
            agent, db, action="set_default_presentation", presentation="expanded"
        )
        assert snap["defaultPresentation"] == "expanded"
        assert _board(agent, db, action="read")["defaultPresentation"] == "expanded"
        result = _call(
            agent, db, action="set_default_presentation", presentation="tall"
        )
        assert result["success"] is False

    def test_focus_and_set_presentation_unavailable(self, home_env, db):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        for args in (
            {"action": "focus_tab", "tabId": "main"},
            {"action": "set_presentation", "presentation": "expanded"},
        ):
            result = _call(agent, db, **args)
            assert result["success"] is True
            assert result["status"] == "unavailable"
            assert result["code"] == "UNAVAILABLE"


# =========================================================================
# owner gate
# =========================================================================


class TestDashboardOwnerGate:
    def test_tenant_scope_denied(self, home_env, db):
        scope = TenantScope(home_env, object(), "room-a", home_env / "a", "tg-a")
        token = set_tenant_scope(scope)
        try:
            via_dispatch = _call(_local_agent(db), db, action="read")
            assert "error" in via_dispatch
            assert via_dispatch.get("success") is not True
            direct = json.loads(
                dashboard(db=db, agent=_local_agent(db), current_session_id=SESSION)
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
            result = json.loads(
                dashboard(db=db, agent=agent, current_session_id=SESSION)
            )
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
            result = json.loads(
                dashboard(db=db, agent=agent, current_session_id=SESSION)
            )
            assert result["success"] is True
            assert result["snapshot"]["revision"] == 0
        finally:
            reset_personal_memory_context(token)


# =========================================================================
# registration: toolset, core tools, config, inline executor, adapter
# =========================================================================


class TestDashboardRegistration:
    def test_registered_in_dashboard_toolset_and_core_tools(self):
        assert registry.get_entry("dashboard") is not None
        assert "dashboard" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["dashboard"]["tools"]) == {"dashboard"}
        assert any(key == "dashboard" for key, _label, _desc in CONFIGURABLE_TOOLSETS)

    def test_same_result_via_function_registry_and_inline_executor(self, home_env, db):
        agent = _local_agent(db)
        _board(agent, db, action="tab_create", tabId="main", title="Main")
        direct = json.loads(dashboard(db=db, agent=agent, current_session_id=SESSION))
        via_registry = json.loads(
            registry.get_entry("dashboard").handler(
                {"action": "read"}, db=db, agent=agent, current_session_id=SESSION
            )
        )
        via_inline = json.loads(
            INLINE_TOOL_EXECUTORS["dashboard"](agent, {"action": "read"}, _ctx())
        )
        assert direct["snapshot"] == via_registry["snapshot"] == via_inline["snapshot"]
        assert "dashboard" in INLINE_TOOL_EXECUTORS

    def test_adapter_hides_dashboard_from_non_owner(self, home_env):
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
