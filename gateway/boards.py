#!/usr/bin/env python3
"""F9b boards service — the dashboard's tabs + widgets, per agent session.

Port of the external agent's board store (``src/boards/board-store.ts``) and its
``board.get`` / ``board.update`` / ``board.widget.put`` server methods
(``src/gateway/server-methods/board.ts``), over the shapes in
``packages/gateway-protocol/src/schema/board.ts`` (``BoardSnapshot``,
``BoardOp``, ``BoardWidgetPutParams``). Hermes has no gateway server-methods
layer, so the service is called directly by the ``dashboard`` tool — the
same in-process shape as the F9a presence tool, which reads live sources
instead of RPC.

A board is one JSON document per agent session, stored under
``state_meta`` key ``gateway.boards:<sessionKey>`` (see
``gateway/gateway_services.py``). A fresh session reads as the external agent's empty
board: ``{sessionKey, revision: 0, tabs: [], widgets: []}`` — no default
tab is invented; the model creates tabs explicitly. Every mutation bumps
``revision`` once per call; tab and widget ``position`` values are
contiguous zero-based indexes, re-derived after every mutation.

``widget_put`` creates or updates trusted *plugin* widgets only, exactly as
in the external agent: the ``pluginKind`` must be ``<pluginId>:<name>`` and the plugin
part must name an installed plugin that is not disabled (and, when a
``plugins.enabled`` allow-list is configured, is on it). The check reads
manifests only (``collect_directory_manifests``) — no plugin code runs.
The F10 half of ``widget_put`` places pre-made *template* widgets instead:
``template`` names one of the widget kit's templates (``metric``,
``progress``, ``checklist``, ``flow``, ``chart``, ``status``) and ``props``
carries that template's data, validated by ``validate_template_data`` — the
same spec the DM widget surface renders, stored so the dashboard renders
the same widget from the same data. ``session:report`` /
``session:progress`` / ``session:website`` have no Hermes counterpart yet.

``focus_tab`` / ``set_presentation`` need a live Control UI command
channel: the external agent broadcasts a ``board.command`` event to connected Control
UI clients and reports ``delivered == 0`` as unavailable. Hermes has a
dashboard heartbeat (``gateway/scale_to_zero.dashboard_client_last_seen``)
but no server-side push channel to dashboard clients, so those two actions
always report unavailable — the durable half, ``set_default_presentation``,
is stored on the board and returned by ``read`` as the effective default
(``split`` when unset).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from gateway.gateway_services import read_service_json, write_service_json

logger = logging.getLogger("gateway.boards")

_TAB_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_WIDGET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PLUGIN_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}:[a-z0-9][a-z0-9._-]{0,63}$")
_TITLE_MAX = 80
_PROPS_MAX_BYTES = 8192
_SIZE_MAP = {"sm": (3, 3), "md": (6, 4), "lg": (8, 6), "xl": (12, 8), "full": (12, 8)}
_PRESENTATIONS = ("split", "expanded")


class BoardError(ValueError):
    """Invalid board operation (bad slug, missing tab/widget, bad placement)."""


def _board_key(session_key: str) -> str:
    return f"gateway.boards:{session_key}"


def _empty_board(session_key: str) -> dict[str, Any]:
    return {
        "sessionKey": session_key,
        "revision": 0,
        "tabs": [],
        "widgets": [],
        "defaultPresentation": None,
    }


def _load_board(db, session_key: str) -> dict[str, Any]:
    """Stored board, or the empty board for a session that has none yet."""
    stored = read_service_json(db, _board_key(session_key))
    if not isinstance(stored, dict) or stored.get("sessionKey") != session_key:
        return _empty_board(session_key)
    board = _empty_board(session_key)
    board.update(
        {
            k: stored[k]
            for k in ("revision", "tabs", "widgets", "defaultPresentation")
            if k in stored
        }
    )
    if not isinstance(board["tabs"], list):
        board["tabs"] = []
    if not isinstance(board["widgets"], list):
        board["widgets"] = []
    if not isinstance(board["revision"], int):
        board["revision"] = 0
    return board


def _save_board(db, board: dict[str, Any]) -> None:
    write_service_json(db, _board_key(board["sessionKey"]), board)


def _reindex(items: list[dict[str, Any]]) -> None:
    for index, item in enumerate(items):
        item["position"] = index


def _require_tab_id(tab_id: Any) -> str:
    if not isinstance(tab_id, str) or not _TAB_ID_RE.match(tab_id):
        raise BoardError("tabId must be a lowercase slug up to 40 characters")
    return tab_id


def _require_widget_name(name: Any) -> str:
    if not isinstance(name, str) or not _WIDGET_NAME_RE.match(name):
        raise BoardError("name must be a lowercase slug up to 64 characters")
    return name


def _require_title(title: Any, *, required: bool = False) -> Optional[str]:
    if title is None:
        if required:
            raise BoardError("title required")
        return None
    if not isinstance(title, str) or not 1 <= len(title) <= _TITLE_MAX:
        raise BoardError("title must be 1-80 characters")
    return title


def _find_tab(board: dict[str, Any], tab_id: str) -> dict[str, Any]:
    for tab in board["tabs"]:
        if tab.get("tabId") == tab_id:
            return tab
    raise BoardError(f"no tab {tab_id!r} on this board")


def _find_widget(board: dict[str, Any], name: str) -> dict[str, Any]:
    for widget in board["widgets"]:
        if widget.get("name") == name:
            return widget
    raise BoardError(f"no widget {name!r} on this board")


def _snapshot(board: dict[str, Any]) -> dict[str, Any]:
    """Public snapshot: the external agent ``BoardSnapshot`` shape plus the
    effective default presentation (``split`` when unset)."""
    return {
        "sessionKey": board["sessionKey"],
        "revision": board["revision"],
        "tabs": [
            {"tabId": t["tabId"], "title": t["title"], "position": t["position"]}
            for t in sorted(board["tabs"], key=lambda t: t["position"])
        ],
        "widgets": [
            dict(w) for w in sorted(board["widgets"], key=lambda w: w["position"])
        ],
        "defaultPresentation": board.get("defaultPresentation") or "split",
    }


def get_board(db, session_key: str) -> dict[str, Any]:
    """Snapshot for *session_key* (empty board when the session has none)."""
    if not session_key:
        raise BoardError("agent session required")
    return _snapshot(_load_board(db, session_key))


def set_default_presentation(db, session_key: str, presentation: str) -> dict[str, Any]:
    """Persist the default presentation; applies on subsequent opens."""
    if presentation not in _PRESENTATIONS:
        raise BoardError("presentation must be split or expanded")
    board = _load_board(db, session_key)
    board["defaultPresentation"] = presentation
    board["revision"] += 1
    _save_board(db, board)
    return _snapshot(board)


def _apply_tab_create(board: dict[str, Any], op: dict[str, Any]) -> None:
    tab_id = _require_tab_id(op.get("tabId"))
    title = _require_title(op.get("title"), required=True)
    if any(t.get("tabId") == tab_id for t in board["tabs"]):
        raise BoardError(f"tab {tab_id!r} already exists")
    board["tabs"].append(
        {
            "tabId": tab_id,
            "title": title,
            "position": len(board["tabs"]),
            "chatDock": "right",
        }
    )


def _apply_tab_update(board: dict[str, Any], op: dict[str, Any]) -> None:
    tab_id = _require_tab_id(op.get("tabId"))
    title = _require_title(op.get("title"))
    position = op.get("position")
    if title is None and position is None:
        raise BoardError("tab_update requires title or position")
    if position is not None and (
        not isinstance(position, int) or isinstance(position, bool) or position < 0
    ):
        raise BoardError("position must be a zero-based integer")
    tab = _find_tab(board, tab_id)
    if title is not None:
        tab["title"] = title
    if position is not None:
        ordered = [
            t
            for t in sorted(board["tabs"], key=lambda t: t["position"])
            if t is not tab
        ]
        ordered.insert(min(position, len(ordered)), tab)
        board["tabs"] = ordered
        _reindex(board["tabs"])


def _apply_tab_delete(board: dict[str, Any], op: dict[str, Any]) -> None:
    tab_id = _require_tab_id(op.get("tabId"))
    _find_tab(board, tab_id)
    board["tabs"] = [t for t in board["tabs"] if t.get("tabId") != tab_id]
    _reindex(board["tabs"])
    board["widgets"] = [w for w in board["widgets"] if w.get("tabId") != tab_id]


def _apply_tabs_reorder(board: dict[str, Any], op: dict[str, Any]) -> None:
    tab_ids = op.get("tabIds")
    if (
        not isinstance(tab_ids, list)
        or not tab_ids
        or any(not isinstance(t, str) or not _TAB_ID_RE.match(t) for t in tab_ids)
    ):
        raise BoardError("tabIds must be a non-empty list of tab slugs")
    current = {t.get("tabId") for t in board["tabs"]}
    if set(tab_ids) != current or len(tab_ids) != len(current):
        raise BoardError("tabs_reorder needs the complete tab order (every tab, once)")
    by_id = {t["tabId"]: t for t in board["tabs"]}
    board["tabs"] = [by_id[t] for t in tab_ids]
    _reindex(board["tabs"])


def _tab_widgets(board: dict[str, Any], tab_id: str) -> list[dict[str, Any]]:
    return sorted(
        (w for w in board["widgets"] if w.get("tabId") == tab_id),
        key=lambda w: w["position"],
    )


def _apply_widget_move(board: dict[str, Any], op: dict[str, Any]) -> None:
    name = _require_widget_name(op.get("name"))
    target_tab = op.get("tabId")
    position = op.get("position")
    after = op.get("after")
    if position is not None and after is not None:
        raise BoardError("widget_move accepts either position or after, not both")
    if position is not None and (
        not isinstance(position, int) or isinstance(position, bool) or position < 0
    ):
        raise BoardError("position must be a zero-based integer")
    if after is not None:
        _require_widget_name(after)
    widget = _find_widget(board, name)
    if target_tab is not None:
        _require_tab_id(target_tab)
        _find_tab(board, target_tab)
        widget["tabId"] = target_tab
    siblings = [w for w in _tab_widgets(board, widget["tabId"]) if w is not widget]
    if after is not None:
        anchor = next((w for w in siblings if w.get("name") == after), None)
        if anchor is None:
            raise BoardError(f"no widget {after!r} on tab {widget['tabId']!r}")
        siblings.insert(siblings.index(anchor) + 1, widget)
    else:
        siblings.insert(
            min(position if position is not None else len(siblings), len(siblings)),
            widget,
        )
    for index, item in enumerate(siblings):
        item["position"] = index


def _apply_widget_resize(board: dict[str, Any], op: dict[str, Any]) -> None:
    name = _require_widget_name(op.get("name"))
    size_w, size_h = op.get("sizeW"), op.get("sizeH")
    for key, value, maximum in (("sizeW", size_w, 12), ("sizeH", size_h, 20)):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= maximum
        ):
            raise BoardError(f"{key} must be an integer from 1 to {maximum}")
    widget = _find_widget(board, name)
    widget["sizeW"], widget["sizeH"] = size_w, size_h


def _apply_widget_remove(board: dict[str, Any], op: dict[str, Any]) -> None:
    name = _require_widget_name(op.get("name"))
    _find_widget(board, name)
    board["widgets"] = [w for w in board["widgets"] if w.get("name") != name]
    for tab in board["tabs"]:
        for index, item in enumerate(_tab_widgets(board, tab["tabId"])):
            item["position"] = index


_OP_HANDLERS = {
    "tab_create": _apply_tab_create,
    "tab_update": _apply_tab_update,
    "tab_delete": _apply_tab_delete,
    "tabs_reorder": _apply_tabs_reorder,
    "widget_move": _apply_widget_move,
    "widget_resize": _apply_widget_resize,
    "widget_remove": _apply_widget_remove,
}


def apply_ops(db, session_key: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply BoardOps atomically (one revision bump); any invalid op aborts
    the whole call with no write."""
    if not session_key:
        raise BoardError("agent session required")
    if not isinstance(ops, list) or not ops:
        raise BoardError("ops must be a non-empty list")
    board = _load_board(db, session_key)
    working = json.loads(json.dumps(board))  # apply on a copy; persist only on success
    for op in ops:
        if not isinstance(op, dict):
            raise BoardError("each op must be an object")
        handler = _OP_HANDLERS.get(str(op.get("kind") or ""))
        if handler is None:
            raise BoardError(f"unknown board op: {op.get('kind')!r}")
        handler(working, op)
    working["revision"] = board["revision"] + 1
    _save_board(db, working)
    return _snapshot(working)


