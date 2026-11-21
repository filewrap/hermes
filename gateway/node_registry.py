"""F9e node registry — paired-device registry for the gateway service layer.

Port of the external agent's ``node-registry.ts`` (in-memory ``nodesById`` /
``nodesByConn`` maps; pairing in ``node-registry-pairing.ts``) onto Hermes'
``state_meta`` store (``gateway/gateway_services.py``): every mutation writes
through to the home's ``state.db`` so paired nodes survive a gateway restart,
and each call loads the maps fresh from the store — no cross-call cache, so a
registry never holds a DB handle the caller already released (one process
serves many profiles; a module-global registry would also leak nodes across
them).

A node is a device that answers HTTP invokes at its own endpoint:

- Pairing is owner-registered: Hermes has no node-initiated gateway
  connection for a node to arrive on, so the owner opens a pairing request
  (the node operator presents its endpoint + bearer token out of band) and
  approves or rejects it. Approving mints the ``pairingIdentity`` + generation
  (cf. the external agent's ``node-registry-pairing.ts``) and records the node.
- Invoke is a plain HTTP POST ``{endpoint}/invoke`` with
  ``{id, command, params, idempotencyKey}`` and a Bearer token — the minimal
  node protocol, defined here because no Hermes node agent exists yet. A node
  API that refuses a command is the node's own limit and surfaces as the
  node's error. There is deliberately NO blocked-command list (user ruling
  2026-11-11: the external agent's ``BLOCKED_INVOKE_COMMANDS`` is dropped).
- ``connected`` is computed live from ``lastSeenAt`` (within 300s), a liveness
  heuristic, not a socket: a node is seen when it is approved and whenever an
  invoke succeeds. Nothing here polls.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from gateway.gateway_services import (
    read_service_json,
    release_service_db,
    resolve_service_db,
    write_service_json,
)

logger = logging.getLogger("gateway.node_registry")

_NODES_KEY = "gateway.nodes"
_CONNECTED_WINDOW_S = 300
_INVOKE_TIMEOUT_S = 30


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


class NodeRegistryError(Exception):
    """A registry operation the caller reports as a tool error."""


class NodeRegistry:
    """Per-home paired-node registry: in-memory maps + state_meta write-through."""

    def __init__(self, db) -> None:
        self._db = db
        self.nodes_by_id: dict[str, dict[str, Any]] = {}
        self.nodes_by_conn: dict[str, str] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def home_key(self) -> str:
        """DB path the registry is bound to (observation scoping, multi-profile)."""
        try:
            return str(getattr(self._db, "db_path", "") or "")
        except Exception:  # health: allow BLE001 -- best-effort home key; any failure reads as no home key
            return ""

    def _load(self) -> None:
        stored = read_service_json(self._db, _NODES_KEY) or {}
        self.nodes_by_id = dict(stored.get("nodes") or {})
        self.pending = dict(stored.get("pending") or {})
        self.nodes_by_conn = {
            str(node.get("pairingIdentity")): node_id
            for node_id, node in self.nodes_by_id.items()
            if node.get("pairingIdentity")
        }

    def _save(self) -> None:
        write_service_json(
            self._db,
            _NODES_KEY,
            {"nodes": self.nodes_by_id, "pending": self.pending},
        )

    # -- reads ------------------------------------------------------------

    def list_nodes(self) -> list[dict[str, Any]]:
        """Node records with live ``connected`` derived from ``lastSeenAt``."""
        now = _now_ms()
        out = []
        for node in self.nodes_by_id.values():
            record = dict(node)
            record.pop("token", None)
            record["connected"] = bool(
                record.get("lastSeenAt")
                and now - int(record["lastSeenAt"]) <= _CONNECTED_WINDOW_S * 1000
            )
            out.append(record)
        return sorted(out, key=lambda n: str(n.get("name") or n.get("nodeId")))

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        node = self.nodes_by_id.get(node_id)
        if node is None:
            return None
        record = dict(node)
        # The bearer token never leaves the registry: describe/status callers
        # (and the model) get everything except the credential.
        record.pop("token", None)
        record["connected"] = bool(
            record.get("lastSeenAt")
            and _now_ms() - int(record["lastSeenAt"]) <= _CONNECTED_WINDOW_S * 1000
        )
        return record

    def resolve_node_id(self, query: str) -> Optional[str]:
        """Node id, or the unique node whose name matches; None when ambiguous/absent."""
        query = (query or "").strip()
        if not query:
            return None
        if query in self.nodes_by_id:
            return query
        matches = [
            node_id
            for node_id, node in self.nodes_by_id.items()
            if str(node.get("name") or "") == query
        ]
        return matches[0] if len(matches) == 1 else None

    def list_pending(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(entry) for entry in self.pending.values()),
            key=lambda e: int(e.get("requestedAt") or 0),
        )

    # -- pairing ----------------------------------------------------------

    def request_pairing(
        self,
        *,
        name: str,
        platform: str,
        endpoint: str,
        token: str,
        caps: Optional[list] = None,
        commands: Optional[list] = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise NodeRegistryError("A pairing request needs a node name.")
        if not endpoint.startswith(("http://", "https://")):
            raise NodeRegistryError("A pairing request needs an http(s) endpoint.")
        if not token:
            raise NodeRegistryError("A pairing request needs the node's bearer token.")
        request_id = _new_id("pairreq")
        node_id = _new_id("node")
        entry = {
            "requestId": request_id,
            "nodeId": node_id,
            "name": name.strip(),
            "platform": platform.strip() or "unknown",
            "endpoint": endpoint.rstrip("/"),
            "caps": list(caps or []),
            "commands": list(commands or []),
            "requestedAt": _now_ms(),
        }
        # The token is stored with the pending request, not in the node record
        # until approval; the registry never logs it.
        entry["token"] = token
        self.pending[request_id] = entry
        self._save()
        return {k: v for k, v in entry.items() if k != "token"}

    def approve_pairing(self, request_id: str) -> dict[str, Any]:
        entry = self.pending.pop(request_id, None)
        if entry is None:
            raise NodeRegistryError(f"No pending pairing request '{request_id}'.")
        node = {
            "nodeId": entry["nodeId"],
            "name": entry["name"],
            "platform": entry["platform"],
            "endpoint": entry["endpoint"],
            "caps": entry["caps"],
            "commands": entry["commands"],
            "token": entry.pop("token"),
            "pairingIdentity": _new_id("pair", 8),
            "pairingGeneration": _new_id("gen", 4),
            "pairedAt": _now_ms(),
            "lastSeenAt": _now_ms(),
        }
        self.nodes_by_id[node["nodeId"]] = node
        self.nodes_by_conn[node["pairingIdentity"]] = node["nodeId"]
        self._save()
        record = dict(node)
        record.pop("token", None)
        record["connected"] = True
        return record

    def reject_pairing(self, request_id: str) -> dict[str, Any]:
        entry = self.pending.pop(request_id, None)
        if entry is None:
            raise NodeRegistryError(f"No pending pairing request '{request_id}'.")
        self._save()
        return {"requestId": request_id, "nodeId": entry["nodeId"], "rejected": True}

    def remove_node(self, node_id: str) -> dict[str, Any]:
        node = self.nodes_by_id.pop(node_id, None)
        if node is None:
            raise NodeRegistryError(f"No paired node '{node_id}'.")
        if node.get("pairingIdentity"):
            self.nodes_by_conn.pop(str(node["pairingIdentity"]), None)
        self._save()
        return {"nodeId": node_id, "name": node.get("name"), "removed": True}

    # -- invoke -----------------------------------------------------------

    def _post_invoke(
        self,
        node: dict[str, Any],
        command: str,
        params: dict,
        request_id: str,
        idempotency_key: str,
        timeout_s: float,
    ) -> tuple[int, bytes]:
        """POST ``{endpoint}/invoke``; returns ``(http_status, payload)``.

        Raises the raw ``urllib`` error on failure (``HTTPError`` carries
        the status for a node refusal; ``URLError``/``TimeoutError`` for a
        route failure) so callers classify it themselves.
        """
        body = json.dumps(
            {
                "id": request_id,
                "command": command,
                "params": dict(params or {}),
                "idempotencyKey": idempotency_key,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{node['endpoint']}/invoke",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {node['token']}",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()

    def probe(
        self,
        node_id: str,
        command: str = "device.status",
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """One structured liveness probe of a paired node.

        Returns ``{"answered", "http_status", "error", "payload"}``: a node
        that answers refreshes ``lastSeenAt`` exactly like a successful
        invoke; a node that answers with an HTTP error proves the route
        and reports its status (401/403 is an auth failure, anything else
        is the node's own refusal); a node that does not answer at all
        reports the transport error. Never raises for a node-side failure
        — only an unknown node or an empty command raises.
        """
        node = self.nodes_by_id.get(node_id)
        if node is None:
            raise NodeRegistryError(f"No paired node '{node_id}'.")
        if not command:
            raise NodeRegistryError("probe needs a command.")
        try:
            status, payload = self._post_invoke(
                node, command, {}, _new_id("probe"), _new_id("probe"), timeout_s
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            return {
                "answered": True,
                "http_status": exc.code,
                "error": detail or None,
                "payload": None,
            }
        except Exception as exc:  # health: allow BLE001 -- probe: any failure reads as unavailable
            return {
                "answered": False,
                "http_status": None,
                "error": str(exc),
                "payload": None,
            }
        node["lastSeenAt"] = _now_ms()
        self._save()
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (TypeError, ValueError):
            decoded = payload.decode("utf-8", "replace")
        return {
            "answered": True,
            "http_status": status,
            "error": None,
            "payload": decoded,
        }

    def invoke(
        self,
        node_id: str,
        command: str,
        params: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        timeout_s: float = _INVOKE_TIMEOUT_S,
    ) -> Any:
        """POST ``{endpoint}/invoke``; returns the node's decoded payload.

        A node that answers refreshes ``lastSeenAt``. A node API refusal is
        the node's own error and surfaces with its status and message.
        """
        node = self.nodes_by_id.get(node_id)
        if node is None:
            raise NodeRegistryError(f"No paired node '{node_id}'.")
        if not command:
            raise NodeRegistryError("invoke needs a command.")
        try:
            _, payload = self._post_invoke(
                node,
                command,
                params or {},
                idempotency_key or _new_id("invoke"),
                idempotency_key or _new_id("idem"),
                timeout_s,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise NodeRegistryError(
                f"Node '{node_id}' refused {command}: HTTP {exc.code} {detail}"
            ) from exc
        except Exception as exc:
            raise NodeRegistryError(
                f"Node '{node_id}' did not answer {command}: {exc}"
            ) from exc
        node["lastSeenAt"] = _now_ms()
        self._save()
        try:
            return json.loads(payload.decode("utf-8"))
        except (TypeError, ValueError):
            return payload.decode("utf-8", "replace")


def release_registry(registry: Optional[NodeRegistry], owned: bool) -> None:
    """Release the DB handle a :func:`get_registry` call owned, if any."""
    if registry is not None:
        release_service_db(owned, registry._db)


def get_registry(db) -> tuple[Optional[NodeRegistry], bool, Optional[str]]:
    """Registry for a service call, loaded fresh from ``state_meta``.

    Returns ``(registry, owned, error)``; the caller releases an owned DB
    handle with :func:`release_service_db` when it is done. No cross-call
    cache: the store is the source of truth, and a cached registry would keep
    a DB handle the caller already released.
    """
    db_handle, owned, error = resolve_service_db(db, write=True)
    if db_handle is None:
        return None, owned, error
    return NodeRegistry(db_handle), owned, None
