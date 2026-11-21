#!/usr/bin/env python3
"""F2 session registry — thin agent tools over the existing SessionDB.

``sessions_list`` projects ``SessionDB.list_sessions_rich``; ``sessions`` reads one
session row and applies the label/pin/archive/group mutations that live on
that row (``_set_session_title`` / ``set_session_pinned`` /
``set_session_archived`` / ``set_session_group``); ``agents_list`` reports the
current agent plus the live delegate-child registry (``list_active_subagents``).
``sessions`` also deletes one session (``action="delete"``, owner-only, with a
confirm step) through the store's guarded ``delete_session``.

Groups (the external agent's sidebar groups, ``src/gateway/session-groups.ts``):
membership is the ``group_name`` column on the row. A new session is filed
under its project automatically (repo root, else working directory); the
agent moves a session with a ``sessions`` patch ``group`` (empty string clears
it), and renames or deletes a whole group with the ``group_rename`` /
``group_delete`` actions. ``list_session_groups`` derives the catalog from
live rows, so a group exists exactly while a session is in it.
"""

import json
import logging
from typing import Any, Optional

# Read shape caps. list_sessions_rich is LIMIT/OFFSET-bounded in SQL; the
# caps below bound what the model can ask for in one call (the external agent's
# sessions_list caps at 200 rows/page; Hermes rows carry heavier previews).
_LIST_DEFAULT_LIMIT = 20
_LIST_MAX_LIMIT = 100
# Batch patch cap, mirroring external-agent sessions-tool (targets <= 100).
_PATCH_MAX_TARGETS = 100
# A subagent goal is free-form task text; cap what the listing carries.
_AGENT_GOAL_CHARS = 200


def _quiet(fn, default, msg, *log_args):
    """``fn()``, or *default* after debug-logging *msg*."""
    try:
        return fn()
    except Exception as e:  # health: allow BLE001 -- best-effort fallback helper; any failure degrades to the default
        logging.debug(msg, *(log_args + (e,)), exc_info=True)
        return default


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _resolve_db(db, *, write: bool, methods=None):
    """Usable SessionDB for this call: the passed handle when it carries the
    needed surface, else our own handle on the current home's state.db.

    A managed worker's handle is a RuntimeSessionStore: it mirrors the owner's
    write/lineage surface, not the SessionDB read/mutation API, so reads open
    their own read-only handle (the session_search pattern) and writes fail
    closed — a write must never silently land in a different home's DB. With
    no handle at all (registry dispatch outside a turn), the current home's
    own DB is the right one, writable for patch.
    Returns ``(db, owned, error)``; *owned* handles are released by the caller.
    """
    from agent.runtime_session_store import RuntimeSessionStore

    if db is None:
        pass  # open our own handle below
    elif isinstance(db, RuntimeSessionStore):
        if write:
            return None, False, ("sessions patch needs the live session store, "
                                 "which is not available in this context")
        db = None  # reads fall through to our own read-only handle
    else:
        needed = methods or (
            ("set_session_pinned", "set_session_archived", "set_session_hidden",
             "set_session_group", "rename_session_group", "delete_session_group",
             "_set_session_title") if write else ("list_sessions_rich", "get_session"))
        if all(callable(getattr(db, name, None)) for name in needed):
            return db, False, None
        if write:
            return None, False, ("sessions patch needs the live session store, "
                                 "which is not available in this context")
        db = None  # reads fall through to our own read-only handle
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        owned = SessionDB(db_path=get_hermes_home() / "state.db", read_only=not write)
        return owned, True, None
    except Exception:  # health: allow BLE001 -- any open failure means the DB is unavailable; the caller reports it
        logging.debug("sessions_tools: cannot open SessionDB", exc_info=True)
        from hermes_state import format_session_db_unavailable

        return None, False, format_session_db_unavailable()


def _release(owned, db) -> None:
    if owned and db is not None:
        from hermes_state_registry import release_or_close

        _quiet(lambda: release_or_close(db), None, "Failed to close sessions_tools SessionDB")


