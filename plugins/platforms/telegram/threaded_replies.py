"""Threaded replies (beta): chain a turn's response messages to each other.

When ``platforms.telegram.threaded_replies`` is on for the active profile,
the second response message in a turn replies to the first response message
(instead of all of them quoting the user's message), the third replies to the
second, and so on. The chain is keyed by (chat, user-message anchor) and
lives only on the adapter for the turn's lifetime (600s TTL, 128 entries);
a failed send, or a send that returns no message id, drops the entry so the
next response falls back to the user's message. One-message turns behave
exactly as before. Only sends marked with ``telegram_threaded_reply``
(response text/rich) chain; working/status messages, approval cards and
swarm cards carry no marker and keep their current reply behaviour.
"""
from __future__ import annotations

import time
from typing import Any

_CONFIG_KEY = "threaded_replies"
_RUNTIME_ATTR = "_threaded_replies_runtime"
FLAG = "telegram_threaded_reply"
_CHAIN_TTL_S = 600.0
_CHAIN_MAX_ENTRIES = 128


def threaded_replies_enabled(adapter: Any) -> bool:
    """Profile-wide ``platforms.telegram.threaded_replies``; False when unset.

    The live runtime attr (set by the /settings toggle) wins over the
    persisted config value, exactly like the streaming-response reader.
    """
    runtime = getattr(adapter, _RUNTIME_ATTR, None)
    if runtime is not None:
        return bool(runtime)
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return bool(extra.get(_CONFIG_KEY, False))
    return False


def _chain_store(adapter: Any) -> dict:
    store = getattr(adapter, "_threaded_reply_chains", None)
    if not isinstance(store, dict):
        store = {}
        adapter._threaded_reply_chains = store
    return store


def _prune_chain_store(store: dict) -> None:
    now = time.monotonic()
    for key in [key for key, (_, stamp) in store.items() if now - stamp > _CHAIN_TTL_S]:
        del store[key]
    while len(store) > _CHAIN_MAX_ENTRIES:
        del store[min(store, key=lambda key: store[key][1])]


def resolve_threaded_reply(adapter: Any, chat_id: Any, anchor: Any) -> int | None:
    """Last response message id sent for (chat, anchor), or None when cold."""
    store = _chain_store(adapter)
    entry = store.get((str(chat_id), str(anchor)))
    if entry is None:
        return None
    message_id, stamp = entry
    if time.monotonic() - stamp > _CHAIN_TTL_S:
        del store[(str(chat_id), str(anchor))]
        return None
    try:
        return int(message_id)
    except (TypeError, ValueError):
        return None


def record_threaded_reply(adapter: Any, chat_id: Any, anchor: Any, message_id: Any) -> None:
    """Remember a response message id as the next reply target for (chat, anchor)."""
    store = _chain_store(adapter)
    store[(str(chat_id), str(anchor))] = (str(message_id), time.monotonic())
    _prune_chain_store(store)


def reset_threaded_reply(adapter: Any, chat_id: Any, anchor: Any) -> None:
    """Drop the chain for (chat, anchor); the next response quotes the user message."""
    _chain_store(adapter).pop((str(chat_id), str(anchor)), None)


def threaded_reply_target(adapter: Any, chat_id: Any, reply_id: Any, metadata: Any,
                          task_chain: bool) -> tuple[bool, Any, Any]:
    """Resolve a send's reply target for the threaded-replies beta.

    Returns ``(threaded, anchor, effective_reply)``: ``threaded`` is True
    only for a marked response send with the beta on and no task-chain
    override; ``anchor`` is the user's message id the chain is keyed by
    (None when not threaded); ``effective_reply`` is the last response
    message id, or the anchor when the chain is cold.
    """
    threaded = (bool((metadata or {}).get(FLAG)) and not task_chain
                and threaded_replies_enabled(adapter))
    anchor = reply_id if threaded else None
    effective = (resolve_threaded_reply(adapter, chat_id, anchor)
                 if anchor is not None else None) or reply_id
    return threaded, anchor, effective


async def threaded_verify_reply(adapter: Any, peer: Any, chat_id: Any, threaded: bool,
                                anchor: Any, effective_reply: Any, thread_id: Any,
                                metadata: Any) -> Any:
    """Verify a (possibly chained) reply target inside a topic.

    Verifies the chained id first; when it is stale (deleted / wrong
    topic) the chain is dropped and the user's message is verified instead,
    so a stale chain never refuses the response. Unthreaded sends verify
    exactly as before.
    """
    from .topic_delivery import verified_topic_reply

    reply_id = await verified_topic_reply(adapter, peer, effective_reply, thread_id, metadata)
    if reply_id is None and threaded and anchor is not None:
        reset_threaded_reply(adapter, chat_id, anchor)
        reply_id = await verified_topic_reply(adapter, peer, anchor, thread_id, metadata)
    return reply_id


def note_threaded_reply(adapter: Any, chat_id: Any, threaded: bool, anchor: Any,
                        message_id: Any) -> None:
    """Advance the chain to a sent response id, or drop it when there is none.

    A send that fails, or returns no message id, reports ``message_id=None``
    (the adapter calls this from its failure path too), so the next response
    falls back to the user's message.
    """
    if not threaded or anchor is None:
        return
    if message_id:
        record_threaded_reply(adapter, chat_id, anchor, message_id)
    else:
        reset_threaded_reply(adapter, chat_id, anchor)
