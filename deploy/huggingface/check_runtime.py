#!/usr/bin/env python3
"""Validate the HF application interpreter without opening live state."""
from __future__ import annotations

import asyncio
import platform
import sqlite3
import sys

MIN_SQLITE = (3, 51, 3)
UVLOOP_VERSION = "0.22.1"


def check_sqlite() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"HF startup blocked: {sys.executable} loads SQLite {sqlite3.sqlite_version}; "
            "SQLite >=3.51.3 is required. Rebuild with the fixed system Python; "
            "a system-python check alone does not validate the application venv."
        )
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')")
        db.execute("INSERT INTO docs VALUES ('hermes')")
        if db.execute("SELECT count(*) FROM docs WHERE docs MATCH 'erm'").fetchone()[0] != 1:
            raise RuntimeError("HF startup blocked: application SQLite FTS5 trigram check failed")


def check_gateway_loop() -> str:
    """HF's supported Linux CPython must not silently use asyncio fallback.

    Exercise the same Runner/loop factory as the CLI, not uvloop.install() or
    asyncio.run(). Other platforms retain the application's normal fallback.
    No gateway, credentials, profile, database or network connection is opened.
    """
    if sys.platform != "linux" or platform.python_implementation() != "CPython":
        raise RuntimeError("HF startup blocked: expected Linux CPython runtime")
    try:
        import uvloop
        from gateway.event_loop import run_gateway_async
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "HF startup blocked: pinned uvloop or gateway loop selector missing; rebuild from the reviewed source"
        ) from exc
    if uvloop.__version__ != UVLOOP_VERSION:
        raise RuntimeError(
            f"HF startup blocked: uvloop=={UVLOOP_VERSION} required; rebuild with the frozen source lock"
        )

    async def probe():
        return asyncio.get_running_loop()

    try:
        loop = run_gateway_async(probe())
    except Exception as exc:
        raise RuntimeError("HF startup blocked: gateway event-loop smoke test failed") from exc
    if not isinstance(loop, uvloop.Loop) or not loop.is_closed():
        raise RuntimeError("HF startup blocked: gateway did not select and close uvloop.Loop")
    return f"uvloop=={UVLOOP_VERSION}; gateway loop uvloop.Loop"


if __name__ == "__main__":
    try:
        check_sqlite()
        loop_info = check_gateway_loop()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(f"HF SQLite verified: {sys.executable} uses {sqlite3.sqlite_version}")
    print(f"HF event loop verified: {loop_info}")
