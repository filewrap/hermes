"""Turn errors are also delivered to the owner-configured Telegram logs channel:
a one-line summary plus the redacted gateway-log excerpt, deduped per error
signature, fail-open when unconfigured or undeliverable. The in-chat
"Something went wrong" reply is unchanged."""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_turn_error_logs import (
    TurnErrorLogsCooldown,
    build_turn_error_summary,
    error_logs_channel_id,
    format_turn_error_message,
    report_turn_error_to_logs_channel,
    turn_error_signature,
)
from gateway.session import SessionSource

CHANNEL_ID = "-1001234567890"
LOG_LINES = [
    "2026-11-02T10:24:00 ERROR gateway.run_turn: Agent error in session agent:main",
    "Traceback (most recent call last):",
    "openrouter.client.RateLimitError: 429 cooldown until 2026-11-03 05:30 IST",
    "DEBUG provider key sk-proj-abcdefgh12345678 rejected",
]


class _FakeAdapter:
    """Telegram adapter stand-in: records sends, optionally raises."""

    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.fail:
            raise RuntimeError("send boom")
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True, error=None)


def _profile_home(tmp_path, *, channel_id=CHANNEL_ID, log_lines=LOG_LINES):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "gateway.log").write_text("\n".join(log_lines) + "\n")
    if channel_id is None:
        (home / "config.yaml").write_text("display:\n  background_process_notifications: concise\n")
    else:
        (home / "config.yaml").write_text(f"telegram:\n  error_logs_channel: {channel_id}\n")
    return home


def _runner(adapter, home, provider="openrouter"):
    runner = SimpleNamespace()
    runner._delivery_adapter_for = lambda source: adapter
    runner._resolve_profile_home_for_source = lambda source: home
    runner._session_state = lambda key: SimpleNamespace(
        turn=SimpleNamespace(agent=SimpleNamespace(provider=provider)))
    return runner


def _source(platform=Platform.TELEGRAM):
    return SessionSource(platform=platform, chat_id="123456", user_id="u")


def _err_429():
    err = RuntimeError("OpenRouter request failed: 429 Too Many Requests")
    err.status_code = 429
    return err


def test_error_logs_channel_id_reads_telegram_section():
    assert error_logs_channel_id({"telegram": {"error_logs_channel": -1001234567890}}) == "-1001234567890"
    assert error_logs_channel_id({"telegram": {"error_logs_channel": "@hermes_logs"}}) == "@hermes_logs"
    assert error_logs_channel_id({"telegram": {}}) is None
    assert error_logs_channel_id({"telegram": {"error_logs_channel": "  "}}) is None
    assert error_logs_channel_id({}) is None
    assert error_logs_channel_id(None) is None


def test_build_turn_error_summary_names_provider_and_kind():
    assert build_turn_error_summary(provider="openrouter", status_code=429) == (
        "Turn failed: openrouter rate limited (HTTP 429)")
    assert build_turn_error_summary(provider="openai", status_code=401) == (
        "Turn failed: openai authentication failed (HTTP 401)")
    assert build_turn_error_summary(failure_reason="provider_unavailable") == (
        "Turn failed: provider unavailable")
    assert build_turn_error_summary(error_kind="ConnectionError") == "Turn failed: ConnectionError"
    assert build_turn_error_summary() == "Turn failed: failed"


def test_turn_error_signature_stable_and_distinct():
    base = turn_error_signature(provider="openrouter", status_code=429,
                                error_kind="RateLimitError", error_text="429 boom")
    assert turn_error_signature(provider="openrouter", status_code=429,
                                error_kind="RateLimitError", error_text="429 boom") == base
    assert turn_error_signature(provider="openrouter", status_code=500,
                                error_kind="RateLimitError", error_text="429 boom") != base
    assert turn_error_signature(provider="openrouter", status_code=429,
                                error_kind="RateLimitError", error_text="other boom") != base


def test_cooldown_collapses_repeats_within_window():
    cooldown = TurnErrorLogsCooldown(cooldown_seconds=600)
    assert cooldown.allows("sig", now=1000.0) is True
    cooldown.mark_sent("sig", now=1000.0)
    assert cooldown.allows("sig", now=1100.0) is False
    assert cooldown.allows("other", now=1100.0) is True
    assert cooldown.allows("sig", now=1700.0) is True


