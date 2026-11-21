"""Purge: owner-only deletion of the bot's own observed Telegram messages.

``purge`` replaces the old ``messages_delete`` action. It deletes by explicit
message IDs (the old behaviour) or by a broader form — newest N, a time
window, or all — resolved ONLY from the in-memory observed bucket for the
current chat/topic. Nothing here fetches history and nothing records message
IDs anywhere: messages never observed in this process cannot be purged, and
the result says so plainly.

Kept in its own sibling so ``client_operations.py`` stays under its line cap;
the class calls in through thin delegation. Every function takes the
operations instance explicitly (``ops``) instead of importing the class.
"""
from __future__ import annotations

import datetime
from typing import Any

from telethon import functions, types

_PURGE_DELETE_CHUNK = 100


def is_owner(ops: Any, source: Any) -> bool:
    """Owner test for owner-only actions (purge). Fails closed when the adapter cannot answer."""
    check = getattr(ops.adapter, "_is_owner_user", None)
    if not callable(check):
        return False
    try:
        return bool(check(ops._source_value(source, "user_id", ""), source))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return False


def is_own_observed(message: Any) -> bool:
    # Telethon messages carry .out; the Observed* shims are built only for
    # this bot's own sends (rich_message_send / send results) and carry none.
    return bool(getattr(message, "out", True))


def observed_date(message: Any) -> datetime.datetime | None:
    date = getattr(message, "date", None)
    if not isinstance(date, datetime.datetime):
        return None
    if date.tzinfo is None:
        return date.replace(tzinfo=datetime.UTC)
    return date


def purge_note(form: str, purged: int, info: dict[str, Any], max_observed: int) -> str:
    if purged == 0:
        base = "Nothing purged: no messages of yours were observed in this chat/topic"
        if form == "window":
            base += f" within the last {info.get('window_minutes', 0)} minutes"
        base += "."
    else:
        base = f"Purged {purged} of your message{'s' if purged != 1 else ''} in this chat/topic."
    if form != "ids":
        base += (f" Only messages observed here in this process can be purged "
                 f"({info.get('observed_own_total', 0)} of your messages observed, "
                 f"at most {max_observed} per chat/topic); older or "
                 f"never-observed messages cannot be purged this way.")
    without_time = info.get("own_without_time", 0)
    if form == "window" and without_time:
        base += (f" {without_time} of your messages have no recorded time and "
                 f"could not be matched to the window.")
    return base


def resolve_purge_ids(ops: Any, source: Any, clean: dict[str, Any]) -> tuple[list[int], dict[str, Any]]:
    """Resolve one purge form to message IDs from this chat/topic's observed bucket.

    Broad forms (last_n / window / all) cover the bot's own observed
    messages only; explicit IDs keep the old delete-by-IDs behaviour.
    """
    form = clean["form"]
    own = [m for m in ops._observed_messages(source) if is_own_observed(m)]
    info: dict[str, Any] = {"observed_own_total": len(own)}
    if form == "ids":
        return list(clean["message_ids"]), info
    if form == "last_n":
        ids = [int(getattr(m, "id", 0) or 0) for m in own[-clean["last_n"]:]]
        return [mid for mid in ids if mid > 0], info
    if form == "all":
        ids = [int(getattr(m, "id", 0) or 0) for m in own]
        return [mid for mid in ids if mid > 0], info
    minutes = clean["minutes"]
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes)
    info["window_minutes"] = minutes
    without_time = 0
    ids = []
    for message in own:
        date = observed_date(message)
        if date is None:
            without_time += 1
            continue
        mid = int(getattr(message, "id", 0) or 0)
        if mid > 0 and date >= cutoff:
            ids.append(mid)
    info["own_without_time"] = without_time
    return ids, info


