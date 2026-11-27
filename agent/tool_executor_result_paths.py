"""Persisted tool-result path recording for result-reference stubs.

Split out of ``agent/tool_executor.py`` (file-size ratchet): the stub path a
tool result spilled to must be recorded so a later result-reference stub can't
dangle.
"""
from __future__ import annotations

import logging
from typing import Any

from agent.tool_dispatch_helpers import _is_multimodal_tool_result
from tools.tool_result_storage import extract_persisted_path

logger = logging.getLogger(__name__)


def _record_persisted_path_for_stub(agent, tool_call_id: str, function_result) -> None:
    """Record the spillover file path so a later result-reference stub can't dangle (best-effort)."""
    try:
        candidates = [function_result] if isinstance(function_result, str) else [
            function_result.get("text_summary"),
            *(p.get("text") for p in function_result.get("content") or [] if isinstance(p, dict)),
        ] if _is_multimodal_tool_result(function_result) else []
        path = next((p for p in map(extract_persisted_path, candidates) if p), None)
        if path:
            agent._tool_guardrails.record_persisted_result(tool_call_id, path)
    except Exception as exc:
        logger.debug("persisted-path record for result stub failed: %s", exc)