def _resolve_profile_db(profile):
    """Another profile's state.db, read-only; None = current profile."""
    from tools.session_search_tool import _resolve_profile_db

    return _resolve_profile_db(profile)


def _session_link(session_id: str, profile: Optional[str] = None) -> str:
    from tools.session_search_tool import _session_link

    return _session_link(session_id, profile)


def _project_row(row: dict[str, Any], profile: Optional[str] = None) -> dict[str, Any]:
    """Whitelist projection of a rich session row: identity, label, state, preview."""
    session_id = row.get("id", "")
    return {
        "session_id": session_id,
        "link": _session_link(session_id, profile),
        "title": row.get("title"),
        "group": row.get("group_name"),
        "source": row.get("source"),
        "model": row.get("model"),
        "started_at": row.get("started_at"),
        "last_active": row.get("last_active"),
        "message_count": row.get("message_count"),
        "pinned": bool(row.get("pinned")),
        "archived": bool(row.get("archived")),
        "hidden": bool(row.get("hidden")),
        "parent_session_id": row.get("parent_session_id"),
        "profile_name": row.get("profile_name"),
        "preview": row.get("preview"),
    }


def check_sessions_requirements() -> bool:
    """Requires the SQLite state database."""
    try:
        from hermes_state import _default_db_path

        return _default_db_path().parent.exists()
    except ImportError:
        return False


# =========================================================================
# sessions_list
# =========================================================================

# Scan window for the pinned-only view: list_sessions_rich has no pinned
# filter, so pins are collected by scanning bounded pages. The cap bounds
# the work; a store with more pinned rows than that is still listed, just
# with has_more left true.
_PINNED_SCAN_WINDOW = 100
_PINNED_SCAN_MAX_PAGES = 50


def _pinned_page(db, *, limit: int, offset: int, profile=None, **rich_kwargs):
    """(projected pinned rows, has_more) with *offset* counting pinned rows.

    Scans the store in bounded windows, skipping the first *offset* pins and
    collecting the next *limit*; stops early once a further pin is seen past
    the page, which is what makes has_more exact.
    """
    rich_kwargs["include_pinned"] = False
    # Mirror the back-fill: a pinned row stamped archived stays visible in
    # the active view (a pin is an explicit keep); only the archived-only
    # view narrows to archived rows.
    if not rich_kwargs.get("archived_only"):
        rich_kwargs["include_archived"] = True
    collected: list[dict[str, Any]] = []
    skipped = 0
    store_offset = 0
    has_more = False
    for _ in range(_PINNED_SCAN_MAX_PAGES):
        rows = db.list_sessions_rich(limit=_PINNED_SCAN_WINDOW, offset=store_offset,
                                     **rich_kwargs)
        if not rows:
            break
        store_offset += len(rows)
        for row in rows:
            if not row.get("pinned"):
                continue
            if skipped < offset:
                skipped += 1
                continue
            if len(collected) < limit:
                collected.append(_project_row(row, profile))
            else:
                has_more = True
                break
        if has_more or len(rows) < _PINNED_SCAN_WINDOW:
            break
    else:
        has_more = True  # scan cap hit: more pins may remain
    return collected, has_more