def test_format_turn_error_message_carries_summary_and_excerpt():
    text = format_turn_error_message(
        summary="Turn failed: openrouter rate limited (HTTP 429)",
        session_key="agent:main", chat_id="123456",
        excerpt="line one\nline two", truncated=True)
    assert text.startswith("⚠️ Turn failed: openrouter rate limited (HTTP 429)")
    assert "agent:main" in text and "123456" in text
    assert "line one\nline two" in text
    assert "(log excerpt truncated)" in text
    empty = format_turn_error_message(
        summary="Turn failed: failed", session_key=None, chat_id=None, excerpt="", truncated=False)
    assert "(log excerpt unavailable)" in empty


def test_report_delivers_summary_and_redacted_excerpt(tmp_path):
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter()
    runner = _runner(adapter, home)
    delivered = asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", error=_err_429(), status_code=429))
    assert delivered is True
    assert len(adapter.sent) == 1
    chat_id, text = adapter.sent[0]
    assert chat_id == CHANNEL_ID
    assert "Turn failed: openrouter rate limited (HTTP 429)" in text
    assert "RateLimitError: 429 cooldown until 2026-11-03 05:30 IST" in text
    assert "sk-proj-abcdefgh12345678" not in text


def test_report_without_channel_is_noop(tmp_path):
    home = _profile_home(tmp_path, channel_id=None)
    adapter = _FakeAdapter()
    runner = _runner(adapter, home)
    delivered = asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", error=_err_429(), status_code=429))
    assert delivered is False
    assert adapter.sent == []


def test_report_adapter_failure_is_fail_open(tmp_path):
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter(fail=True)
    runner = _runner(adapter, home)
    delivered = asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", error=_err_429(), status_code=429))
    assert delivered is False


def test_report_dedupes_same_error_within_cooldown(tmp_path):
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter()
    runner = _runner(adapter, home)
    cooldown = TurnErrorLogsCooldown(cooldown_seconds=600)
    kwargs = {"error": _err_429(), "status_code": 429, "cooldown": cooldown}
    assert asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", now=1000.0, **kwargs)) is True
    assert asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", now=1100.0, **kwargs)) is False
    assert len(adapter.sent) == 1
    assert asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main", now=1700.0, **kwargs)) is True
    assert len(adapter.sent) == 2


def test_report_non_telegram_turn_is_noop(tmp_path):
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter()
    runner = _runner(adapter, home)
    delivered = asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(Platform.LOCAL), "agent:main", error=_err_429(), status_code=429))
    assert delivered is False
    assert adapter.sent == []


def test_report_failed_result_payload(tmp_path):
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter()
    runner = _runner(adapter, home)
    delivered = asyncio.run(report_turn_error_to_logs_channel(
        runner, _source(), "agent:main",
        failure_reason="provider_unavailable",
        error_text="The selected AI provider is currently unavailable."))
    assert delivered is True
    assert "Turn failed: openrouter provider unavailable" in adapter.sent[0][1]


def test_agent_error_reply_reports_to_logs_channel(tmp_path):
    """The exception path still returns the sanitized in-chat reply AND reports
    the failure to the logs channel."""
    home = _profile_home(tmp_path)
    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)

    async def stop_typing(event, source):
        return None

    runner._hmwa_stop_typing_for_turn = stop_typing
    runner._session_state = lambda key: SimpleNamespace(
        turn=SimpleNamespace(agent=SimpleNamespace(provider="openrouter")))
    runner._delivery_adapter_for = lambda source: adapter
    runner._resolve_profile_home_for_source = lambda source: home
    source = _source()
    prepared = runner._PreparedTurn([], "", None, None, None, None)
    reply = asyncio.run(runner._hmwa_agent_error_reply(
        _err_429(), MessageEvent(text="x", source=source), source, None, "agent:main", prepared))
    assert "Something went wrong" in reply
    assert len(adapter.sent) == 1
    chat_id, text = adapter.sent[0]
    assert chat_id == CHANNEL_ID
    assert "Turn failed: openrouter rate limited (HTTP 429)" in text
    assert "RateLimitError: 429 cooldown until 2026-11-03 05:30 IST" in text
    assert "sk-proj-abcdefgh12345678" not in text
