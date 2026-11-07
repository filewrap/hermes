"""One-request isolated worker for Telegram tenant slash commands.

The parent supplies a tenant-only HOME/HERMES_HOME and a credential-scrubbed
process environment. This module never starts adapters or the gateway loop; it
only dispatches one already-authorized command through the regular idle command
registry in a separate process.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

os.environ["HERMES_TENANT_COMMAND_WORKER"] = "1"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    candidate = getattr(value, "text", None)
    if isinstance(candidate, str):
        return candidate
    candidate = getattr(value, "result", None)
    if isinstance(candidate, str):
        return candidate
    return str(value)


async def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    raw_source = payload.get("source") or {}
    if not isinstance(raw_source, dict):
        raise ValueError("Invalid source")
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(raw_source.get("chat_id") or ""),
        chat_name=str(raw_source.get("chat_name") or "") or None,
        chat_type=str(raw_source.get("chat_type") or "dm"),
        user_id=str(raw_source.get("user_id") or ""),
        user_name=str(raw_source.get("user_name") or "") or None,
        thread_id=str(raw_source.get("thread_id") or "") or None,
    )
    text = str(payload.get("text") or "")
    if not text.strip().startswith("/"):
        raise ValueError("Not a slash command")
    event = MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        user_id=source.user_id,
        user_name=source.user_name,
        source=source,
        metadata={"thread_id": source.thread_id} if source.thread_id else {},
    )
    # A few legacy config handlers read the adapter's raw text alias rather
    # than MessageEvent.text. In the isolated worker this is plain text, never
    # a live Telethon Message object.
    event.message = text
    # This runner exists only in this child process. The parent has a distinct
    # live owner runner, adapter set, module globals, and session database.
    runner = GatewayRunner()
    quick_key = runner._session_key_for_source(source)
    handled, result = await runner._hm_dispatch_idle_commands(event, source, quick_key)
    return {"handled": bool(handled), "result": _text(result), "text": event.text}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("Request too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid request")
        # Third-party or legacy helpers may print to stdout; keep the IPC stream
        # machine-readable and send incidental output to the captured stderr.
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(_dispatch(payload))
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.flush()
        return 0
    except BaseException as exc:
        print(f"tenant command worker: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