def validate_purge_payload(int_fn: Any, params: dict[str, Any], max_observed: int) -> dict[str, Any]:
    """Validate one purge form. ``int_fn`` is the operations ``_int`` validator."""
    p = dict(params)
    if set(p) - {"message_ids", "last_n", "minutes", "hours", "all"}:
        raise ValueError("purge accepts only message_ids, last_n, minutes, hours, and all")
    forms = [key for key in ("message_ids", "last_n", "minutes", "hours", "all")
             if p.get(key) not in (None, False)]
    if len(forms) != 1:
        raise ValueError("purge needs exactly one of message_ids, last_n, minutes, hours, or all")
    form = forms[0]
    if form == "message_ids":
        ids = p.get("message_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("message_ids must contain 1–100 message IDs")
        cleaned = [int_fn(x, "message_id") for x in ids]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("message_ids must be unique")
        return {"form": "ids", "message_ids": cleaned}
    if form == "last_n":
        return {"form": "last_n",
                "last_n": int_fn(p["last_n"], "last_n", minimum=1, maximum=max_observed)}
    if form == "minutes":
        return {"form": "window",
                "minutes": int_fn(p["minutes"], "minutes", minimum=1, maximum=43200)}
    if form == "hours":
        return {"form": "window",
                "minutes": int_fn(p["hours"], "hours", minimum=1, maximum=720) * 60}
    return {"form": "all"}


def prepare_purge_request_params(
    ops: Any, source: Any, clean: dict[str, Any], max_observed: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Owner-gate + resolve a validated purge form to executable params.

    Returns ``(resolved_params, None)`` on success, or ``(None, early_result)``
    for the owner refusal and the empty (nothing-to-purge) no-op.
    """
    if not is_owner(ops, source):
        return None, ops._error("FORBIDDEN", "Purge is owner-only.")
    ids, info = resolve_purge_ids(ops, source, clean)
    if not ids:
        return None, {"ok": True, "status": "complete", "action": "purge",
                      "data": {"deleted_message_ids": [], "purged": 0,
                               "form": clean["form"], **info,
                               "note": purge_note(clean["form"], 0, info, max_observed)}}
    resolved = {"form": clean["form"], "message_ids": ids, "_purge_info": info}
    if clean["form"] == "ids" and not ops._all_messages_observed(source, ids):
        return None, ops._error(
            "MESSAGE_SCOPE_INVALID",
            "Every message ID must have been observed in this exact chat/thread before deletion.")
    return resolved, None


def capabilities_listing(ops: Any, source: Any) -> tuple[list[str], dict[str, Any]]:
    """Mutations + parameters visible to this actor (purge is owner-only)."""
    from .client_operations import CLIENT_ACTION_PARAMETERS, MUTATION_ACTIONS

    parameters = dict(CLIENT_ACTION_PARAMETERS)
    mutations = sorted(MUTATION_ACTIONS)
    if not is_owner(ops, source):
        mutations = [name for name in mutations if name != "purge"]
        parameters.pop("purge", None)
    return mutations, parameters


async def execute_purge(ops: Any, peer: Any, params: dict[str, Any], max_observed: int) -> dict[str, Any]:
    """Delete the resolved IDs in 100-message batches; same RPC as the old delete."""
    ids = params["message_ids"]
    info = params.get("_purge_info") or {}
    form = params.get("form") or "ids"
    deleted: list[int] = []
    for start in range(0, len(ids), _PURGE_DELETE_CHUNK):
        chunk = ids[start:start + _PURGE_DELETE_CHUNK]
        if isinstance(peer, types.InputPeerChannel):
            channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
            await ops.adapter._mtproto_rich_request(
                functions.channels.DeleteMessagesRequest(channel=channel, id=chunk))
        else:
            await ops.adapter._mtproto_rich_request(
                functions.messages.DeleteMessagesRequest(id=chunk, revoke=True))
        deleted.extend(chunk)
    return {"deleted_message_ids": deleted, "purged": len(deleted),
            "form": form, **info, "note": purge_note(form, len(deleted), info, max_observed)}
