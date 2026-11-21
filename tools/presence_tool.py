#!/usr/bin/env python3
"""F9a presence service — thin agent tool over the live snapshot sources Hermes already has.

``presence`` reports live people, devices and recent activity, computed at query
time. There is NO presence store (same shape as the external agent: ``presence.query`` is
computed live from the connection snapshot + ``NodeSession`` list,
``src/gateway/server-methods/presence.ts:230``; the tool is a thin RPC client,
``src/agents/tools/presence-tool.ts:12``). Hermes' snapshot sources:

1. SessionDB Telegram rows across ALL profiles — the current home's DB (the
   live handle the inline executor passes, else the session_search read-only
   path) plus every other profile's ``state.db`` opened read-only via
   ``tools/session_search_tool._resolve_profile_db``. Rows carry ``user_id``,
   ``display_name``, ``chat_id``, ``chat_type`` and ``last_active``. A row is
   attributable to a person only when its session key carries a user segment
   (``gateway/session.py::build_session_key``: ``<ns>:<platform>:<chat_type>``
   + chat/thread + user); an /allowgc shared-group session has no user segment,
   so its activity is group-level and is NOT attributed to any one person.
2. ``PairingStore.list_approved("telegram")`` on the launch home — admitted
   users who have no session row yet (online=false, no activity).
3. ``SwarmObserver`` live cards (``gateway/swarm_observer.get_active_observer``)
   — a person with a running/stalling delegate card has live agent work right
   now. Reachable only inside a gateway process; None elsewhere (best-effort).
4. The dashboard WS heartbeat (``gateway/scale_to_zero.dashboard_client_last_seen``)
   — the dashboard client device, online iff the last WS frame is inside the
   gateway's own ``scale_to_zero.idle_timeout_minutes`` window.
5. The gateway loop heartbeat (``state/gateway.heartbeat``, 30 s cadence, 150 s
   TTL — the ``gateway/memory_status.py`` pattern) — a gateway ``alive`` flag so
   "nobody online" is distinguishable from "gateway down".

``online`` is a strictly live signal: the requester (mid-turn right now) or a
person with a live swarm card. Everyone else reports ``online=false`` with
``lastActivityAt`` — no invented recency window turns "messaged an hour ago"
into "online". The external agent retains no offline activity at all ("Offline activity
is not retained", ``presence.ts:347``); Hermes DOES retain session activity, so
the port reports it instead of dropping it — that is the one deliberate
divergence, and the field is named ``lastActivityAt`` so the two are never
confused.

NO location, NO network/IP anywhere in the output (user ruling 2026-11-11):
The external agent's ``include`` offers ``devices``/``network``/``location`` and its
``PresenceLocationSchema`` carries approximate IP geography; the Hermes port
accepts only ``devices`` and never emits IP, network or location fields.

OWNER GATE (user ruling 2026-11-11: the requester must be the owner,
non-owner gets nothing), fail-closed, in three layers:

1. Tenant sessions: ``presence`` is NOT in ``agent/tenant_policy.SUPPORTED``,
   so ``tenant_tool_definitions`` strips it from tenant catalogs and
   ``tenant_tool_dispatch`` denies any attempt. Structural — nothing to add.
2. Telegram adapter (``plugins/platforms/telegram/telethon_adapter.py``):
   ``disabled_tools_for_source`` returns ``["presence"]`` for any sender who
   is not the LAUNCH owner (``_is_launch_owner_user``, authorization home),
   DM or group, so the model never sees the tool in a non-owner turn.
3. Handler (this file, defense in depth): a live tenant scope denies; a
   Telegram DM personal context must match the LAUNCH owner's id — NOT the
   ``tools/skill_manager_tool._personal_context_is_owner`` pattern, which
   answers "profile owner" and would pass any profiled user in their own DM
   (every tg-<id> profile stamps its own user as its OWNER_TELEGRAM_ID);
   local surfaces (``cli``/``local``) run as the owner-operator and are
   allowed. A
   Telegram group turn has no DM personal context, so the handler trusts the
   adapter layer there — a model hallucinating a ``presence`` call in a
   non-owner group turn would reach the handler. That residual is accepted:
   the catalog is the enforcement boundary for group turns, exactly as it is
   for every other owner-gated tool.

No ``check_fn``: like ``github_identity_status``/``session_status`` the tool
stays visible when a store is missing and reports per-section availability.
A ``check_fn`` is TTL-cached per home and cannot answer a per-requester
question.
"""

