"""F9e ``nodes`` tool — paired-device discovery, pairing, and invoke.

Port of the external agent's nodes-tool (status/describe/pending/approve/reject/notify/
read wrappers/invoke) onto ``gateway.node_registry``, plus ``diagnose``: one
structured ``device.status`` probe classified across route, auth, pairing,
and reconnect (the node-connect skill's evidence). Pairing requests are
owner-registered (Hermes has no node-initiated gateway connection for a node
to arrive on), approve mints the pairing identity + generation, and invoke is
an HTTP POST to the node's own endpoint.

Deliberately dropped from the external agent, per the user ruling (2026-11-11): the
``BLOCKED_INVOKE_COMMANDS`` set (``system.run`` etc.) and the media/path-policy
invoke blocks — there are no hard-blocked invoke commands in Hermes; if a
node API itself refuses a command, that is the node's limit and it surfaces
as the node's error. Media actions (``camera_snap``/``camera_clip``/
``photos_latest``/``screen_record``/``screen_snapshot``) are not separate
actions: Hermes has no node media pipeline, so those commands go through the
generic ``invoke`` and return the node's payload as-is.

Owner-only throughout (``tools.gateway_owner.requester_is_owner``); the
telethon adapter hides the tool from every non-owner sender, and tenant
sessions never see it.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Optional

from gateway.node_registry import NodeRegistryError, get_registry, release_registry
from tools.gateway_owner import owner_only_error, requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger("tools.nodes")

# The diagnose probe: the lightest read the node API offers. A node that
# answers it proves route, auth, pairing, and reconnect in one round trip.
_PROBE_COMMAND = "device.status"
_PROBE_TIMEOUT_S = 10.0

_STEP_STATES = ("ok", "failed", "pending", "skipped", "unknown")

_READ_COMMANDS = {
    "camera_list": "camera.list",
    "notifications_list": "notifications.list",
    "device_status": "device.status",
    "device_info": "device.info",
    "device_permissions": "device.permissions",
    "device_health": "device.health",
}

_PTZ_OPERATIONS = ("status", "set", "move", "home")
_NOTIFICATION_ACTIONS = ("open", "dismiss", "reply")
_LOCATION_ACCURACY = ("coarse", "balanced", "precise")


def _with_registry(kwargs: dict, fn):
    registry_obj, owned, error = get_registry(kwargs.get("db"))
    if registry_obj is None:
        return {"ok": False, "error": error or "Node registry is unavailable."}
    try:
        return fn(registry_obj)
    except NodeRegistryError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        release_registry(registry_obj, owned)


def _resolve_node(registry_obj, query: str) -> Optional[str]:
    node_id = registry_obj.resolve_node_id(query or "")
    return node_id


def _invoke(
    registry_obj,
    node_query: str,
    command: str,
    params: Optional[dict],
    timeout_ms: Optional[int] = None,
) -> dict[str, Any]:
    node_id = _resolve_node(registry_obj, node_query)
    if node_id is None:
        return {"ok": False, "error": f"No paired node matches '{node_query}'."}
    try:
        payload = registry_obj.invoke(
            node_id,
            command,
            params or {},
            idempotency_key=f"invoke:{secrets.token_hex(8)}",
            timeout_s=(timeout_ms / 1000.0) if timeout_ms else 30.0,
        )
    except NodeRegistryError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "node": node_id, "command": command, "result": payload}


def _status(args: dict, kwargs: dict) -> dict[str, Any]:
    def run(registry_obj):
        return {"ok": True, "action": "status", "nodes": registry_obj.list_nodes()}

    return _with_registry(kwargs, run)


def _describe(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    if not node_query:
        return {
            "ok": False,
            "action": "describe",
            "error": "node is required for describe.",
        }

    def run(registry_obj):
        node_id = _resolve_node(registry_obj, node_query)
        if node_id is None:
            return {
                "ok": False,
                "action": "describe",
                "error": f"No paired node matches '{node_query}'.",
            }
        return {
            "ok": True,
            "action": "describe",
            "node": registry_obj.get_node(node_id),
        }

    return _with_registry(kwargs, run)


def _pending(args: dict, kwargs: dict) -> dict[str, Any]:
    def run(registry_obj):
        return {"ok": True, "action": "pending", "pending": registry_obj.list_pending()}

    return _with_registry(kwargs, run)


def _request(args: dict, kwargs: dict) -> dict[str, Any]:
    name = str(args.get("name") or args.get("node") or "").strip()
    platform = str(args.get("platform") or "").strip()
    endpoint = str(args.get("endpoint") or "").strip()
    token = str(args.get("token") or "").strip()
    caps = args.get("caps") or []
    commands = args.get("commands") or []

    def run(registry_obj):
        entry = registry_obj.request_pairing(
            name=name,
            platform=platform,
            endpoint=endpoint,
            token=token,
            caps=list(caps) if isinstance(caps, list) else [],
            commands=list(commands) if isinstance(commands, list) else [],
        )
        return {"ok": True, "action": "request", "request": entry}

    return _with_registry(kwargs, run)


def _approve(args: dict, kwargs: dict) -> dict[str, Any]:
    request_id = str(args.get("requestId") or "").strip()
    if not request_id:
        return {"ok": False, "action": "approve", "error": "requestId is required."}

    def run(registry_obj):
        node = registry_obj.approve_pairing(request_id)
        return {"ok": True, "action": "approve", "node": node}

    return _with_registry(kwargs, run)


def _reject(args: dict, kwargs: dict) -> dict[str, Any]:
    request_id = str(args.get("requestId") or "").strip()
    if not request_id:
        return {"ok": False, "action": "reject", "error": "requestId is required."}

    def run(registry_obj):
        return {
            "ok": True,
            "action": "reject",
            **registry_obj.reject_pairing(request_id),
        }

    return _with_registry(kwargs, run)


def _remove(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    if not node_query:
        return {"ok": False, "action": "remove", "error": "node is required."}

    def run(registry_obj):
        node_id = _resolve_node(registry_obj, node_query)
        if node_id is None:
            return {
                "ok": False,
                "action": "remove",
                "error": f"No paired node matches '{node_query}'.",
            }
        return {"ok": True, "action": "remove", **registry_obj.remove_node(node_id)}

    return _with_registry(kwargs, run)


def _notify(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    if not node_query:
        return {"ok": False, "action": "notify", "error": "node is required."}
    if not title and not body:
        return {"ok": False, "action": "notify", "error": "title or body is required."}
    params = {"title": title, "body": body}
    for key in ("sound", "priority", "delivery"):
        if args.get(key) is not None:
            params[key] = args[key]

    def run(registry_obj):
        result = _invoke(registry_obj, node_query, "system.notify", params)
        return {**result, "action": "notify"}

    return _with_registry(kwargs, run)


def _read_wrapper(action: str, args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    if not node_query:
        return {"ok": False, "action": action, "error": "node is required."}

    def run(registry_obj):
        result = _invoke(registry_obj, node_query, _READ_COMMANDS[action], {})
        return {**result, "action": action}

    return _with_registry(kwargs, run)


def _notifications_action(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    key = str(args.get("notificationKey") or "").strip()
    action_name = str(args.get("notificationAction") or "").strip().lower()
    if not node_query:
        return {
            "ok": False,
            "action": "notifications_action",
            "error": "node is required.",
        }
    if not key:
        return {
            "ok": False,
            "action": "notifications_action",
            "error": "notificationKey is required.",
        }
    if action_name not in _NOTIFICATION_ACTIONS:
        return {
            "ok": False,
            "action": "notifications_action",
            "error": "notificationAction must be open, dismiss, or reply.",
        }
    reply_text = str(args.get("notificationReplyText") or "").strip()
    if action_name == "reply" and not reply_text:
        return {
            "ok": False,
            "action": "notifications_action",
            "error": "notificationReplyText is required when notificationAction is reply.",
        }

    def run(registry_obj):
        result = _invoke(
            registry_obj,
            node_query,
            "notifications.actions",
            {"key": key, "action": action_name, "replyText": reply_text or None},
        )
        return {**result, "action": "notifications_action"}

    return _with_registry(kwargs, run)


def _location_get(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    if not node_query:
        return {"ok": False, "action": "location_get", "error": "node is required."}
    accuracy = args.get("desiredAccuracy")
    if accuracy is not None and accuracy not in _LOCATION_ACCURACY:
        return {
            "ok": False,
            "action": "location_get",
            "error": "desiredAccuracy must be coarse, balanced, or precise.",
        }
    params: dict[str, Any] = {}
    if args.get("maxAgeMs") is not None:
        params["maxAgeMs"] = args["maxAgeMs"]
    if accuracy is not None:
        params["desiredAccuracy"] = accuracy
    if args.get("locationTimeoutMs") is not None:
        params["timeoutMs"] = args["locationTimeoutMs"]

    def run(registry_obj):
        result = _invoke(registry_obj, node_query, "location.get", params)
        return {**result, "action": "location_get"}

    return _with_registry(kwargs, run)


def _which(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    bins = args.get("bins")
    if not node_query:
        return {"ok": False, "action": "which", "error": "node is required."}
    if not isinstance(bins, list) or not bins:
        return {
            "ok": False,
            "action": "which",
            "error": "bins (a non-empty list) is required.",
        }

    def run(registry_obj):
        result = _invoke(registry_obj, node_query, "system.which", {"bins": bins})
        return {**result, "action": "which"}

    return _with_registry(kwargs, run)


def _camera_ptz(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    device_id = str(args.get("deviceId") or "").strip()
    operation = str(args.get("ptzOperation") or "").strip().lower()
    if not node_query:
        return {"ok": False, "action": "camera_ptz", "error": "node is required."}
    if not device_id:
        return {"ok": False, "action": "camera_ptz", "error": "deviceId is required."}
    if operation not in _PTZ_OPERATIONS:
        return {
            "ok": False,
            "action": "camera_ptz",
            "error": "ptzOperation must be status, set, move, or home.",
        }
    axes = {
        k: args[k]
        for k in ("panDegrees", "tiltDegrees", "zoomPercent")
        if args.get(k) is not None
    }
    if operation in ("status", "home") and axes:
        return {
            "ok": False,
            "action": "camera_ptz",
            "error": f"{operation} does not accept axis values.",
        }
    if operation in ("set", "move") and not axes:
        return {
            "ok": False,
            "action": "camera_ptz",
            "error": f"{operation} requires at least one PTZ axis.",
        }
    if operation == "status":
        command, params = "camera.ptz.status", {"deviceId": device_id}
    elif operation == "home":
        command, params = (
            "camera.ptz.control",
            {"deviceId": device_id, "operation": "home"},
        )
    else:
        command = "camera.ptz.control"
        params = {
            "deviceId": device_id,
            "operation": operation,
            "set" if operation == "set" else "delta": axes,
        }

    def run(registry_obj):
        result = _invoke(registry_obj, node_query, command, params)
        return {**result, "action": "camera_ptz"}

    return _with_registry(kwargs, run)


def _step(name: str, state: str, detail: str = "") -> dict[str, Any]:
    assert state in _STEP_STATES, state
    return {"name": name, "state": state, "detail": detail}


def _diagnose(args: dict, kwargs: dict) -> dict[str, Any]:
    """One probe, classified into the four connection steps.

    Route → auth → pairing → reconnect, in the order a connection fails:
    the probe is one real ``device.status`` invoke against the paired
    node, and the first failed transition names the fix. A node that is
    not paired yet is classified from the pending list instead — no
    probe exists until pairing approves it. ``ok`` reports the
    diagnosis, not the node: a broken node still diagnoses ``ok`` with
    a ``verdict`` naming what broke.
    """
    node_query = str(args.get("node") or "").strip()
    if not node_query:
        return {
            "ok": False,
            "action": "diagnose",
            "error": "node is required for diagnose.",
        }
    timeout_ms = args.get("timeoutMs")
    timeout_s = (
        (timeout_ms / 1000.0)
        if isinstance(timeout_ms, int) and timeout_ms > 0
        else _PROBE_TIMEOUT_S
    )

    def run(registry_obj):
        node_id = _resolve_node(registry_obj, node_query)
        if node_id is None:
            pending = [
                entry
                for entry in registry_obj.list_pending()
                if str(entry.get("name") or "") == node_query
            ]
            if pending:
                entry = pending[0]
                return {
                    "ok": True,
                    "action": "diagnose",
                    "node": node_query,
                    "verdict": "pairing_pending",
                    "requestId": entry["requestId"],
                    "steps": [
                        _step("route", "unknown", "No paired node to probe yet."),
                        _step(
                            "auth",
                            "unknown",
                            "Auth is proven at pairing, not before.",
                        ),
                        _step(
                            "pairing",
                            "pending",
                            f"Pairing request {entry['requestId']} waits for approval.",
                        ),
                        _step(
                            "reconnect",
                            "skipped",
                            "Nothing to reconnect until the node is paired.",
                        ),
                    ],
                    "probe": None,
                    "next_step": (
                        f"Approve the exact pairing request {entry['requestId']} — "
                        "re-list pending first, never approve by position."
                    ),
                }
            return {
                "ok": True,
                "action": "diagnose",
                "node": node_query,
                "verdict": "not_paired",
                "requestId": None,
                "steps": [
                    _step("route", "unknown", "No paired node to probe."),
                    _step("auth", "unknown", "No token is registered for this node."),
                    _step("pairing", "failed", "No pairing request exists for this node."),
                    _step(
                        "reconnect",
                        "skipped",
                        "Nothing to reconnect until the node is paired.",
                    ),
                ],
                "probe": None,
                "next_step": (
                    "Open a pairing request: the node operator supplies its endpoint "
                    "and bearer token out of band, then approve the request."
                ),
            }
        node = registry_obj.get_node(node_id)
        was_connected = bool(node.get("connected"))
        last_seen = node.get("lastSeenAt")
        age_s = (
            max(0, int(time.time() * 1000 - int(last_seen)) // 1000)
            if last_seen
            else None
        )
        probe = registry_obj.probe(node_id, _PROBE_COMMAND, timeout_s)
        # The probe refreshes lastSeenAt on success: re-read so the record
        # reports the state the probe just proved, not the stale one.
        node = registry_obj.get_node(node_id)
        base = {
            "ok": True,
            "action": "diagnose",
            "node": node,
            "probe": probe,
        }
        if probe["answered"] and probe["http_status"] is not None and probe["http_status"] < 400:
            reconnect_detail = "Answered the probe just now."
            if not was_connected:
                reconnect_detail += " It was stale — this probe reconnected it."
            return {
                **base,
                "verdict": "healthy",
                "requestId": None,
                "steps": [
                    _step("route", "ok", "The probe reached the node."),
                    _step("auth", "ok", "The node accepted the token."),
                    _step("pairing", "ok", "The node is paired."),
                    _step("reconnect", "ok", reconnect_detail),
                ],
                "next_step": "No fix needed — the node answers.",
            }
        if probe["answered"] and probe["http_status"] in (401, 403):
            return {
                **base,
                "verdict": "auth_failed",
                "requestId": None,
                "steps": [
                    _step("route", "ok", "The probe reached the node."),
                    _step(
                        "auth",
                        "failed",
                        f"Token rejected: HTTP {probe['http_status']}.",
                    ),
                    _step("pairing", "ok", "The node is paired."),
                    _step(
                        "reconnect",
                        "skipped",
                        "Auth must pass before reconnect means anything.",
                    ),
                ],
                "next_step": (
                    "Re-pair with a fresh token: open a new pairing request with the "
                    "token the node operator supplies now, then approve it. The old "
                    "token is stale — never reuse it."
                ),
            }
        if probe["answered"]:
            return {
                **base,
                "verdict": "node_error",
                "requestId": None,
                "steps": [
                    _step("route", "ok", "The probe reached the node."),
                    _step("auth", "ok", "The node accepted the token."),
                    _step("pairing", "ok", "The node is paired."),
                    _step(
                        "reconnect",
                        "failed",
                        f"Node refused {_PROBE_COMMAND}: HTTP {probe['http_status']}.",
                    ),
                ],
                "next_step": (
                    "The node's own API refused the probe — fix it on the node side, "
                    "then run diagnose again."
                ),
            }
        age_detail = f" Last seen {age_s}s ago." if age_s is not None else ""
        return {
            **base,
            "verdict": "unreachable",
            "requestId": None,
            "steps": [
                _step(
                    "route",
                    "failed",
                    f"The probe never reached the node: {probe['error']}",
                ),
                _step(
                    "auth",
                    "unknown",
                    "Never reached the node, so auth was never tested.",
                ),
                _step("pairing", "ok", "The node is paired."),
                _step("reconnect", "failed", f"The node stopped answering.{age_detail}"),
            ],
            "next_step": (
                "Check the route from the host: the endpoint must resolve and answer "
                "— DNS, TLS, or the node being down."
            ),
        }

    return _with_registry(kwargs, run)


def _invoke_action(args: dict, kwargs: dict) -> dict[str, Any]:
    node_query = str(args.get("node") or "").strip()
    command = str(args.get("invokeCommand") or args.get("command") or "").strip()
    if not node_query:
        return {"ok": False, "action": "invoke", "error": "node is required."}
    if not command:
        return {"ok": False, "action": "invoke", "error": "invokeCommand is required."}
    params = args.get("params") or args.get("invokeParams") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            return {
                "ok": False,
                "action": "invoke",
                "error": "params must be a JSON object.",
            }
    if not isinstance(params, dict):
        return {
            "ok": False,
            "action": "invoke",
            "error": "params must be a JSON object.",
        }
    timeout_ms = args.get("timeoutMs")

    def run(registry_obj):
        result = _invoke(
            registry_obj,
            node_query,
            command,
            params,
            timeout_ms=timeout_ms if isinstance(timeout_ms, int) else None,
        )
        return {**result, "action": "invoke"}

    return _with_registry(kwargs, run)


_ACTIONS = {
    "status": _status,
    "describe": _describe,
    "diagnose": _diagnose,
    "pending": _pending,
    "request": _request,
    "approve": _approve,
    "reject": _reject,
    "remove": _remove,
    "notify": _notify,
    "camera_list": lambda a, k: _read_wrapper("camera_list", a, k),
    "notifications_list": lambda a, k: _read_wrapper("notifications_list", a, k),
    "device_status": lambda a, k: _read_wrapper("device_status", a, k),
    "device_info": lambda a, k: _read_wrapper("device_info", a, k),
    "device_permissions": lambda a, k: _read_wrapper("device_permissions", a, k),
    "device_health": lambda a, k: _read_wrapper("device_health", a, k),
    "notifications_action": _notifications_action,
    "location_get": _location_get,
    "which": _which,
    "camera_ptz": _camera_ptz,
    "invoke": _invoke_action,
}


def nodes(args: Optional[dict] = None, **kwargs) -> str:
    """Paired devices: status/describe/diagnose/pending/request/approve/reject/remove/notify/invoke + read wrappers.

    Returns a JSON string: the registry rejects any other handler result type.
    """
    args = dict(args or {})
    args.update(kwargs)
    if not requester_is_owner(kwargs.get("agent")):
        return owner_only_error("nodes")
    action = str(args.get("action") or "status").strip().lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return tool_error(f"Unknown nodes action '{action}'.", action=action, ok=False)
    try:
        return json.dumps(handler(args, kwargs), ensure_ascii=False)
    except (
        Exception
    ) as exc:  # fail-closed: a broken node registry never takes the turn down
        logger.error("nodes failed", exc_info=True)
        return tool_error(f"nodes {action} failed: {exc}", action=action, ok=False)


_NODES_SCHEMA = {
    "name": "nodes",
    "description": (
        "Discover, configure, and control paired devices (owner only). Actions: status lists paired "
        "nodes with live connected state; describe inspects a node; diagnose probes one node and "
        "classifies the failure across route, auth, pairing, and reconnect (verdict healthy, "
        "unreachable, auth_failed, node_error, pairing_pending, or not_paired); pending lists pairing "
        "requests; request opens a pairing request (Hermes has no node-initiated pairing channel, so the owner "
        "registers the node's endpoint + token); approve/reject decide a request; remove unpairs a node; "
        "notify sends a system notification; camera_list, notifications_list, device_status, device_info, "
        "device_permissions, device_health, location_get, which, camera_ptz, notifications_action are "
        "read/command wrappers; invoke runs any node command — no commands are blocked, a node API refusal "
        "is the node's own error. Media commands go through invoke and return the node's payload as-is."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "describe",
                    "diagnose",
                    "pending",
                    "request",
                    "approve",
                    "reject",
                    "remove",
                    "notify",
                    "camera_list",
                    "notifications_list",
                    "device_status",
                    "device_info",
                    "device_permissions",
                    "device_health",
                    "location_get",
                    "which",
                    "camera_ptz",
                    "notifications_action",
                    "invoke",
                ],
                "description": "Nodes action",
            },
            "node": {"type": "string", "description": "Paired node id or display name"},
            "name": {
                "type": "string",
                "description": "Node display name (request action)",
            },
            "platform": {
                "type": "string",
                "description": "Node platform, e.g. android (request action)",
            },
            "endpoint": {
                "type": "string",
                "description": "Node HTTP endpoint (request action)",
            },
            "token": {
                "type": "string",
                "description": "Node bearer token (request action)",
            },
            "caps": {
                "type": "array",
                "description": "Node capability ids (request action)",
            },
            "commands": {
                "type": "array",
                "description": "Node command ids (request action)",
            },
            "requestId": {
                "type": "string",
                "description": "Pairing request id (approve/reject)",
            },
            "title": {"type": "string", "description": "Notification title"},
            "body": {"type": "string", "description": "Notification body"},
            "invokeCommand": {
                "type": "string",
                "description": "Node command for invoke",
            },
            "timeoutMs": {
                "type": "integer",
                "description": "Probe/invoke timeout in milliseconds (diagnose defaults to 10000)",
            },
            "params": {"type": "object", "description": "Command params for invoke"},
            "bins": {"type": "array", "description": "Executable names for which"},
            "deviceId": {
                "type": "string",
                "description": "Camera device id for camera_ptz",
            },
            "ptzOperation": {
                "type": "string",
                "description": "status, set, move, or home",
            },
            "notificationKey": {"type": "string", "description": "Notification key"},
            "notificationAction": {
                "type": "string",
                "description": "open, dismiss, or reply",
            },
        },
    },
}


registry.register(
    name="nodes",
    toolset="nodes",
    schema=_NODES_SCHEMA,
    handler=lambda args, **kw: nodes(
        args,
        db=kw.get("db"),
        agent=kw.get("agent"),
    ),
    emoji="📱",
)
