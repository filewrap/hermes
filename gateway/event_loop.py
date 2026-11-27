"""Gateway-only event-loop selection; never change the process-wide policy."""

import asyncio
import logging
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _new_gateway_loop() -> asyncio.AbstractEventLoop:
    """Prefer uvloop, retaining the configured selector policy as a fallback."""
    loop = None
    if sys.platform not in {"win32", "cygwin"}:
        try:
            import uvloop

            loop = uvloop.new_event_loop()
        except (ImportError, OSError, RuntimeError, NotImplementedError) as exc:
            # Missing wheels/native support must not prevent gateway startup.
            # Only loop construction is guarded: never retry a running gateway.
            logger.info("uvloop unavailable; using asyncio policy fallback (%s)", type(exc).__name__)
    if loop is None:
        # Honor an existing custom selector policy (including SelectSelector),
        # rather than hard-coding a fresh DefaultEventLoopPolicy.
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def run_gateway_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run one gateway with Python 3.11+ Runner's normal teardown semantics."""
    try:
        with asyncio.Runner(loop_factory=_new_gateway_loop) as runner:
            return runner.run(coro)
    finally:
        # Runner does not clear the current loop when given a custom factory.
        # Also close an unstarted coroutine if native loop construction failed.
        asyncio.set_event_loop(None)
        coro.close()