def _installed_plugin_ids() -> Optional[set[str]]:
    """Registry ids + bare names of installed plugins (manifests only — no
    plugin code runs), for the widget_put trust check. None when the
    inventory cannot be read (fail-closed: widget_put refuses)."""
    try:
        from hermes_cli.plugins_discovery import collect_directory_manifests
        from hermes_cli.plugins_manifest import manifest_key

        manifests = collect_directory_manifests()
        return {manifest_key(m) for m in manifests} | {m.name for m in manifests}
    except Exception:
        logger.debug("boards: plugin inventory unreadable", exc_info=True)
        return None


def _check_plugin_kind(plugin_kind: Any) -> str:
    if not isinstance(plugin_kind, str) or not _PLUGIN_KIND_RE.match(plugin_kind):
        raise BoardError("pluginKind must use the <pluginId>:<name> format")
    plugin_id = plugin_kind.split(":", 1)[0]
    installed = _installed_plugin_ids()
    if installed is None:
        raise BoardError("plugin inventory unavailable; cannot verify widget plugin")
    if plugin_id not in installed:
        raise BoardError(f"no installed plugin {plugin_id!r} supplies widgets")
    try:
        from hermes_cli.plugins_discovery import (
            _get_disabled_plugins,
            _get_enabled_plugins,
        )

        if plugin_id in _get_disabled_plugins():
            raise BoardError(f"plugin {plugin_id!r} is disabled")
        enabled = _get_enabled_plugins()
        if enabled is not None and plugin_id not in enabled:
            raise BoardError(f"plugin {plugin_id!r} is not enabled")
    except BoardError:
        raise
    except Exception:
        logger.debug("boards: plugin enablement unreadable", exc_info=True)
    return plugin_kind