import json
import logging
import time
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger("tools.presence")

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 100
# Gateway loop heartbeat TTL (gateway/memory_status.py): writer cadence is 30 s;
# 150 s tolerates a briefly stalled loop without letting a long-dead gateway's
# last sample pose as current.
_GATEWAY_HEARTBEAT_FRESH_TTL_S = 150.0
# A swarm card in one of these states is live work for its route's user.
_LIVE_CARD_STATES = frozenset({"running", "stalling"})
_LOCAL_PLATFORMS = frozenset({"cli", "local"})


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _coerce_epoch(value: Any) -> Optional[float]:
    """Epoch seconds as a float, or None for missing/unparseable values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")) or result < 0:
        return None
    return result


def _ms(epoch_seconds: Optional[float]) -> Optional[int]:
    """Epoch seconds -> integer epoch milliseconds (the external agent presence units)."""
    if epoch_seconds is None:
        return None
    return int(epoch_seconds * 1000)


def _person_id(user_id: Any) -> str:
    return f"telegram:{user_id}"


def _attributable_user_id(row: dict[str, Any]) -> Optional[str]:
    """Telegram user id a session row's activity belongs to, or None.

    The session key is authoritative: ``<ns>:telegram:<slot>:<chat>[:<thread>]
    [:<user>]`` (``gateway/session.py::build_session_key``). DMs carry the peer
    as ``chat_id`` (no user segment); groups/threads carry the participant as
    the last segment only when the session is per-user; an /allowgc shared
    session has no user segment, so it attributes to nobody. The row's
    ``user_id`` column is used only where the key confirms attribution (a DM
    row, or a group/thread row with a user segment) — the shared row's
    ``user_id`` is its creator, not its current speaker.
    """
    key = str(row.get("session_key") or "")
    chat_type = str(row.get("chat_type") or "")
    parts = key.split(":") if key else []
    if len(parts) >= 4 and parts[1] == "telegram":
        slot = parts[2]
        if slot == "dm":
            return str(parts[3]) if parts[3] else None
        if slot in {"group", "forum"}:
            return str(parts[4]) if len(parts) == 5 and parts[4] else None
        if slot == "thread":
            return str(parts[5]) if len(parts) == 6 and parts[5] else None
        return None
    # Keyless or unparseable key: a DM row is still per-user by construction.
    if chat_type == "dm":
        user_id = row.get("user_id")
        return str(user_id) if user_id else None
    return None


# =========================================================================
# Owner gate (layer 3 — handler side)
# =========================================================================

def _requester_is_owner(agent: Any) -> bool:
    """True only when the requester is provably the owner (fail-closed).

    Tenant scope active -> False (non-owner by construction). Telegram DM
    personal context present -> its user must equal the LAUNCH owner's id
    (``owner_user_id`` on the launch home, NOT the context home: every
    tg-<id> profile stamps ``OWNER_TELEGRAM_ID=<its own user>`` into its own
    .env, so the context home would call every profiled user "the owner").
    Local surfaces (``cli``/``local``) run as the owner-operator -> True. A
    Telegram group turn carries no DM context, so the adapter layer owns it
    and the handler allows (see module docstring).
    """
    try:
        from agent.tenant_scope import current_tenant_scope

        if current_tenant_scope() is not None:
            return False
    except Exception:
        logger.debug("presence: tenant scope unreadable", exc_info=True)
        return False
    try:
        from gateway.telegram_personal_settings import current_personal_memory_context

        context = current_personal_memory_context()
    except Exception:
        logger.debug("presence: personal memory context unreadable", exc_info=True)
        return False
    if context:
        try:
            from hermes_cli.profiles import get_profile_dir
            from gateway.mongo_multiuser import owner_user_id

            return str(context.get("user_id") or "") == owner_user_id(get_profile_dir("default"))
        except Exception:
            logger.debug("presence: owner id unreadable", exc_info=True)
            return False
    platform = str(getattr(agent, "platform", "") or "").strip().lower() if agent is not None else ""
    if platform in _LOCAL_PLATFORMS or platform == "":
        # Local operator (CLI/TUI/desktop, or a registry call with no agent):
        # the person at the keyboard is the owner.
        return True
    if platform == "telegram":
        # Group turn: no DM context to check; the adapter's per-source
        # catalog filter is the gate (see module docstring).
        return True
    # Any other remote platform (discord, slack, ...) cannot prove the
    # requester is the Telegram owner: deny.
    return False


def _requester_telegram_id(agent: Any) -> Optional[str]:
    """Telegram user id of the requester, when the DM context carries it."""
    try:
        from gateway.telegram_personal_settings import current_personal_memory_context

        context = current_personal_memory_context()
    except Exception:
        logger.debug("presence: requester context unreadable", exc_info=True)
        return None
    if not context:
        return None
    user_id = str(context.get("user_id") or "").strip()
    return user_id or None


def _owner_telegram_id() -> Optional[str]:
    """Install owner's Telegram id, when the launch home's .env names it."""
    try:
        from hermes_cli.profiles import get_profile_dir
        from gateway.mongo_multiuser import owner_user_id

        return str(owner_user_id(get_profile_dir("default")))
    except Exception:
        logger.debug("presence: owner telegram id unreadable", exc_info=True)
        return None


# =========================================================================
# Snapshot sources (all live, no presence store)
# =========================================================================

def _iter_profile_dbs(current_db, owned_current: bool):
    """``(profile_name, SessionDB)`` for the current home plus every other live profile.

    The passed handle covers the current home (whatever profile the turn runs
    under); every other profile's ``state.db`` is opened read-only. Handles
    opened here are released by the caller. A profile whose DB cannot be
    opened is skipped, never fatal.
    """
    from tools.session_search_tool import _resolve_profile_db

    yield None, current_db
    try:
        from hermes_cli.profiles import get_profile_dir, list_profile_names
        from hermes_constants import get_hermes_home

        current_home = get_hermes_home()
    except Exception:
        logger.debug("presence: profile homes unreadable", exc_info=True)
        return
    for name in list_profile_names():
        try:
            home = get_profile_dir(name)
            if home.resolve() == current_home.resolve():
                continue
            db = _resolve_profile_db(name)
        except Exception:
            logger.debug("presence: skipping profile %r (DB unavailable)", name, exc_info=True)
            continue
        yield name, db


def _collect_people(db, owned_db) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``(people_by_id, db_errors)`` from SessionDB Telegram rows + approved list.

    People keyed by Telegram user id: display name (latest row wins),
    last activity (max ``last_active``/``started_at``), session count, chat
    types. Admitted users with no rows are added with no activity.
    """
    people: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def _touch(user_id: str, name: Optional[str], at: Optional[float],
               chat_type: Optional[str]) -> None:
        person = people.get(user_id)
        if person is None:
            person = {
                "id": _person_id(user_id), "user_id": str(user_id),
                "name": None, "last_activity": None,
                "sessions": 0, "chats": set(),
            }
            people[user_id] = person
        person["sessions"] += 1
        if chat_type:
            person["chats"].add(str(chat_type))
        # Rows arrive newest-first, so the first name seen for a user is the
        # latest display name; keep it unless a later row is strictly newer.
        if name and (person["name"] is None
                     or (at is not None and person["last_activity"] is not None
                         and at > person["last_activity"])):
            person["name"] = str(name)
        if at is not None and (person["last_activity"] is None or at > person["last_activity"]):
            person["last_activity"] = at

    # The current home's DB is read first (the passed handle, or our own
    # read-only handle on the current home); other profiles follow. The
    # 1000-row window is newest-first, so it covers the active population;
    # a profile with more Telegram sessions than that only loses its oldest.
    for profile_name, profile_db in _iter_profile_dbs(db, owned_db):
        if profile_db is None:
            continue
        try:
            rows = profile_db.list_sessions_rich(
                source="telegram", limit=1000, order_by_last_active=True,
                include_archived=True, include_hidden=True, include_children=True,
            )
        except Exception:
            errors.append(f"profile {profile_name or 'current'} sessions unreadable")
            logger.debug("presence: list_sessions_rich failed for %r", profile_name, exc_info=True)
            rows = []
        for row in rows:
            user_id = _attributable_user_id(row)
            if not user_id:
                continue
            at = _coerce_epoch(row.get("last_active")) or _coerce_epoch(row.get("started_at"))
            _touch(user_id, row.get("display_name"), at, row.get("chat_type"))
        if profile_name is not None:
            # _iter_profile_dbs opened this handle; release it now.
            from hermes_state_registry import release_or_close

            try:
                release_or_close(profile_db)
            except Exception:
                logger.debug("presence: releasing profile DB failed", exc_info=True)
    # Admitted users with no session row yet: present, never active.
    try:
        from hermes_cli.profiles import get_profile_dir
        from gateway.pairing import PairingStore

        store = PairingStore(profile=None)
        # PairingStore() with no profile resolves under the CURRENT home;
        # presence needs the launch home's admission list, so rebuild it
        # against the default home when the turn runs under a profile.
        try:
            from hermes_constants import get_hermes_home

            if get_hermes_home().resolve() != get_profile_dir("default").resolve():
                store = PairingStore(profile="default")
        except Exception:
            logger.debug("presence: default-home PairingStore unreadable", exc_info=True)
        for user_id in store.list_approved("telegram") or []:
            uid = str(user_id)
            if uid and uid not in people:
                people[uid] = {
                    "id": _person_id(uid), "user_id": uid, "name": None,
                    "last_activity": None, "sessions": 0, "chats": set(),
                }
    except Exception:
        errors.append("approved-users list unreadable")
        logger.debug("presence: PairingStore read failed", exc_info=True)
    return people, errors


