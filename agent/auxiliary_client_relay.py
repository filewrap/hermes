"""Relay identity for one auxiliary LLM call (Relay logical-call scope).

Split out of ``agent.auxiliary_client`` (file-size ratchet); every physical attempt in one
auxiliary call funnels through ``_relay_sync_completion`` / ``_relay_async_completion`` /
``_relay_sync_stream``, which read this context. The names stay importable from the facade so
existing imports keep working.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger("agent.auxiliary_client")


_RELAY_AUX_CALL_CONTEXT: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("auxiliary_relay_call", default=None)
)


@contextlib.contextmanager
def _relay_aux_call_scope(args: tuple, kwargs: dict):
    """Bind a fresh relay call context for one auxiliary call; mark it failed on any exception."""
    task = args[0] if args else kwargs.get("task")
    token = _RELAY_AUX_CALL_CONTEXT.set({
        "task": str(task or "unknown"),
        "request_id": f"aux-{uuid.uuid4().hex}",
        "attempt_count": 0,
        "provider": "",
        "model": "",
        "response_model": None,
        "api_mode": "chat_completions",
    })
    try:
        yield
    except BaseException as exc:
        _fail_relay_auxiliary_call(exc)
        raise
    finally:
        _RELAY_AUX_CALL_CONTEXT.reset(token)


def _relay_auxiliary_call(callback):
    """Give every physical retry in one auxiliary call a shared Relay identity."""
    @functools.wraps(callback)
    def wrapped(*args, **kwargs):
        with _relay_aux_call_scope(args, kwargs):
            return callback(*args, **kwargs)
    return wrapped


def _relay_auxiliary_call_async(callback):
    """Async counterpart to :func:`_relay_auxiliary_call`."""
    @functools.wraps(callback)
    async def wrapped(*args, **kwargs):
        with _relay_aux_call_scope(args, kwargs):
            return await callback(*args, **kwargs)
    return wrapped


def _set_relay_auxiliary_route(provider: str | None, model: str | None, api_mode: str | None) -> None:
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    context["provider"] = str(provider or "auxiliary")
    context["model"] = str(model or "unknown")
    context["response_model"] = None
    context["api_mode"] = str(api_mode or "chat_completions")


def _record_route_info(
    route_info: Optional[dict[str, str]], provider: Optional[str], model: Optional[str]
) -> None:
    """Expose the concrete route selected for one auxiliary call."""
    if route_info is not None:
        route_info["provider"] = provider or "auto"
        route_info["model"] = model or "default"


def _relay_auxiliary_metadata(
    *, provider: str | None = None, api_mode: str | None = None
) -> tuple[str, str, dict[str, Any]] | None:
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return None
    attempt_count = int(context.get("attempt_count") or 0)
    context["attempt_count"] = attempt_count + 1
    provider_name = str(provider or context.get("provider") or "auxiliary")
    model_name = str(context.get("model") or "unknown")
    return provider_name, model_name, {
        "api_mode": str(api_mode or context.get("api_mode") or "chat_completions"),
        "api_request_id": str(context["request_id"]),
        "call_role": f"auxiliary:{context['task']}",
        "retry_count": attempt_count,
        "auxiliary_task": str(context["task"]),
    }


def _complete_relay_auxiliary_call(*, outcome: str = "success", error_class: Optional[str] = None) -> None:
    """Close one auxiliary logical call after acceptance or terminal failure. A success
    reports the last attempt error it recovered from (``none`` when the first attempt held)."""
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    from agent import relay_llm
    relay_llm.complete_logical_call(
        str(context.get("request_id") or ""), outcome=outcome,
        model_name=str(context.get("model") or "unknown"),
        provider_name=str(context.get("provider") or "auxiliary"),
        response_model_name=context.get("response_model"),
        error_class=error_class or context.get("error_class") or "none",
    )


def _note_relay_auxiliary_error(exc: BaseException) -> None:
    """Remember one failed attempt's classified reason on the logical call."""
    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is not None and isinstance(exc, Exception):
        from agent import auxiliary_call_outcome
        context["error_class"] = auxiliary_call_outcome.error_class(
            exc, provider=str(context.get("provider") or ""), model=str(context.get("model") or ""))


def _fail_relay_auxiliary_call(exc: BaseException) -> None:
    """Close a call that ended in ``exc`` without replacing it: a Hermes abort is ``cancelled``,
    anything else ``failed`` with the classifier's reason for the error that ended it."""
    try:
        from agent import auxiliary_call_outcome
        if auxiliary_call_outcome.is_cancellation(exc):
            _complete_relay_auxiliary_call(outcome="cancelled", error_class="none")
            return
        _note_relay_auxiliary_error(exc)
        _complete_relay_auxiliary_call(outcome="failed")
    except Exception:
        logger.warning("Relay auxiliary failure finalization failed", exc_info=True)
