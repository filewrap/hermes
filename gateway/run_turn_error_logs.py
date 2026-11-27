"""Turn-error delivery to the owner-configured Telegram logs channel.

A failed gateway turn shows the user only the sanitized "Something went wrong"
reply; the real cause lived in the gateway log, which the owner had to export
by hand. This sink reports every such failure to a Telegram channel the owner
configures, with a one-line summary plus the redacted log excerpt, so the cause
is visible without touching the host.

Config (``config.yaml``, top-level ``telegram:`` section — read through the
gateway's raw-YAML loader, no DEFAULT_CONFIG merge, no new env var)::

    telegram:
      error_logs_channel: -1001234567890   # or @channelusername; absent/blank = off

Delivery reuses the turn's own Telegram adapter send path (no second client, no
bot token) and the owning profile's scope: the profile home is resolved from the
source, never from ``os.environ``. The sink is fail-open — any failure logs and
returns False, never raising into the turn — and a per-signature cooldown keeps
a crashing loop from flooding the channel.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Optional

from gateway.config import Platform

logger = logging.getLogger(__name__)

# config.yaml key under the top-level ``telegram:`` section.
ERROR_LOGS_CHANNEL_KEY = "error_logs_channel"
# Same error signature collapses to one channel message per window.
ERROR_LOGS_COOLDOWN_SECONDS = 600.0
# Newest gateway-log lines per report; mirrors gateway_logs.DEFAULT_LOG_LINES.
ERROR_LOGS_EXCERPT_LINES = 50


def error_logs_channel_id(config: dict | None) -> str | None:
    """``telegram.error_logs_channel`` from raw user YAML; ``None`` when absent/blank.

    Accepts a numeric channel id (YAML parses ``-100…`` as int) or a username.
    """
    from hermes_cli.config import cfg_get

    raw = cfg_get(config, "telegram", ERROR_LOGS_CHANNEL_KEY)
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    return text or None


def build_turn_error_summary(
    *, provider: Optional[str] = None, status_code: Any = None,
    error_kind: Optional[str] = None, failure_reason: Optional[str] = None,
) -> str:
    """One plain-words line: provider + error kind, never a raw traceback."""
    if status_code == 429:
        kind = "rate limited (HTTP 429)"
    elif status_code == 401:
        kind = "authentication failed (HTTP 401)"
    elif isinstance(status_code, int):
        kind = f"failed (HTTP {status_code})"
    elif failure_reason:
        kind = str(failure_reason).replace("_", " ")
    elif error_kind:
        kind = str(error_kind)
    else:
        kind = "failed"
    provider_part = f"{provider} " if provider else ""
    return f"Turn failed: {provider_part}{kind}"


def turn_error_signature(
    *, provider: Optional[str] = None, status_code: Any = None,
    error_kind: Optional[str] = None, error_text: str = "",
) -> str:
    """Stable dedup key for one failure shape. The error's first line keeps distinct
    failures distinct without letting variable tails (ids, timestamps) split them."""
    first_line = (str(error_text or "").strip().splitlines() or [""])[0][:120]
    raw = "|".join([str(provider or ""), str(status_code or ""),
                    str(error_kind or ""), first_line])
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


class TurnErrorLogsCooldown:
    """Minimal per-signature cooldown: a crashing loop sends one channel message
    per signature per window, not one per failed turn. Only a delivered message
    marks the signature, so a failed send does not suppress the next attempt."""

    def __init__(self, cooldown_seconds: float = ERROR_LOGS_COOLDOWN_SECONDS):
        self._cooldown_seconds = float(cooldown_seconds)
        self._last_sent: dict[str, float] = {}

    def allows(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last_sent.get(key)
        return last is None or now - last >= self._cooldown_seconds

    def mark_sent(self, key: str, *, now: float | None = None) -> None:
        self._last_sent[key] = time.monotonic() if now is None else now


DEFAULT_TURN_ERROR_LOGS_COOLDOWN = TurnErrorLogsCooldown()


def format_turn_error_message(
    *, summary: str, session_key: Optional[str], chat_id: Optional[str],
    excerpt: str, truncated: bool,
) -> str:
    """Channel message: summary line, turn context, then the log excerpt fenced
    so arbitrary log text cannot break the message's rich formatting."""
    blocks = [f"⚠️ {summary}"]
    context = " / ".join(part for part in (
        f"session {session_key}" if session_key else "",
        f"chat {chat_id}" if chat_id else "",
    ) if part)
    if context:
        blocks.append(context)
    if excerpt:
        blocks.append("```\n" + excerpt.replace("```", "'''") + "\n```")
        if truncated:
            blocks.append("(log excerpt truncated)")
    else:
        blocks.append("(log excerpt unavailable)")
    return "\n\n".join(blocks)