def sessions_list(limit: int = _LIST_DEFAULT_LIMIT, offset: int = 0, pinned_only: bool = False,
                  archived: str = "active", search: Optional[str] = None,
                  source: Optional[str] = None, include_children: bool = False,
                  profile: Optional[str] = None, db=None, group: Optional[str] = None,
                  current_session_id: Optional[str] = None) -> str:
    """List sessions from the registry (``list_sessions_rich``), newest first."""
    try:
        limit = max(1, min(_LIST_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    archived = (archived or "active").strip().lower()
    if archived not in ("active", "archived", "all"):
        return tool_error("archived must be 'active', 'archived', or 'all'", success=False)

    owned_db = None
    to_release = None
    try:
        if profile is not None and str(profile).strip():
            try:
                owned_db = _resolve_profile_db(profile)
            except Exception as e:  # health: allow BLE001 -- a bad profile name is a tool error, not a crash
                return tool_error(f"profile '{profile}': {e}", success=False)
            db = owned_db
            to_release = owned_db
        db, owned, error = _resolve_db(db, write=False)
        if error is not None:
            return tool_error(error, success=False)
        if owned:
            to_release = db
        rich_kwargs = dict(
            # The pinned back-fill obeys the other filters EXCEPT archived, so
            # it must stay off the archived-only view or pins leak into it.
            include_pinned=archived != "archived",
            include_archived=archived == "all", archived_only=archived == "archived",
            search_query=(search or None), source=(source or None),
            include_children=bool(include_children),
            # search_query is only honoured on the chain-ordered path.
            order_by_last_active=bool(search),
            group=(group or None),
        )
        if pinned_only:
            sessions, has_more = _pinned_page(db, limit=limit, offset=offset,
                                              profile=profile, **rich_kwargs)
        else:
            rows = db.list_sessions_rich(limit=limit, offset=offset, **rich_kwargs)
            sessions = [_project_row(r, profile) for r in rows]
            # Exact has_more: the back-fill appends pins past LIMIT, so the
            # page itself cannot prove a next page — probe one row past it,
            # without the back-fill.
            probe_kwargs = {k: v for k, v in rich_kwargs.items() if k != "include_pinned"}
            probe = db.list_sessions_rich(limit=1, offset=offset + limit,
                                          include_pinned=False, **probe_kwargs)
            has_more = len(probe) > 0
        return _ok(sessions=sessions, count=len(sessions), limit_applied=limit,
                   offset=offset, has_more=has_more,
                   next_offset=offset + limit if has_more else None,
                   current_session_id=current_session_id)
    except Exception as e:  # health: allow BLE001 -- tool boundary: DB failures surface as tool errors, never a crash
        logging.error("sessions_list failed", exc_info=True)

        return tool_error(f"sessions_list: {e}", success=False)
    finally:
        if to_release is not None:
            _release(True, to_release)


# =========================================================================
# sessions — get / patch one session (or a batch of them)
# =========================================================================

def _patch_one(db, session_id: str, *, label, pinned, archived, hidden, group) -> dict[str, Any]:
    """Apply the requested mutations to one row; returns its receipt."""
    changed: list[str] = []
    if label is not None:
        if not isinstance(label, str):
            return {"session_id": session_id, "success": False,
                    "error": "label must be a string (empty string clears it)"}
        try:
            landed = db._set_session_title(session_id, label, source=db.TITLE_SOURCE_USER)
        except ValueError as e:
            return {"session_id": session_id, "success": False, "error": str(e)}
        if not landed:
            return {"session_id": session_id, "success": False, "error": "session not found"}
        changed.append("label")
    if pinned is not None:
        if not db.set_session_pinned(session_id, bool(pinned)):
            return {"session_id": session_id, "success": False, "error": "session not found"}
        changed.append("pinned")
    if archived is not None:
        if not db.set_session_archived(session_id, bool(archived)):
            return {"session_id": session_id, "success": False, "error": "session not found"}
        changed.append("archived")
    if hidden is not None:
        if not db.set_session_hidden(session_id, bool(hidden)):
            return {"session_id": session_id, "success": False, "error": "session not found"}
        changed.append("hidden")
    if group is not None:
        if not isinstance(group, str):
            return {"session_id": session_id, "success": False,
                    "error": "group must be a string (empty string clears it)"}
        if not db.set_session_group(session_id, group):
            return {"session_id": session_id, "success": False, "error": "session not found"}
        changed.append("group")
    row = db.get_session(session_id) or {}
    return {"session_id": session_id, "success": True, "changed": changed,
            "session": _project_row(row)}


def _delete_is_owner(parent_agent) -> bool:
    """Owner gate for the delete action. The shared ``requester_is_owner`` is
    exact for local/DM/tenant turns; in a Telegram group turn it trusts the
    adapter catalog (the tool is visible to admitted members), so there the
    turn's actor id must match the launch owner id exactly."""
    from gateway.session_context import get_session_env
    from tools.gateway_owner import owner_telegram_id, requester_is_owner

    actor_id = (get_session_env("HERMES_SESSION_ACTOR_ID", "") or "").strip()
    if actor_id:
        owner_id = owner_telegram_id()
        return bool(owner_id) and str(actor_id) == str(owner_id)
    return requester_is_owner(parent_agent)


def _sessions_delete(db, session_id: str, *, confirm: bool) -> str:
    """Owner-only delete of one session: confirm step, then the store's
    guarded delete (a live turn's lease refuses plainly instead of
    corrupting the running turn)."""
    row = db.get_session(session_id)
    if not row:
        return tool_error(f"session '{session_id}' not found", success=False)
    title = row.get("title") or session_id
    if confirm is not True:
        return tool_error(
            f"Delete session '{title}' ({session_id})? Pass confirm=true to delete it permanently.",
            success=False)
    try:
        from hermes_state_errors import SessionActiveWriteGuardError
        from hermes_state_runtime import RuntimeStoreError

        deleted = db.delete_session(session_id, exclude_active_write_guards=True)
    except SessionActiveWriteGuardError as exc:
        return tool_error(str(exc), success=False)
    except RuntimeStoreError as exc:
        # The ledger refuses to delete a session with accepted, unfinished
        # turns; say what to do (mirrors the CLI's `hermes sessions delete`).
        if exc.reason == "unknown_execution":
            return tool_error(
                f"Cannot delete session '{session_id}': a turn was lost across a gateway restart. "
                f"Acknowledge it first with: hermes sessions discard {session_id}",
                success=False)
        if exc.reason == "session_busy":
            return tool_error(
                f"Cannot delete session '{session_id}': it has a queued or running turn. "
                "Wait for it to finish (or stop it), then retry.",
                success=False)
        return tool_error(f"Cannot delete session '{session_id}': {exc.reason}", success=False)
    if not deleted:
        return tool_error(f"session '{session_id}' not found", success=False)
    return _ok(deleted=session_id, title=title)


def _sessions_group_action(db, action: str, name, to) -> str:
    """Group-catalog actions for :func:`sessions` (list/rename/delete)."""
    if action == "group_list":
        groups = db.list_session_groups()
        return _ok(groups=groups, count=len(groups))
    if action == "group_rename":
        if not isinstance(name, str) or not name.strip() or not isinstance(to, str) or not to.strip():
            return tool_error("group_rename needs name and to (non-empty strings)", success=False)
        try:
            updated = db.rename_session_group(name, to)
        except ValueError as e:
            return tool_error(str(e), success=False)
        return _ok(renamed_from=name.strip(), renamed_to=to.strip(), sessions_updated=updated)
    if not isinstance(name, str) or not name.strip():
        return tool_error("group_delete needs name (a non-empty string)", success=False)
    try:
        updated = db.delete_session_group(name)
    except ValueError as e:
        return tool_error(str(e), success=False)
    return _ok(deleted=name.strip(), sessions_ungrouped=updated)


def _sessions_patch(db, session_id, label, pinned, archived, hidden, group,
                    targets, current_session_id) -> str:
    """Patch path for :func:`sessions`: one session, or a batch via targets."""
    ids = list(targets) if targets is not None else [(session_id or current_session_id or "").strip()]
    if not ids[0]:
        return tool_error("patch needs session_id or targets (no current session in this context)",
                          success=False)
    receipts = [_patch_one(db, sid, label=label, pinned=pinned,
                           archived=archived, hidden=hidden, group=group) for sid in ids]
    if targets is None:
        receipt = receipts[0]
        if not receipt["success"]:
            return tool_error(receipt["error"], success=False)
        return _ok(**{k: v for k, v in receipt.items() if k != "session_id"},
                   session_id=receipt["session_id"])
    return _ok(results=receipts, succeeded=sum(1 for r in receipts if r["success"]),
               failed=sum(1 for r in receipts if not r["success"]))


def sessions(session_id: Optional[str] = None, action: str = "get",
             label: Optional[str] = None, pinned: Optional[bool] = None,
             archived: Optional[bool] = None, hidden: Optional[bool] = None,
             group: Optional[str] = None, name: Optional[str] = None,
             to: Optional[str] = None,
             targets: Optional[list[str]] = None, profile: Optional[str] = None,
             db=None, current_session_id: Optional[str] = None,
             confirm: bool = False, parent_agent=None) -> str:
    """Read one session row, patch its label/pin/archive/group state, delete
    one session (owner-only, confirm step), or maintain the group catalog
    (group_list / group_rename / group_delete)."""
    action = (action or "get").strip().lower()
    if action not in ("get", "patch", "group_list", "group_rename", "group_delete", "delete"):
        return tool_error("action must be 'get', 'patch', 'group_list', 'group_rename', 'group_delete', or 'delete'",
                          success=False)
    if action == "delete":
        if not _delete_is_owner(parent_agent):
            from tools.gateway_owner import owner_only_error

            return owner_only_error("sessions")
        if profile is not None and str(profile).strip():
            return tool_error("delete writes the current profile only; cross-profile access is read-only",
                              success=False)
        if targets is not None:
            return tool_error("delete takes one session_id; targets is patch-only", success=False)
        if not (session_id or "").strip():
            return tool_error("delete needs session_id (an explicit session id)", success=False)
    if action == "patch" and profile is not None and str(profile).strip():
        return tool_error("patch writes the current profile only; cross-profile access is read-only",
                          success=False)
    if action in ("group_rename", "group_delete") and profile is not None and str(profile).strip():
        return tool_error("group renames/deletes write the current profile only; "
                          "cross-profile access is read-only", success=False)
    if action == "patch" and label is None and pinned is None and archived is None and hidden is None and group is None:
        return tool_error("patch needs at least one of label, pinned, archived, hidden, group", success=False)
    if targets is not None and (not isinstance(targets, list) or not targets):
        return tool_error("targets must be a non-empty list of session ids", success=False)
    if targets is not None and len(targets) > _PATCH_MAX_TARGETS:
        return tool_error(f"targets is capped at {_PATCH_MAX_TARGETS} sessions", success=False)
    if targets is not None and session_id is not None:
        return tool_error("targets cannot be combined with session_id", success=False)

    owned_db = None
    try:
        if profile is not None and str(profile).strip():
            try:
                owned_db = _resolve_profile_db(profile)
            except Exception as e:  # health: allow BLE001 -- a bad profile name is a tool error, not a crash
                return tool_error(f"profile '{profile}': {e}", success=False)
            db = owned_db
        db, owned, error = _resolve_db(
            db, write=action in ("patch", "group_rename", "group_delete", "delete"),
            methods=("delete_session", "get_session") if action == "delete" else (
                ("list_session_groups",) if action == "group_list" else None))
        if error is not None:
            return tool_error(error, success=False)
        if owned:
            owned_db = db
        if action == "delete":
            return _sessions_delete(db, (session_id or "").strip(), confirm=bool(confirm))
        if action in ("group_list", "group_rename", "group_delete"):
            return _sessions_group_action(db, action, name, to)
        if action == "get":
            target = (session_id or current_session_id or "").strip()
            if not target:
                return tool_error("get needs session_id (no current session in this context)",
                                  success=False)
            row = db.get_session(target)
            if not row:
                return tool_error(f"session '{target}' not found", success=False)
            return _ok(session=_project_row(row, profile))
        return _sessions_patch(db, session_id, label, pinned, archived, hidden, group,
                               targets, current_session_id)
    except Exception as e:  # health: allow BLE001 -- tool boundary: DB failures surface as tool errors, never a crash
        logging.error("sessions failed", exc_info=True)
        return tool_error(f"sessions: {e}", success=False)
    finally:
        if owned_db is not None:
            _release(True, owned_db)


# =========================================================================
# agents_list — the current agent plus live delegate children
# =========================================================================

def agents_list(db=None, current_session_id: Optional[str] = None) -> str:
    """List agents: this session's agent plus live delegate subagents.

    Hermes has no configured-agent registry (delegate_task spawns by task, not
    by agent id), so this reports the live registry: the current agent and the
    running children in ``list_active_subagents``.
    """
    agents: list[dict[str, Any]] = []
    try:
        model = None
        started_at = None
        if current_session_id:
            db, owned, _error = _resolve_db(db, write=False)
            try:
                if db is not None:
                    row = _quiet(lambda: db.get_session(current_session_id), None,
                                 "agents_list: get_session failed for %s", current_session_id) or {}
                    model = row.get("model")
                    started_at = row.get("started_at")
            finally:
                if owned:
                    _release(True, db)
        agents.append({"id": current_session_id or "main", "name": "main", "kind": "main",
                       "model": model, "status": "active", "started_at": started_at})
    except Exception:  # health: allow BLE001 -- the main-agent entry is best-effort; a failed lookup still lists the agent
        logging.debug("agents_list: current-agent entry failed", exc_info=True)
        agents.append({"id": current_session_id or "main", "name": "main", "kind": "main",
                       "model": None, "status": "active", "started_at": None})
    try:
        from tools.delegate_tool_registry import list_active_subagents

        for child in list_active_subagents():
            goal = child.get("goal")
            agents.append({
                "id": child.get("subagent_id"),
                "name": (str(goal)[:_AGENT_GOAL_CHARS] if goal else None),
                "kind": "subagent",
                "model": child.get("model"),
                "status": child.get("status"),
                "depth": child.get("depth"),
                "parent_id": child.get("parent_id"),
                "started_at": child.get("started_at"),
            })
    except Exception:  # health: allow BLE001 -- live children are optional; a registry failure lists just the current agent
        logging.debug("agents_list: list_active_subagents failed", exc_info=True)
    return _ok(agents=agents, count=len(agents))


# =========================================================================
# Schemas + registration
# =========================================================================

def _sessions_db_check() -> bool:
    return check_sessions_requirements()


SESSIONS_LIST_SCHEMA = {
    "name": "sessions_list",
    "description": (
        "List Hermes sessions from the session registry, newest first. Each row "
        "carries its label (title), group, source, model, last activity, message count, "
        "pin/archive state and a short preview, plus a link that renders as a "
        "titled link when you refer to the session. Use to find a session to "
        "resume, organise, or read — pass archived='archived' for the archived "
        "view, archived='all' to include archived rows, pinned_only=true for "
        "pins only, search to match a session id or title, group to list only "
        "one group ('ungrouped' for sessions with no group)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max sessions to return. Default 20, max 100.",
                "default": 20,
            },
            "offset": {
                "type": "integer",
                "description": "Rows to skip for the next page. Default 0.",
                "default": 0,
            },
            "pinned_only": {
                "type": "boolean",
                "description": "Only sessions pinned with sessions(pinned=true). Default false.",
                "default": False,
            },
            "archived": {
                "type": "string",
                "enum": ["active", "archived", "all"],
                "description": "'active' (default) hides archived rows, 'archived' lists only them, 'all' includes both.",
                "default": "active",
            },
            "search": {
                "type": "string",
                "description": "Match against a session id or title.",
            },
            "group": {
                "type": "string",
                "description": "Only sessions in this group; 'ungrouped' lists sessions with no group.",
            },
            "source": {
                "type": "string",
                "description": "Only sessions from this source (e.g. cli, telegram).",
            },
            "include_children": {
                "type": "boolean",
                "description": "Include compression/branch children as their own rows. Default false.",
                "default": False,
            },
            "profile": {
                "type": "string",
                "description": "Read another Hermes profile's sessions (read-only). Omit for the current profile.",
            },
        },
        "required": [],
    },
}

