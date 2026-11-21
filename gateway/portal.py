#!/usr/bin/env python3
"""F9c portal allocator — loopback-only routes for local dev servers.

Port of the external agent's portal allocation (``src/agents/tools/portal-tool.ts``
over the gateway's ``portal.open`` / ``portal.list`` / ``portal.close``
server methods): the model asks for a route, the gateway allocates a port
and mints a bearer token, and the dev server starts with ``PUBLIC_URL`` +
``PORT``. Hermes has no gateway portal allocator and no public ingress, so
every portal here is loopback-only: ``url`` and ``publicUrl`` are both the
``127.0.0.1`` URL, and the result carries the same loopback warning
the external agent's tool prints.

Routes live in ``state_meta`` key ``gateway.portals`` (see
``gateway/gateway_services.py``) so allocations survive a gateway restart.
The bearer token is readable by the owner — user ruling 2026-11-11: no
write-scope gate on the token. The token is stored alongside the route in
state.db (same trust as the session data there) and is only ever returned
to the owner-only ``portal`` tool.

A portal is an allocation, not a listener: ``open`` verifies the port is
free at allocation time (bind test) but nothing listens on it until the
dev server starts. ``close`` removes the allocation; it does not stop any
server already running on the port.
"""

from __future__ import annotations

import logging
import secrets
import socket
import time
from typing import Any, Optional

from gateway.gateway_services import read_service_json, write_service_json

logger = logging.getLogger("gateway.portal")

_PORTALS_KEY = "gateway.portals"
_LOOPBACK_WARNING = (
    "Loopback-only portal: access is local to the Gateway host, even when "
    "the app runs on a worker. For a remote viewer, configure private "
    "portal ingress or a reachable direct listener; local use is still valid."
)


class PortalError(ValueError):
    """Invalid portal operation (bad port/path, port in use, unknown id)."""


def _load(db) -> list[dict[str, Any]]:
    stored = read_service_json(db, _PORTALS_KEY)
    if isinstance(stored, dict) and isinstance(stored.get("portals"), list):
        return [p for p in stored["portals"] if isinstance(p, dict)]
    return []


def _save(db, portals: list[dict[str, Any]]) -> None:
    write_service_json(db, _PORTALS_KEY, {"portals": portals})


def _port_free(port: int) -> bool:
    """True when nothing listens on 127.0.0.1:port right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _validate_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PortalError("port must be an integer from 1 to 65535")
    return port


def open_portal(
    db,
    port: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    path: str = "/",
) -> dict[str, Any]:
    """Allocate a loopback route for a dev server on *port*."""
    port = _validate_port(port)
    if path is None:
        path = "/"
    if not isinstance(path, str) or not path.startswith("/"):
        raise PortalError("path must start with /")
    portals = _load(db)
    if any(p.get("port") == port for p in portals):
        raise PortalError(f"port {port} already has a portal allocated")
    if not _port_free(port):
        raise PortalError(f"port {port} is already in use")
    token = secrets.token_urlsafe(32)
    portal_id = f"portal_{secrets.token_hex(6)}"
    url = f"http://127.0.0.1:{port}{path}"
    portal = {
        "id": portal_id,
        "port": port,
        "title": title,
        "description": description,
        "path": path,
        "url": url,
        "publicUrl": url,
        "bearerUrl": f"{url}{'&' if '?' in url else '?'}token={token}",
        "bearerToken": token,
        "exposure": {"kind": "loopback-only"},
        "createdAt": int(time.time() * 1000),
    }
    portals.append(portal)
    _save(db, portals)
    return dict(portal)


def list_portals(db) -> dict[str, Any]:
    """All active portals, each with its exposure facts."""
    return {"portals": [dict(p) for p in _load(db)]}


def close_portal(db, portal_id: str) -> dict[str, Any]:
    """Remove the allocation *portal_id*; ``{closed: True}``."""
    if not isinstance(portal_id, str) or not portal_id:
        raise PortalError("id required")
    portals = _load(db)
    remaining = [p for p in portals if p.get("id") != portal_id]
    if len(remaining) == len(portals):
        raise PortalError(f"no portal {portal_id!r}")
    _save(db, remaining)
    return {"closed": True, "id": portal_id}


def loopback_warning() -> str:
    """The loopback-only warning, for tool result text."""
    return _LOOPBACK_WARNING
