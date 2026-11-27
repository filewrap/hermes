#!/usr/bin/env python3
"""Shared store for the F9b-e gateway service layer (boards, portals, nodes).

Services keep their state in the home's ``state.db`` ``state_meta`` KV table
(``SessionDB.get_meta``/``set_meta``) — no new table, no new file, no new
database. Keys are namespaced ``gateway.<service>.<id>`` and values are JSON.
That is the same store the scheduler and kanban retag use for service
bookkeeping, and it survives restarts with the rest of Hermes state.

The external agent layer this ports keeps its registries in memory
(``node-registry.ts:143`` in-memory maps; the board store is per-session
memory behind the gateway). Hermes adds the ``state_meta`` write-through so
a board, portal allocation or paired node survives a gateway restart; the
live computation rule from F9a still holds — nothing here is a *snapshot*
store for presence, and ``connected``/``lastSeen`` on nodes is derived from
recorded invoke activity, not a polled liveness table.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.gateway_services")


def resolve_service_db(db, *, write: bool):
    """Usable SessionDB for a gateway service call.

    The passed handle when it carries the ``state_meta`` surface, else our
    own handle on the current home's ``state.db``. A managed worker's handle
    is a ``RuntimeSessionStore``: it mirrors the owner's write/lineage
    surface, not the SessionDB API, so reads open their own read-only handle
    and writes fail closed — a write must never silently land in a different
    home's DB. With no handle at all (registry dispatch outside a turn), the
    current home's own DB is the right one. Returns ``(db, owned, error)``;
    *owned* handles are released by the caller.
    """
    from agent.runtime_session_store import RuntimeSessionStore

    if db is None:
        pass  # open our own handle below
    elif isinstance(db, RuntimeSessionStore):
        if write:
            return (
                None,
                False,
                (
                    "gateway service write needs the live session store, "
                    "which is not available in this context"
                ),
            )
        db = None  # reads fall through to our own read-only handle
    else:
        needed = ("get_meta", "set_meta") if write else ("get_meta",)
        if all(callable(getattr(db, name, None)) for name in needed):
            return db, False, None
        if write:
            return (
                None,
                False,
                (
                    "gateway service write needs the live session store, "
                    "which is not available in this context"
                ),
            )
        db = None  # reads fall through to our own read-only handle
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        owned = SessionDB(db_path=get_hermes_home() / "state.db", read_only=not write)
        return owned, True, None
    except Exception:  # health: allow BLE001 -- any open failure means the DB is unavailable; the caller reports it
        logging.debug("gateway_services: cannot open SessionDB", exc_info=True)
        from hermes_state import format_session_db_unavailable

        return None, False, format_session_db_unavailable()


def release_service_db(owned, db) -> None:
    """Release an owned handle from :func:`resolve_service_db`."""
    if owned and db is not None:
        from hermes_state_registry import release_or_close

        try:
            release_or_close(db)
        except Exception:
            logger.debug("gateway_services: releasing SessionDB failed", exc_info=True)


def read_service_json(db, key: str) -> Optional[Any]:
    """Decoded JSON at ``state_meta[key]``, or None when absent/unparseable."""
    try:
        raw = db.get_meta(key)
    except Exception:
        logger.debug("gateway_services: get_meta failed for %r", key, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.debug("gateway_services: unparseable state_meta %r", key, exc_info=True)
        return None


def write_service_json(db, key: str, value: Any) -> None:
    """Encode *value* as JSON into ``state_meta[key]``."""
    db.set_meta(key, json.dumps(value, ensure_ascii=False))