def _live_swarm_users(now: float) -> dict[str, float]:
    """``{telegram_user_id: card_started_at}`` for live swarm cards (gateway only)."""
    live: dict[str, float] = {}
    try:
        from gateway.swarm_observer import get_active_observer

        observer = get_active_observer()
    except Exception:
        logger.debug("presence: swarm observer unreadable", exc_info=True)
        return live
    if observer is None:
        return live
    try:
        cards = list(getattr(observer, "cards", {}).values())
    except Exception:
        logger.debug("presence: swarm cards unreadable", exc_info=True)
        return live
    for card in cards:
        try:
            if getattr(card, "state", "") not in _LIVE_CARD_STATES:
                continue
            route = getattr(card, "route", None)
            user_id = str(getattr(route, "user_id", "") or "") if route is not None else ""
            if not user_id:
                continue
            started = _coerce_epoch(getattr(card, "started_at", None)) or now
            if user_id not in live or started < live[user_id]:
                live[user_id] = started
        except Exception:
            logger.debug("presence: unreadable swarm card skipped", exc_info=True)
            continue
    return live


def _dashboard_device(now: float) -> Optional[dict[str, Any]]:
    """The dashboard client device, or None when no client ever attached."""
    try:
        from gateway.scale_to_zero import (
            dashboard_client_last_seen,
            parse_idle_timeout_seconds,
        )
        from gateway.run import _load_gateway_config

        last_seen = dashboard_client_last_seen()
    except Exception:
        logger.debug("presence: dashboard heartbeat unreadable", exc_info=True)
        return None
    if last_seen is None:
        return None
    idle_seconds = 120.0
    try:
        user_cfg = _load_gateway_config()
        gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
        stz = gw.get("scale_to_zero") if isinstance(gw, dict) else None
        idle_seconds = parse_idle_timeout_seconds(
            stz.get("idle_timeout_minutes") if isinstance(stz, dict) else None)
    except Exception:
        logger.debug("presence: dashboard idle timeout unreadable; using default", exc_info=True)
    online = (now - last_seen) <= idle_seconds
    return {
        "id": "dashboard",
        "name": "Dashboard",
        "kind": "client",
        "personIds": [],
        "online": online,
        "activity": {"at": _ms(last_seen), "source": "dashboard"},
        "connections": [{"platform": "web"}],
    }


