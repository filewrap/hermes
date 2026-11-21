#!/usr/bin/env python3
"""F9c ``portal`` tool — open/list/close loopback portals for dev servers.

Port of the external agent's ``src/agents/tools/portal-tool.ts`` over the Hermes
portal allocator (``gateway/portal.py``). ``open`` allocates a port and
mints a bearer token; ``list`` shows active routes with their exposure
facts; ``close`` removes an allocation. Every portal is loopback-only
(Hermes has no public ingress); the result text says so, as the external agent's
does.

Owner only (user ruling 2026-11-11), including the bearer token: the
token is readable by the owner with no write-scope gate. The handler
re-checks the owner, the Telegram adapter hides the tool from non-owner
senders, and tenant sessions never see it (not in
``agent/tenant_policy.SUPPORTED``). No ``check_fn``: like ``presence``
the tool stays visible when the store is missing and reports per-call
availability.
"""

import json
import logging
from typing import Any, Optional

from gateway.gateway_services import release_service_db, resolve_service_db
from gateway.portal import (
    PortalError,
    close_portal,
    list_portals,
    loopback_warning,
    open_portal,
)
from tools.gateway_owner import owner_only_error, requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger("tools.portal")

_PORTAL_ACTIONS = ("open", "list", "close")


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _format_open(portal: dict[str, Any]) -> str:
    return (
        f"{loopback_warning()} Portal route allocated at {portal['url']}. "
        f"Pass PUBLIC_URL={portal['publicUrl']} and PORT={portal['port']} when "
        "starting the dev server. Browser access and application rendering "
        "remain unverified from the requesting viewer; allocation is not "
        "delivery proof. Use the returned URLs unchanged."
    )


def _format_list(result: dict[str, Any]) -> str:
    portals = result["portals"]
    warning = f"{loopback_warning()} " if portals else ""
    return (
        f"{warning}{len(portals)} active portal{'s' if len(portals) != 1 else ''}. "
        "Inspect each portal's exposure facts; browser access and application "
        "rendering remain unverified from the requesting viewer."
    )


_PORTAL_SCHEMA = {
    "name": "portal",
    "description": (
        "Allocate loopback portal routes for local dev servers (open/list/close). "
        "open mints a bearer token and returns the route URL; start the dev server "
        "with PUBLIC_URL and PORT from the result. Every portal is loopback-only: "
        "access is local to the Gateway host. Owner only, including the bearer token."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_PORTAL_ACTIONS),
                "description": "Portal action: open, list, or close",
            },
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "description": "open: local port to expose (required)",
            },
            "title": {
                "type": "string",
                "description": "open: short title for the route",
            },
            "description": {
                "type": "string",
                "description": "open: longer description of the route",
            },
            "path": {
                "type": "string",
                "description": "open: URL path, must start with / (default /)",
            },
            "id": {
                "type": "string",
                "description": "close: portal id from open or list",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def portal(
    action: str = "list",
    port: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    path: Optional[str] = None,
    portal_id: Optional[str] = None,
    db=None,
    agent=None,
) -> str:
    """Open, list, or close a loopback portal. Owner only."""
    if not requester_is_owner(agent):
        return owner_only_error("portal")
    action = str(action or "list").strip().lower()
    if action not in _PORTAL_ACTIONS:
        return tool_error(f"unknown portal action: {action}", success=False)
    if action == "open" and port is None:
        return tool_error("portal open needs a port", success=False)
    if action == "close" and not portal_id:
        return tool_error("portal close needs an id", success=False)
    write = action != "list"
    owned_db = None
    try:
        db, owned, error = resolve_service_db(db, write=write)
        if owned:
            owned_db = db
        if error is not None:
            return tool_error(f"portal: {error}", success=False)
        if action == "open":
            result = open_portal(
                db, port, title=title, description=description, path=path or "/"
            )
            return _ok(text=_format_open(result), portal=result)
        if action == "list":
            result = list_portals(db)
            return _ok(text=_format_list(result), **result)
        result = close_portal(db, portal_id)
        return _ok(text=f"Portal {portal_id}: closed.", **result)
    except PortalError as exc:
        return tool_error(f"portal: {exc}", success=False)
    except Exception as exc:  # health: allow BLE001 -- tool boundary: service failures surface as tool errors, never a crash
        logger.error("portal failed", exc_info=True)
        return tool_error(f"portal: {exc}", success=False)
    finally:
        if owned_db is not None:
            release_service_db(True, owned_db)


registry.register(
    name="portal",
    toolset="portal",
    schema=_PORTAL_SCHEMA,
    handler=lambda args, **kw: portal(
        action=args.get("action", "list"),
        port=args.get("port"),
        title=args.get("title"),
        description=args.get("description"),
        path=args.get("path"),
        portal_id=args.get("id"),
        db=kw.get("db"),
        agent=kw.get("agent"),
    ),
    emoji="🔌",
)
