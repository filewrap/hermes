#!/usr/bin/env python3
"""F9b ``dashboard`` tool — read and arrange the session dashboard (boards).

Port of the external agent's ``src/agents/tools/dashboard-tool.ts`` (BoardCommand /
BoardOp, tabs + widgets) over the Hermes boards service (``gateway/boards.py``),
which stores one board per agent session in ``state_meta``. Actions:
``read`` (snapshot), ``tab_create`` / ``tab_update`` / ``tab_delete`` /
``tabs_reorder``, ``widget_put`` / ``widget_move`` / ``widget_resize`` /
``widget_remove``, ``focus_tab``, ``set_presentation``,
``set_default_presentation``.

``widget_put`` creates or updates trusted plugin widgets only
(``<pluginId>:<name>``, plugin installed and not disabled) — or, with
``template``, places a pre-made template widget (``metric``, ``progress``,
``checklist``, ``flow``, ``chart``, ``status``) whose ``props`` carry that
template's data, validated by the widget kit. ``widget_list`` lists the
widgets on one tab (or every widget, in tab order). ``focus_tab`` and
``set_presentation`` need a live dashboard command channel — Hermes has a
dashboard heartbeat but no server-side push channel to dashboard clients,
so they report unavailable; ``set_default_presentation`` is the durable
half and is stored on the board (``read`` returns the effective default,
``split`` when unset).

Owner only (user ruling 2026-11-11): the handler re-checks the owner, the
Telegram adapter hides the tool from non-owner senders, and tenant
sessions never see it (not in ``agent/tenant_policy.SUPPORTED``). No
``check_fn``: like ``presence`` the tool stays visible when the store is
missing and reports per-call availability — a ``check_fn`` is TTL-cached
per home and cannot answer a per-requester question.
"""

import json
import logging
from typing import Any, Optional

from gateway.boards import (
    BoardError,
    apply_ops,
    get_board,
    list_widgets,
    put_template_widget,
    put_widget,
    set_default_presentation,
)
from gateway.gateway_services import (
    release_service_db,
    resolve_service_db,
)
from tools.gateway_owner import owner_only_error, requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger("tools.dashboard")

_DASHBOARD_ACTIONS = (
    "read",
    "tab_create",
    "tab_update",
    "tab_delete",
    "tabs_reorder",
    "widget_put",
    "widget_move",
    "widget_resize",
    "widget_remove",
    "widget_list",
    "focus_tab",
    "set_presentation",
    "set_default_presentation",
)


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    return (
        f"Dashboard revision {snapshot['revision']}: "
        f"{len(snapshot['tabs'])} tabs, {len(snapshot['widgets'])} widgets"
    )


def _op_for_action(action: str, args: dict[str, Any]) -> dict[str, Any]:
    """BoardOp for a tab/widget mutation action (mirrors the external agent's opForAction)."""

    def _name() -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise BoardError("name required")
        return name

    def _tab_id() -> str:
        tab_id = args.get("tabId")
        if not isinstance(tab_id, str) or not tab_id:
            raise BoardError("tabId required")
        return tab_id

    if action == "tab_create":
        return {"kind": "tab_create", "tabId": _tab_id(), "title": args.get("title")}
    if action == "tab_update":
        op: dict[str, Any] = {"kind": "tab_update", "tabId": _tab_id()}
        if args.get("title") is not None:
            op["title"] = args.get("title")
        if args.get("position") is not None:
            op["position"] = args.get("position")
        if "title" not in op and "position" not in op:
            raise BoardError("tab_update requires title or position")
        return op
    if action == "tab_delete":
        return {"kind": "tab_delete", "tabId": _tab_id()}
    if action == "tabs_reorder":
        return {"kind": "tabs_reorder", "tabIds": args.get("tabIds")}
    if action == "widget_move":
        op = {"kind": "widget_move", "name": _name()}
        if args.get("tabId") is not None:
            op["tabId"] = args.get("tabId")
        if args.get("position") is not None:
            op["position"] = args.get("position")
        if args.get("after") is not None:
            op["after"] = args.get("after")
        return op
    if action == "widget_resize":
        for key in ("sizeW", "sizeH"):
            if args.get(key) is None:
                raise BoardError(f"{key} required")
        return {
            "kind": "widget_resize",
            "name": _name(),
            "sizeW": args.get("sizeW"),
            "sizeH": args.get("sizeH"),
        }
    if action == "widget_remove":
        return {"kind": "widget_remove", "name": _name()}
    raise BoardError(f"unknown dashboard action: {action}")