def _gateway_status(now: float) -> dict[str, Any]:
    """``{alive, lastHeartbeatAt}`` from the gateway loop heartbeat file."""
    try:
        from hermes_cli.profiles import get_profile_dir
        from gateway.lifecycle_ledger import _read_json
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        heartbeat = _read_json(get_loop_heartbeat_path(get_profile_dir("default")))
    except Exception:
        logger.debug("presence: gateway heartbeat unreadable", exc_info=True)
        return {"alive": False, "lastHeartbeatAt": None}
    if not heartbeat:
        return {"alive": False, "lastHeartbeatAt": None}
    sampled_at = None
    try:
        from datetime import UTC, datetime

        raw = heartbeat.get("updated_at")
        if isinstance(raw, str) and raw:
            sampled_at = datetime.fromisoformat(raw)
            if sampled_at.tzinfo is None:
                sampled_at = sampled_at.replace(tzinfo=UTC)
            sampled_at = sampled_at.timestamp()
    except (TypeError, ValueError):
        sampled_at = None
    alive = sampled_at is not None and 0 <= (now - sampled_at) <= _GATEWAY_HEARTBEAT_FRESH_TTL_S
    return {"alive": alive, "lastHeartbeatAt": _ms(sampled_at)}


# =========================================================================
# Projection (the external agent result shape, minus location/network)
# =========================================================================

