"""Turn-finalization helpers moved out of ``gateway/run_turn_runner.py``.

``TurnRunner`` is over its file-size cap, so behaviour that runs after the
agent turn settles lives here as module functions taking the turn runner
(``turn_runner._ctx`` / ``turn_runner._runner``), called from
``_run_sync_scoped``. Moved verbatim; no behaviour change.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.config import Platform

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def sync_session_after_run(turn_runner: Any, agent_history):
    """Sync session_id right after run_conversation(): compression can rotate before a follow-up
    model call fails, and the failure return must still point at the compressed child.
    Returns (compacted_in_place, effective_session_id, effective_history_offset)."""
    ctx = turn_runner._ctx
    runner = turn_runner._runner
    agent = ctx.agent_holder[0]
    # In-place compaction compacts the transcript WITHOUT rotating the id, so the id-change diff
    # can't see it; compress_context() sets this flag and the gateway re-baselines as for a split.
    compacted_in_place = bool(getattr(agent, "_last_compaction_in_place", False)) if agent else False
    agent_session_id = getattr(agent, 'session_id', ctx.session_id) if agent else ctx.session_id
    session_was_split = bool(agent and ctx.session_key and agent_session_id != ctx.session_id)
    if session_was_split:
        logger.info("Session split detected: %s → %s (compression)", ctx.session_id, agent_session_id)
        entry = runner.session_store._entries.get(ctx.session_key)
        persisted = False
        if entry:
            entry_session_id = getattr(entry, "session_id", None)
            if not ctx._run_still_current():
                logger.info(
                    "Skipping session split sync for stale run %s — "
                    "generation %s is no longer current",
                    ctx.session_key or "?", ctx.run_generation,
                )
            elif entry_session_id == agent_session_id:
                persisted = True
            elif entry_session_id != ctx.session_id:
                logger.info(
                    "Skipping session split sync for %s because the "
                    "session binding moved from %s to %s before "
                    "compression finished",
                    ctx.session_key or "?", ctx.session_id, entry_session_id,
                )
            else:
                entry.session_id = agent_session_id
                runner.session_store._save()
                runner.session_store._record_gateway_session_peer(agent_session_id, ctx.session_key, ctx.source)
                persisted = True
        # Only after this run published its split — a stale /stop→/new predecessor must not
        # mutate routing state.
        if persisted:
            src = ctx.source
            if (
                getattr(src, "platform", None) == Platform.TELEGRAM and getattr(src, "chat_type", None) == "dm"
                and getattr(src, "thread_id", None) is None and runner._session_db is not None
            ):
                turn_runner._restore_telegram_thread_id_after_split(agent_session_id)
            runner._sync_telegram_topic_binding(src, entry, reason="agent-run-compression")
    from gateway.session_cli_turn import cli_turn
    if not cli_turn(ctx.inbound_message_id).get("model_override"):
        # A `/model --once` / `/moa` turn ran another route for this admission only: the row keeps
        # the session's model, or a /resume would come back on the one-turn model.
        runner._sync_session_model_from_agent(agent_session_id, agent)
    # history_offset=0 whenever the agent's message list lost the original history prefix
    # (split OR in-place compaction): the returned `messages` is the compacted set, persist all
    # of it; slicing past the pre-compaction length would drop everything.
    offset = 0 if (session_was_split or compacted_in_place) else len(agent_history)
    return compacted_in_place, agent_session_id, offset


def append_auto_media_tags(final_response: str, result, agent_history, history_media_paths) -> str:
    """Append MEDIA:<path> tags from tool results (e.g. TTS) that the model's final text omits, so
    extract_media() delivers each file once. Scoped to THIS turn (slice at len(agent_history)) so
    a stale MEDIA: path from an earlier turn never rides a later reply; the history-path dedup is
    the secondary guard — and the sole one when mid-run compression shrank the list."""
    from gateway.run import _collect_auto_append_media_tags
    if "MEDIA:" in final_response:
        return final_response
    # Scan tool results for MEDIA:<path> tags that need to be delivered as native audio/file
    # attachments. The TTS tool embeds MEDIA: tags in its JSON response, but the model's final text
    # reply usually doesn't include them. We collect unique tags from tool results and append any that
    # aren't already present in the final response, so the adapter's extract_media() can find and
    # deliver the files exactly once. Scope the scan to THIS turn's tool results only. ``agent_history``
    # was passed into run_conversation as ``conversation_history``, so the agent's returned ``messages``
    # list is ``agent_history`` followed by the messages produced this turn. Slicing at
    # ``len(agent_history)`` isolates the current turn precisely, so a stale MEDIA: path emitted by a
    # tool several turns earlier (still present in the full message list) can never leak onto a later
    # text-only reply. (Fixes #34608) Path-based deduplication against _history_media_paths (collected
    # before run_conversation) is retained as a secondary guard. It is also the sole guard on the
    # fallback branch taken when mid-run context compression shrinks the message list below the original
    # history length, preserving the compression-safe behaviour of #160.
    media_tags, has_voice_directive = _collect_auto_append_media_tags(
        result.get("messages", []), history_offset=len(agent_history), history_media_paths=history_media_paths,
    )
    if not media_tags:
        return final_response
    unique_tags = (["[[audio_as_voice]]"] if has_voice_directive else []) + list(dict.fromkeys(media_tags))
    return final_response + "\n" + "\n".join(unique_tags)