SESSIONS_SCHEMA = {
    "name": "sessions",
    "description": (
        "Read, organise, delete, or group Hermes sessions. get (default) returns the session's label, "
        "group, source, model, pin/archive state and preview. patch sets label (the session's "
        "title; empty string clears it), pinned, archived (false restores), hidden, or group "
        "(empty string clears it, leaving the session ungrouped), on one session (session_id, "
        "or the current session when omitted) or on up to 100 via targets. delete permanently "
        "deletes one session (owner-only; needs an explicit session_id and confirm=true — "
        "without confirm it only reports what would be deleted; a session with a live turn "
        "refuses plainly). group_list lists "
        "every group with its session count; group_rename moves every session in a group to a "
        "new name; group_delete ungroups every session in a group. New sessions are filed "
        "under their project automatically; patch group to move one to a different group. "
        "Pinning keeps a session visible; archiving hides it from the default listing but "
        "keeps its messages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session to read or patch. Omit to target the current session. Cannot be combined with targets.",
            },
            "action": {
                "type": "string",
                "enum": ["get", "patch", "group_list", "group_rename", "group_delete", "delete"],
                "description": "'get' reads the row; 'patch' applies the label/pinned/archived/hidden/group changes; 'group_list' lists every group; 'group_rename' moves a whole group to a new name; 'group_delete' ungroups a whole group; 'delete' permanently deletes one session (owner-only, needs confirm=true).",
                "default": "get",
            },
            "confirm": {
                "type": "boolean",
                "description": "Delete only. Pass true to confirm deleting the session. A delete without confirm only reports what would be deleted and deletes nothing.",
                "default": False,
            },
            "label": {
                "type": "string",
                "description": "Patch only. New title for the session (max 100 chars); empty string clears it.",
            },
            "pinned": {
                "type": "boolean",
                "description": "Patch only. True pins the session, false unpins it.",
            },
            "archived": {
                "type": "boolean",
                "description": "Patch only. True archives the session, false restores it.",
            },
            "hidden": {
                "type": "boolean",
                "description": "Patch only. True hides the session from the default listing, false unhides it.",
            },
            "group": {
                "type": "string",
                "description": "Patch only. The session's group; empty string clears it (ungrouped). New sessions are filed under their project automatically — patch only when the topic belongs elsewhere.",
            },
            "name": {
                "type": "string",
                "description": "group_rename / group_delete only. The group to rename or delete.",
            },
            "to": {
                "type": "string",
                "description": "group_rename only. The new name for the group.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Patch only. Up to 100 session ids to patch identically; cannot be combined with session_id.",
            },
            "profile": {
                "type": "string",
                "description": "Get only. Read a session from another Hermes profile (read-only); patch writes the current profile only.",
            },
        },
        "required": [],
    },
}