def _project_person(person: dict[str, Any], *, online: bool,
                    online_since: Optional[float], include_devices: bool) -> dict[str, Any]:
    last_activity = person["last_activity"]
    activity = (
        {"at": _ms(last_activity), "source": "telegram-interaction"}
        if last_activity is not None else None
    )
    projected: dict[str, Any] = {
        "id": person["id"],
        "platform": "telegram",
        "name": person["name"] or person["user_id"],
        "online": online,
        "activity": activity,
        "deviceCount": 1 if include_devices else 0,
        "sessions": person["sessions"],
        "chats": sorted(person["chats"]),
    }
    if online_since is not None:
        projected["onlineSince"] = _ms(online_since)
    if include_devices:
        projected["devices"] = [{
            "id": person["id"],
            "name": "Telegram",
            "kind": "client",
            "personIds": [person["id"]],
            "online": online,
            "activity": activity,
            "connections": [{"platform": "telegram"}],
        }]
    return projected


def _snapshot(db, owned_db, *, agent, current_session_id) -> dict[str, Any]:
    """The whole live snapshot: people, devices, gateway, errors."""
    now = time.time()
    people, errors = _collect_people(db, owned_db)
    live_swarm = _live_swarm_users(now)

    # The requester is online right now: their turn is running. Resolve who
    # that is — the DM context's user, else the install owner on a local
    # surface — and mark only that person online.
    requester_id = _requester_telegram_id(agent)
    if requester_id is None and agent is not None and str(getattr(agent, "platform", "") or "").strip().lower() in _LOCAL_PLATFORMS:
        requester_id = _owner_telegram_id()
    if requester_id is not None and requester_id in people:
        live_swarm.setdefault(requester_id, now)
    elif requester_id is not None:
        people[requester_id] = {
            "id": _person_id(requester_id), "user_id": requester_id, "name": None,
            "last_activity": None, "sessions": 0, "chats": set(),
        }
        live_swarm.setdefault(requester_id, now)

    dashboard = _dashboard_device(now)
    gateway = _gateway_status(now)

    projected_people = [
        _project_person(person, online=person["user_id"] in live_swarm,
                        online_since=live_swarm.get(person["user_id"]),
                        include_devices=False)
        for person in sorted(
            people.values(),
            key=lambda p: (p["user_id"] not in live_swarm,
                           -(p["last_activity"] or 0), p["user_id"]),
        )
    ]
    devices: list[dict[str, Any]] = []
    if dashboard is not None:
        devices.append(dashboard)
    return {
        "observedAt": _ms(now),
        "people": projected_people,
        "devices": devices,
        "gateway": gateway,
        "errors": errors,
        "live_swarm": live_swarm,
        "people_by_id": people,
    }


