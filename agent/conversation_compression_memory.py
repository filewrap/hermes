"""Memory half of ``agent.conversation_compression``.

The pre-compress memory context: provider ``on_pre_compress()`` insights and the
direct-message evidence filter they checkpoint from. ``conversation_compression``
imports every name (import-for-use at its call site), as it does for
``conversation_compression_checkpoint``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from agent.context_engine import sanitize_memory_context
from agent.conversation_compression_checkpoint import _checkpoint_blocked, _checkpoint_incapable
from agent.memory_provider import PRE_COMPRESS_CHECKPOINT_API_VERSION

# Log-record parity with the origin module.
logger = logging.getLogger("agent.conversation_compression")


def _direct_messages_for_pre_compress_memory(messages: Any) -> list[dict[str, Any]]:
    """Return direct user/assistant evidence safe for memory checkpointing.
    Summaries, tool rows and system messages are omitted; assistant prose is kept with ``tool_calls``
    stripped, and pure tool-call wrappers are dropped."""
    # Deferred import: context_compressor → turn_context → this module would form an import cycle.
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
    direct_messages: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"} or message.get(COMPRESSED_SUMMARY_METADATA_KEY):
            continue
        if role == "assistant" and message.get("tool_calls"):
            content = message.get("content")
            has_prose = bool(content.strip() if isinstance(content, str) else content)
            if not has_prose:
                continue
            message = {k: v for k, v in message.items() if k != "tool_calls"}
        direct_messages.append(message)
    return direct_messages


def _pre_compress_memory_context(agent: Any, messages: list, checkpoint_required: bool) -> str:
    """Provider ``on_pre_compress()`` insights to surface in the summary ("" if none).
    Raw messages stay the API v1 provider contract; normalized evidence goes only to API v2+ checkpoint
    providers inside MemoryManager.on_pre_compress(). Raises :class:`CompressionCheckpointUnavailable` when a
    required checkpoint cannot be taken."""
    memory_context = ""
    memory_manager = getattr(agent, "_memory_manager", None)
    evidence_messages = _direct_messages_for_pre_compress_memory(messages)
    # A detached review/side-question fork (no memory manager, session DB or persistence) cannot checkpoint by design.
    if checkpoint_required and getattr(agent, "_persist_disabled", False) and memory_manager is None:
        checkpoint_required = getattr(agent, "_session_db", None) is not None
    if checkpoint_required:
        supports_checkpoint = getattr(memory_manager, "supports_pre_compress_checkpoint", None)
        if memory_manager is None or not callable(supports_checkpoint):
            raise _checkpoint_incapable(f"no active provider implements checkpoint API v{PRE_COMPRESS_CHECKPOINT_API_VERSION}")
        try:
            compatible = bool(supports_checkpoint(PRE_COMPRESS_CHECKPOINT_API_VERSION))
        except Exception as exc:
            raise _checkpoint_blocked("provider capability probe failed") from exc
        if not compatible:
            raise _checkpoint_incapable(f"active provider does not implement checkpoint API v{PRE_COMPRESS_CHECKPOINT_API_VERSION}")
        try:
            _maybe_ctx = memory_manager.on_pre_compress(
                messages, evidence_messages=evidence_messages, require_checkpoint=True,
                checkpoint_api_version=PRE_COMPRESS_CHECKPOINT_API_VERSION,
            )
        except Exception as exc:
            logger.warning("Required pre-compress checkpoint failed (%s)", type(exc).__name__)
            raise _checkpoint_blocked(f"provider checkpoint API v{PRE_COMPRESS_CHECKPOINT_API_VERSION} failed") from exc
        if isinstance(_maybe_ctx, str):
            memory_context = sanitize_memory_context(_maybe_ctx)
    elif memory_manager:
        with contextlib.suppress(Exception):
            _maybe_ctx = memory_manager.on_pre_compress(messages, evidence_messages=evidence_messages)
            if isinstance(_maybe_ctx, str):
                memory_context = sanitize_memory_context(_maybe_ctx)
    return memory_context