AGENTS_LIST_SCHEMA = {
    "name": "agents_list",
    "description": (
        "List the agents in this run: the current agent and its live subagents. "
        "Hermes spawns subagents by task rather than by a configured agent id, "
        "so this reports the live registry — each entry carries its goal, model, "
        "status and depth, and agents that already finished are not listed."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


from tools.registry import registry, tool_error

registry.register(
    name="sessions_list",
    toolset="sessions",
    schema=SESSIONS_LIST_SCHEMA,
    handler=lambda args, **kw: sessions_list(
        limit=args.get("limit", _LIST_DEFAULT_LIMIT), offset=args.get("offset", 0),
        pinned_only=args.get("pinned_only", False), archived=args.get("archived", "active"),
        search=args.get("search"), source=args.get("source"), group=args.get("group"),
        include_children=args.get("include_children", False), profile=args.get("profile"),
        db=kw.get("db"), current_session_id=kw.get("current_session_id")),
    check_fn=_sessions_db_check,
    emoji="🗂️")

registry.register(
    name="sessions",
    toolset="sessions",
    schema=SESSIONS_SCHEMA,
    handler=lambda args, **kw: sessions(
        session_id=args.get("session_id"), action=args.get("action", "get"),
        label=args.get("label"), pinned=args.get("pinned"), archived=args.get("archived"),
        hidden=args.get("hidden"), group=args.get("group"),
        name=args.get("name"), to=args.get("to"),
        targets=args.get("targets"), profile=args.get("profile"),
        db=kw.get("db"), current_session_id=kw.get("current_session_id"),
        confirm=args.get("confirm", False), parent_agent=kw.get("parent_agent")),
    check_fn=_sessions_db_check,
    emoji="✏️")

registry.register(
    name="agents_list",
    toolset="sessions",
    schema=AGENTS_LIST_SCHEMA,
    handler=lambda args, **kw: agents_list(
        db=kw.get("db"), current_session_id=kw.get("current_session_id")),
    check_fn=_sessions_db_check,
    emoji="🤖")