def _turn_provider(runner: Any, session_key: Optional[str]) -> Optional[str]:
    try:
        state = runner._session_state(session_key)
        agent = getattr(getattr(state, "turn", None), "agent", None)
        return getattr(agent, "provider", None)
    except Exception:
        return None


def _read_error_excerpt(profile_home: Path) -> tuple[str, bool]:
    """Newest gateway-log lines for the failed turn, redacted exactly like /logs
    (``gateway_logs.read_gateway_logs`` forces secret/token redaction)."""
    from plugins.platforms.telegram.gateway_logs import read_gateway_logs

    try:
        result = read_gateway_logs(profile_home, log="gateway", limit=ERROR_LOGS_EXCERPT_LINES)
    except Exception:
        logger.debug("Turn-error log excerpt unavailable for %s", profile_home, exc_info=True)
        return "", False
    return str(result.get("text") or ""), bool(result.get("truncated"))


async def report_turn_error_to_logs_channel(
    runner: Any, source: Any, session_key: Optional[str], *,
    error: Optional[BaseException] = None, status_code: Any = None,
    failure_reason: Optional[str] = None, error_text: Optional[str] = None,
    provider: Optional[str] = None,
    cooldown: Optional[TurnErrorLogsCooldown] = None, now: Optional[float] = None,
) -> bool:
    """Send one turn-error report to the owning profile's Telegram logs channel.

    Fail-open: any failure (no channel configured, no Telegram adapter, log read
    or send error) logs and returns False, never raising into the turn. Only
    Telegram turns report: the channel is a Telegram channel and delivery reuses
    the turn's own Telegram adapter send path.
    """
    try:
        return await _report_turn_error_to_logs_channel(
            runner, source, session_key, error=error, status_code=status_code,
            failure_reason=failure_reason, error_text=error_text, provider=provider,
            cooldown=cooldown, now=now,
        )
    except Exception:
        logger.debug("Turn-error logs-channel delivery failed", exc_info=True)
        return False


async def _report_turn_error_to_logs_channel(
    runner: Any, source: Any, session_key: Optional[str], *,
    error: Optional[BaseException], status_code: Any, failure_reason: Optional[str],
    error_text: Optional[str], provider: Optional[str],
    cooldown: Optional[TurnErrorLogsCooldown], now: Optional[float],
) -> bool:
    if getattr(source, "platform", None) != Platform.TELEGRAM:
        return False
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return False
    # The owning profile's home, resolved from the source — never os.environ,
    # which holds the launch profile under multiplexing.
    home = Path(runner._resolve_profile_home_for_source(source))
    from gateway.run import _load_gateway_config

    channel_id = error_logs_channel_id(_load_gateway_config(home / "config.yaml"))
    if not channel_id:
        return False
    if provider is None:
        provider = _turn_provider(runner, session_key)
    error_kind = type(error).__name__ if error is not None else None
    if error_text is None:
        error_text = str(error) if error is not None else ""
    summary = build_turn_error_summary(
        provider=provider, status_code=status_code,
        error_kind=error_kind, failure_reason=failure_reason,
    )
    signature = turn_error_signature(
        provider=provider, status_code=status_code,
        error_kind=error_kind, error_text=error_text,
    )
    cooldown = cooldown if cooldown is not None else DEFAULT_TURN_ERROR_LOGS_COOLDOWN
    now = time.monotonic() if now is None else now
    dedup_key = f"{home}:{signature}"
    if not cooldown.allows(dedup_key, now=now):
        return False
    excerpt, truncated = _read_error_excerpt(home)
    text = format_turn_error_message(
        summary=summary, session_key=session_key,
        chat_id=getattr(source, "chat_id", None),
        excerpt=excerpt, truncated=truncated,
    )
    result = await adapter.send(channel_id, text)
    if not bool(getattr(result, "success", False)):
        logger.warning("Turn-error logs-channel send failed: %s", getattr(result, "error", "?"))
        return False
    cooldown.mark_sent(dedup_key, now=now)
    return True