def _check_template(template: Any, data: Any) -> tuple[str, dict[str, Any]]:
    """Validate a pre-made widget template + data; returns (name, cleaned data).

    The widget kit is the single authority on template shapes (the same
    validator the DM widget surface uses); the import is function-level so
    boards stay usable where the Telegram platform is not loaded — a failed
    import refuses the template put instead of breaking the service.
    """
    try:
        from plugins.platforms.telegram.widget_kit import validate_template_data
    except Exception as exc:
        logger.debug("boards: template widgets unavailable", exc_info=True)
        raise BoardError(f"template widgets unavailable: {exc}")
    try:
        cleaned = validate_template_data(template, data)
    except ValueError as exc:
        raise BoardError(str(exc))
    if len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) > _PROPS_MAX_BYTES:
        raise BoardError("template data exceeds the 8KB limit")
    return str(template).strip().lower(), cleaned


def _check_props(props: Any) -> None:
    if props is not None:
        if not isinstance(props, dict):
            raise BoardError("props must be an object")
        if (
            len(json.dumps(props, ensure_ascii=False).encode("utf-8"))
            > _PROPS_MAX_BYTES
        ):
            raise BoardError("props exceed the 8KB limit")


def _place_widget(
    working: dict[str, Any],
    *,
    name: str,
    title: Optional[str],
    tab_id: Optional[str],
    size: Optional[str],
    after: Optional[str],
    kind_fields: dict[str, Any],
) -> dict[str, Any]:
    """Create or update the widget named *name* on its tab, in place.

    ``kind_fields`` carries the kind-specific stored fields (``contentKind``,
    ``contentOwner``, plus ``pluginKind`` or ``template`` + ``props``). A
    re-put replaces the kind wholesale: the other kind's key is dropped, so
    a plugin widget re-put as a template (or the reverse) leaves no stale
    kind behind. Update moves the widget to the end of its tab unless
    ``after`` pins it after a sibling; create appends (or inserts after
    ``after``). ``props`` is cleared on update when absent, kept off a
    fresh widget when absent — the same as ``widget_put`` always did.
    """
    if tab_id is not None:
        _require_tab_id(tab_id)
    if size is not None and size not in _SIZE_MAP:
        raise BoardError("size must be sm, md, lg, xl or full")
    if after is not None:
        _require_widget_name(after)
    if tab_id is not None:
        _find_tab(working, tab_id)
    elif working["tabs"]:
        tab_id = sorted(working["tabs"], key=lambda t: t["position"])[0]["tabId"]
    else:
        raise BoardError("widget_put needs a tabId: this board has no tabs yet")
    if after is not None:
        anchor = next(
            (
                w
                for w in working["widgets"]
                if w.get("name") == after and w.get("tabId") == tab_id
            ),
            None,
        )
        if anchor is None:
            raise BoardError(f"no widget {after!r} on tab {tab_id!r}")
    size_w, size_h = _SIZE_MAP.get(size or "md", _SIZE_MAP["md"])
    existing = next((w for w in working["widgets"] if w.get("name") == name), None)
    if existing is not None:
        existing["tabId"] = tab_id
        existing.pop("pluginKind", None)
        existing.pop("template", None)
        existing.update(kind_fields)
        existing["sizeW"], existing["sizeH"] = size_w, size_h
        if title is not None:
            existing["title"] = title
        existing["revision"] = int(existing.get("revision") or 1) + 1
        siblings = [w for w in _tab_widgets(working, tab_id) if w is not existing]
        if after is not None:
            anchor = next(w for w in siblings if w.get("name") == after)
            siblings.insert(siblings.index(anchor) + 1, existing)
        else:
            siblings.append(existing)
        for index, item in enumerate(siblings):
            item["position"] = index
        return existing
    siblings = _tab_widgets(working, tab_id)
    widget: dict[str, Any] = {
        "name": name,
        "tabId": tab_id,
        "sizeW": size_w,
        "sizeH": size_h,
        "position": len(siblings),
        "grantState": "none",
        "revision": 1,
    }
    widget.update(kind_fields)
    if widget.get("props") is None:
        widget.pop("props", None)
    if title is not None:
        widget["title"] = title
    working["widgets"].append(widget)
    if after is not None:
        anchor = next(w for w in siblings if w.get("name") == after)
        ordered = [w for w in _tab_widgets(working, tab_id) if w is not widget]
        ordered.insert(ordered.index(anchor) + 1, widget)
        for index, item in enumerate(ordered):
            item["position"] = index
    return widget