_DASHBOARD_SCHEMA = {
    "name": "dashboard",
    "description": (
        "Read and arrange this session dashboard. widget_put places a trusted plugin widget "
        "or a pre-made template widget on a tab. "
        "Actions: read snapshot; tab_create/tab_update/tab_delete/tabs_reorder; "
        "widget_put/widget_move/widget_resize/widget_remove/widget_list; focus_tab opens the dashboard side panel; "
        "set_presentation shows the dashboard alongside chat (split) or across the task area (expanded). "
        "focus_tab and set_presentation require a connected dashboard client and do not save a default. "
        "set_default_presentation saves split or expanded for subsequent opens without requiring a connected client; "
        "read returns the effective defaultPresentation (split when unset). "
        "Widgets use stable names. widget_put creates or updates a trusted plugin widget "
        "(pluginKind <pluginId>:<name> for an installed, enabled plugin) or, with template, "
        "a pre-made template widget (metric, progress, checklist, flow, chart, status) whose "
        "props carry that template's data; template and pluginKind are never given together. "
        "widget_list lists the widgets on one tab (tabId given) or every widget in tab order. "
        "Sizes: sm=3x3, md=6x4, lg=8x6, xl=12x8, full=12x8 single-widget emphasis. Owner only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_DASHBOARD_ACTIONS),
                "description": "Dashboard action; widget_put places a plugin widget or a pre-made template widget; widget_list lists widgets on a tab",
            },
            "tabId": {
                "type": "string",
                "description": "Stable tab slug (lowercase, up to 40 characters)",
            },
            "title": {"type": "string", "description": "Tab title (1-80 characters)"},
            "presentation": {
                "type": "string",
                "enum": ["split", "expanded"],
                "description": "Dashboard panel presentation",
            },
            "position": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based position",
            },
            "tabIds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Complete tab order",
            },
            "name": {"type": "string", "description": "Stable widget name"},
            "after": {
                "type": "string",
                "description": "Place after stable widget name",
            },
            "sizeW": {"type": "integer", "minimum": 1, "maximum": 12},
            "sizeH": {"type": "integer", "minimum": 1, "maximum": 20},
            "size": {"type": "string", "enum": ["sm", "md", "lg", "xl", "full"]},
            "pluginKind": {
                "type": "string",
                "description": "Registered widget kind (<pluginId>:<name>) for an installed plugin",
            },
            "template": {
                # Hardcoded on purpose: importing widget_kit here would pull
                # the Telegram adapter in at tool-discovery time. The service
                # (gateway.boards._check_template) enforces TEMPLATES.
                "type": "string",
                "enum": ["metric", "progress", "checklist", "flow", "chart", "status"],
                "description": "Pre-made widget template for widget_put (props carry that template's data)",
            },
            "props": {
                "type": "object",
                "description": "Widget JSON props (maximum 8KB encoded); for a template widget, the template's data",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _dashboard_read(db, session_key: str, params: dict) -> dict[str, Any]:
    return get_board(db, session_key)


def _dashboard_widget_list(db, session_key: str, params: dict) -> str:
    widgets = list_widgets(db, session_key, tab_id=params["tabId"])
    scope = f" on tab {params['tabId']}" if params["tabId"] else ""
    return _ok(
        text=f"{len(widgets)} widget(s){scope}",
        widgets=widgets,
    )


def _dashboard_set_default_presentation(
    db, session_key: str, params: dict
) -> dict[str, Any] | str:
    if params["presentation"] not in ("split", "expanded"):
        return tool_error("presentation must be split or expanded", success=False)
    return set_default_presentation(db, session_key, params["presentation"])


def _dashboard_widget_put(db, session_key: str, params: dict) -> dict[str, Any] | str:
    name = params["name"]
    template = params["template"]
    plugin_kind = params["pluginKind"]
    if name is None:
        return tool_error("name required for widget_put", success=False)
    if template is not None and plugin_kind is not None:
        return tool_error(
            "widget_put takes template or pluginKind, not both",
            success=False,
        )
    if template is not None:
        return put_template_widget(
            db,
            session_key,
            name=name,
            title=params["title"],
            template=template,
            data=params["props"],
            tab_id=params["tabId"],
            size=params["size"],
            after=params["after"],
        )
    if plugin_kind is not None:
        return put_widget(
            db,
            session_key,
            name=name,
            title=params["title"],
            plugin_kind=plugin_kind,
            props=params["props"],
            tab_id=params["tabId"],
            size=params["size"],
            after=params["after"],
        )
    return tool_error("widget_put needs template or pluginKind", success=False)


def _dashboard_apply_ops(
    db, session_key: str, action: str, params: dict
) -> dict[str, Any]:
    return apply_ops(
        db,
        session_key,
        [
            _op_for_action(
                action,
                {
                    "tabId": params["tabId"],
                    "title": params["title"],
                    "position": params["position"],
                    "tabIds": params["tabIds"],
                    "name": params["name"],
                    "after": params["after"],
                    "sizeW": params["sizeW"],
                    "sizeH": params["sizeH"],
                },
            )
        ],
    )


_DASHBOARD_ACTION_HANDLERS = {
    "read": _dashboard_read,
    "widget_list": _dashboard_widget_list,
    "set_default_presentation": _dashboard_set_default_presentation,
    "widget_put": _dashboard_widget_put,
}


def _dashboard_dispatch(
    action: str, db, session_key: str, params: dict
) -> dict[str, Any] | str:
    """Snapshot (or finished response) for one dashboard action.

    Tab/widget mutations not in the table go through ``apply_ops``.
    """
    handler = _DASHBOARD_ACTION_HANDLERS.get(action)
    if handler is not None:
        return handler(db, session_key, params)
    return _dashboard_apply_ops(db, session_key, action, params)


def dashboard(
    action: str = "read",
    tabId: Optional[str] = None,
    title: Optional[str] = None,
    presentation: Optional[str] = None,
    position: Optional[int] = None,
    tabIds: Optional[list] = None,
    name: Optional[str] = None,
    after: Optional[str] = None,
    sizeW: Optional[int] = None,
    sizeH: Optional[int] = None,
    size: Optional[str] = None,
    pluginKind: Optional[str] = None,
    template: Optional[str] = None,
    props: Optional[dict] = None,
    db=None,
    agent=None,
    session_key: Optional[str] = None,
    current_session_id: Optional[str] = None,
) -> str:
    """Read and arrange the session dashboard. Owner only."""
    if not requester_is_owner(agent):
        return owner_only_error("dashboard")
    action = str(action or "read").strip().lower()
    if action not in _DASHBOARD_ACTIONS:
        return tool_error(f"unknown dashboard action: {action}", success=False)
    session_key = session_key or current_session_id or ""
    if not session_key and action not in ("focus_tab", "set_presentation"):
        return tool_error(
            "dashboard needs the agent session (no session id)", success=False
        )
    if action in ("focus_tab", "set_presentation"):
        # No server-side push channel to dashboard clients exists in Hermes
        # (heartbeat only); the durable half is set_default_presentation.
        return _ok(
            status="unavailable",
            code="UNAVAILABLE",
            message="Dashboard unavailable. No dashboard client is connected to receive the command.",
            text="Dashboard unavailable. No dashboard client is connected to receive the command.",
        )
    write = action not in ("read", "widget_list")
    owned_db = None
    try:
        db, owned, error = resolve_service_db(db, write=write)
        if owned:
            owned_db = db
        if error is not None:
            return tool_error(f"dashboard: {error}", success=False)
        result = _dashboard_dispatch(
            action,
            db,
            session_key,
            {
                "tabId": tabId,
                "title": title,
                "presentation": presentation,
                "position": position,
                "tabIds": tabIds,
                "name": name,
                "after": after,
                "sizeW": sizeW,
                "sizeH": sizeH,
                "size": size,
                "pluginKind": pluginKind,
                "template": template,
                "props": props,
            },
        )
        if isinstance(result, str):
            return result
        snapshot = result
    except BoardError as exc:
        return tool_error(f"dashboard: {exc}", success=False)
    except Exception as exc:  # health: allow BLE001 -- tool boundary: service failures surface as tool errors, never a crash
        logger.error("dashboard failed", exc_info=True)
        return tool_error(f"dashboard: {exc}", success=False)
    finally:
        if owned_db is not None:
            release_service_db(True, owned_db)
    return _ok(text=_snapshot_text(snapshot), snapshot=snapshot)


registry.register(
    name="dashboard",
    toolset="dashboard",
    schema=_DASHBOARD_SCHEMA,
    handler=lambda args, **kw: dashboard(
        action=args.get("action", "read"),
        tabId=args.get("tabId"),
        title=args.get("title"),
        presentation=args.get("presentation"),
        position=args.get("position"),
        tabIds=args.get("tabIds"),
        name=args.get("name"),
        after=args.get("after"),
        sizeW=args.get("sizeW"),
        sizeH=args.get("sizeH"),
        size=args.get("size"),
        pluginKind=args.get("pluginKind"),
        template=args.get("template"),
        props=args.get("props"),
        db=kw.get("db"),
        agent=kw.get("agent"),
        session_key=kw.get("session_key"),
        current_session_id=kw.get("current_session_id"),
    ),
    emoji="📊",
)