# =========================================================================
# Tool entry point
# =========================================================================

_PRESENCE_SCHEMA = {
    "name": "presence",
    "description": (
        "List live people, devices and recent activity on this gateway. "
        "Computed at query time from connected clients and session activity "
        "(no presence store). People are admitted Telegram users with their "
        "last activity; online means a turn or live subagent work is running "
        "for them right now. Devices are the dashboard client and each "
        "person's Telegram client. Owner only: a non-owner request gets "
        "nothing. No location or network data is ever reported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "person", "device"],
                "description": "List connected people (default), inspect a person, or inspect a connected device.",
            },
            "person": {
                "type": "string",
                "description": "For person: me, a person id (telegram:<user id> or the bare id), or an exact display name.",
            },
            "deviceId": {
                "type": "string",
                "description": "For device: the exact presence device id returned by this tool.",
            },
            "include": {
                "type": "array",
                "items": {"type": "string", "enum": ["devices"]},
                "description": "Optional device details per person. Only 'devices' is accepted; network and location are never reported.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum people returned (default 50, maximum 100); truncation is reported.",
            },
        },
        "required": [],
    },
}


def _presence_list_result(snap: dict[str, Any], limit: int,
                          include_person_devices: bool) -> str:
    """``list`` action: paged people (+ projected devices when asked)."""
    people = snap["people"]
    live_swarm = snap["live_swarm"]
    people_by_id = snap["people_by_id"]
    total = len(people)
    page = people[:limit]
    if include_person_devices:
        page = [
            _project_person(people_by_id[p["id"].split(":", 1)[1]],
                            online=p["online"],
                            online_since=(live_swarm.get(p["id"].split(":", 1)[1])),
                            include_devices=True)
            for p in page
        ]
    result: dict[str, Any] = {
        "observedAt": snap["observedAt"],
        "status": "ok",
        "people": page,
        "devices": snap["devices"],
        "totalPeople": total,
        "truncated": total > len(page),
        "gateway": snap["gateway"],
    }
    if snap["errors"]:
        result["message"] = "; ".join(snap["errors"])
    return _ok(**result)


def _presence_person_result(snap: dict[str, Any], agent: Any,
                            person: Optional[str]) -> str:
    """``person`` action: one person by id, name, or ``me``."""
    people_by_id = snap["people_by_id"]
    live_swarm = snap["live_swarm"]
    target = (person or "").strip()
    if not target:
        return tool_error("presence person needs a person (me, an id, or a display name)", success=False)
    if target.lower() == "me":
        requester_id = _requester_telegram_id(agent)
        if requester_id is None and agent is not None and str(getattr(agent, "platform", "") or "").strip().lower() in _LOCAL_PLATFORMS:
            requester_id = _owner_telegram_id()
        if requester_id is None:
            return _ok(observedAt=snap["observedAt"], status="identity-unavailable",
                       people=[], devices=snap["devices"], totalPeople=0, truncated=False,
                       gateway=snap["gateway"],
                       message="Could not determine the requester's identity.")
        target = _person_id(requester_id)
    candidates: list[dict[str, Any]] = []
    if target.startswith("telegram:"):
        person_row = people_by_id.get(target.split(":", 1)[1])
        candidates = [person_row] if person_row is not None else []
    elif target.isdecimal():
        person_row = people_by_id.get(target)
        candidates = [person_row] if person_row is not None else []
    else:
        candidates = [p for p in people_by_id.values() if (p["name"] or "") == target]
    if not candidates:
        return _ok(observedAt=snap["observedAt"], status="not-found",
                   people=[], devices=snap["devices"], totalPeople=0, truncated=False,
                   gateway=snap["gateway"],
                   message="No connected person matches. Offline activity is not retained beyond session history.")
    if len(candidates) > 1:
        return _ok(observedAt=snap["observedAt"], status="ambiguous",
                   people=[], devices=snap["devices"], totalPeople=0, truncated=False,
                   gateway=snap["gateway"],
                   message=f"Display name {target!r} matches {len(candidates)} people; use a person id.")
    person_row = candidates[0]
    projected = _project_person(
        person_row, online=person_row["user_id"] in live_swarm,
        online_since=live_swarm.get(person_row["user_id"]), include_devices=True)
    return _ok(observedAt=snap["observedAt"], status="ok", people=[projected],
               devices=snap["devices"], totalPeople=1, truncated=False,
               gateway=snap["gateway"])