def put_widget(
    db,
    session_key: str,
    *,
    name: str,
    title: Optional[str] = None,
    plugin_kind: str,
    props: Optional[dict] = None,
    tab_id: Optional[str] = None,
    size: Optional[str] = None,
    after: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update a trusted plugin widget (``widget_put``)."""
    if not session_key:
        raise BoardError("agent session required")
    name = _require_widget_name(name)
    title = _require_title(title)
    plugin_kind = _check_plugin_kind(plugin_kind)
    if tab_id is not None:
        _require_tab_id(tab_id)
    if size is not None and size not in _SIZE_MAP:
        raise BoardError("size must be sm, md, lg, xl or full")
    if after is not None:
        _require_widget_name(after)
    _check_props(props)
    board = _load_board(db, session_key)
    working = json.loads(json.dumps(board))
    _place_widget(
        working,
        name=name,
        title=title,
        tab_id=tab_id,
        size=size,
        after=after,
        kind_fields={
            "contentKind": "plugin",
            "contentOwner": "plugin",
            "pluginKind": plugin_kind,
            "props": props,
        },
    )
    working["revision"] = board["revision"] + 1
    _save_board(db, working)
    return _snapshot(working)


def put_template_widget(
    db,
    session_key: str,
    *,
    name: str,
    title: Optional[str] = None,
    template: str,
    data: Optional[dict] = None,
    tab_id: Optional[str] = None,
    size: Optional[str] = None,
    after: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update a pre-made template widget (``widget_put`` + ``template``).

    The stored widget is the render spec the dashboard reads: ``template``
    names the kit template, ``props`` holds its validated data, and
    ``build_widget_html(template, title, props)`` reproduces the widget the
    DM surface sends for the same spec.
    """
    if not session_key:
        raise BoardError("agent session required")
    name = _require_widget_name(name)
    title = _require_title(title)
    template, cleaned = _check_template(template, data)
    if tab_id is not None:
        _require_tab_id(tab_id)
    if size is not None and size not in _SIZE_MAP:
        raise BoardError("size must be sm, md, lg, xl or full")
    if after is not None:
        _require_widget_name(after)
    board = _load_board(db, session_key)
    working = json.loads(json.dumps(board))
    _place_widget(
        working,
        name=name,
        title=title,
        tab_id=tab_id,
        size=size,
        after=after,
        kind_fields={
            "contentKind": "template",
            "contentOwner": "agent",
            "template": template,
            "props": cleaned,
        },
    )
    working["revision"] = board["revision"] + 1
    _save_board(db, working)
    return _snapshot(working)


def list_widgets(
    db, session_key: str, tab_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """Widgets on one tab (``tab_id`` given), or every widget in tab order."""
    if not session_key:
        raise BoardError("agent session required")
    board = _load_board(db, session_key)
    if tab_id is not None:
        _require_tab_id(tab_id)
        _find_tab(board, tab_id)
        return [dict(w) for w in _tab_widgets(board, tab_id)]
    ordered_tabs = sorted(board["tabs"], key=lambda t: t["position"])
    tab_ids = {t["tabId"] for t in ordered_tabs}
    widgets = [
        dict(w) for tab in ordered_tabs for w in _tab_widgets(board, tab["tabId"])
    ]
    widgets.extend(
        dict(w)
        for w in sorted(board["widgets"], key=lambda w: w["position"])
        if w.get("tabId") not in tab_ids
    )
    return widgets