def _presence_device_result(snap: dict[str, Any], device_id: Optional[str]) -> str:
    """``device`` action: one device by id (dashboard + per-person Telegram)."""
    people_by_id = snap["people_by_id"]
    live_swarm = snap["live_swarm"]
    target = (device_id or "").strip()
    if not target:
        return tool_error("presence device needs a deviceId", success=False)
    all_devices: list[dict[str, Any]] = list(snap["devices"])
    for person_row in people_by_id.values():
        all_devices.append({
            "id": person_row["id"], "name": "Telegram", "kind": "client",
            "personIds": [person_row["id"]],
            "online": person_row["user_id"] in live_swarm,
            "activity": (
                {"at": _ms(person_row["last_activity"]), "source": "telegram-interaction"}
                if person_row["last_activity"] is not None else None
            ),
            "connections": [{"platform": "telegram"}],
        })
    matches = [d for d in all_devices if d["id"] == target]
    if not matches:
        return _ok(observedAt=snap["observedAt"], status="not-found",
                   people=[], devices=[], totalPeople=0, truncated=False,
                   gateway=snap["gateway"], message="No connected device matches.")
    return _ok(observedAt=snap["observedAt"], status="ok", people=[], devices=matches,
               totalPeople=0, truncated=False, gateway=snap["gateway"])


def presence(action: str = "list", person: Optional[str] = None,
             deviceId: Optional[str] = None, include: Optional[list] = None,
             limit: int = _LIST_DEFAULT_LIMIT, db=None, agent=None,
             current_session_id: Optional[str] = None) -> str:
    """Report live people/devices/recent activity. Owner only."""
    if not _requester_is_owner(agent):
        return tool_error("presence is available to the owner only", success=False)

    action = str(action or "list").strip().lower()
    if action not in {"list", "person", "device"}:
        return tool_error("presence action must be list, person or device", success=False)
    try:
        limit = max(1, min(_LIST_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    include_devices = False
    if include is not None:
        if not isinstance(include, (list, tuple)):
            return tool_error("presence include must be a list", success=False)
        unknown = [str(item) for item in include if str(item) != "devices"]
        if unknown:
            return tool_error(
                "presence include accepts only 'devices'; network and location are never reported",
                success=False)
        include_devices = "devices" in [str(item) for item in include]

    owned_db = None
    try:
        from tools.sessions_tools import _resolve_db, _release

        db, owned, error = _resolve_db(db, write=False)
        if owned:
            owned_db = db
        if error is not None:
            return tool_error(f"presence: {error}", success=False)
        snap = _snapshot(db, owned_db, agent=agent, current_session_id=current_session_id)
    except Exception as exc:  # health: allow BLE001 -- tool boundary: snapshot failures surface as tool errors, never a crash
        logger.error("presence failed", exc_info=True)
        return tool_error(f"presence: {exc}", success=False)
    finally:
        if owned_db is not None:
            _release(True, owned_db)

    if action == "list":
        return _presence_list_result(snap, limit, include_devices)

    if action == "person":
        return _presence_person_result(snap, agent, person)

    # action == "device"
    return _presence_device_result(snap, deviceId)


registry.register(
    name="presence",
    toolset="presence",
    schema=_PRESENCE_SCHEMA,
    handler=lambda args, **kw: presence(
        action=args.get("action", "list"), person=args.get("person"),
        deviceId=args.get("deviceId"), include=args.get("include"),
        limit=args.get("limit", _LIST_DEFAULT_LIMIT),
        db=kw.get("db"), agent=kw.get("agent"),
        current_session_id=kw.get("current_session_id")),
    emoji="🟢")
