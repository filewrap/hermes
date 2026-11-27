"""The limited badge counts down from the reset and clears itself.

A provider rate-limited at 10 PM with a 12h reset must stop showing
"limited · 12h" by 10 AM: the remaining time is computed fresh from the
reset timestamp on every render, and once the reset has passed the badge
is gone. A provider with no reset time is limited only while the pool's
own cooldown (last_status_at + TTL) still binds; after that the badge
clears on its own. And any successful request to the provider clears the
limited state at once — a success proves it is working again.
"""

import time
from types import SimpleNamespace

from plugins.platforms.telegram.provider_management_ui_text import (
    _limited_badge,
    provider_cooldown_state,
)


def _entry(**updates):
    row = {
        "last_status": "ok",
        "last_status_at": None,
        "last_error_code": None,
        "last_error_reason": None,
        "last_error_message": None,
        "last_error_reset_at": None,
        "failure_reason": None,
    }
    row.update(updates)
    return SimpleNamespace(**row)


class _FakePool:
    def __init__(self, entries):
        self._entries = list(entries)

    def entries(self):
        return list(self._entries)


def _freeze_time(monkeypatch, at):
    monkeypatch.setattr(time, "time", lambda: at)


def _exhausted_pool(monkeypatch, entries):
    monkeypatch.setattr(
        "agent.credential_pool.load_pool", lambda slug: _FakePool(entries))


# ── The countdown: a 12h reset at 10 PM is gone by 10 AM ─────────────


def test_badge_counts_down_from_the_reset(monkeypatch):
    t0 = 1795500000.0  # 10 PM
    _exhausted_pool(monkeypatch, [
        _entry(last_status="exhausted", last_status_at=t0,
               last_error_code=429, last_error_reset_at=t0 + 12 * 3600),
    ])
    _freeze_time(monkeypatch, t0)
    assert _limited_badge("nous")[0] == "limited · 12h"
    # Six hours later the same render shows six hours left, not twelve.
    _freeze_time(monkeypatch, t0 + 6 * 3600)
    assert _limited_badge("nous")[0] == "limited · 6h"


def test_badge_disappears_once_the_reset_has_passed(monkeypatch):
    t0 = 1795500000.0  # 10 PM
    _exhausted_pool(monkeypatch, [
        _entry(last_status="exhausted", last_status_at=t0,
               last_error_code=429, last_error_reset_at=t0 + 12 * 3600),
    ])
    # 10 AM, twelve hours later: the reset has passed, no badge.
    _freeze_time(monkeypatch, t0 + 12 * 3600 + 60)
    assert _limited_badge("nous") is None
    assert provider_cooldown_state("nous") is None


# ── No reset time: the badge must still expire on the pool's TTL ──────


def test_badge_with_unknown_reset_expires_when_the_pool_cooldown_elapses(monkeypatch):
    t0 = 1795500000.0
    # 429, hit two hours ago, no provider-supplied reset: the pool's own
    # 1h 429 cooldown has elapsed, so the provider is usable again.
    _exhausted_pool(monkeypatch, [
        _entry(last_status="exhausted", last_status_at=t0 - 2 * 3600,
               last_error_code=429, last_error_reset_at=None),
    ])
    _freeze_time(monkeypatch, t0)
    assert _limited_badge("nous") is None
    assert provider_cooldown_state("nous") is None


# ── Success clears the limited state at once ─────────────────────────


def _real_entry(provider="openrouter", **updates):
    from agent.credential_pool import PooledCredential

    row = {
        "provider": provider,
        "id": "e1",
        "label": f"{provider}-1",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-test-1",
        "last_status": "exhausted",
        "last_status_at": 1795500000.0,
        "last_error_code": 429,
        "last_error_reset_at": 1795500000.0 + 12 * 3600,
    }
    row.update(updates)
    return PooledCredential(**row)


def _real_pool(entries, provider="openrouter"):
    from agent.credential_pool import CredentialPool

    pool = CredentialPool(provider, list(entries))
    pool._persist = lambda **kwargs: None  # type: ignore[method-assign]
    return pool


def test_mark_success_clears_an_exhausted_entry():
    pool = _real_pool([_real_entry()])
    assert pool.mark_success(credential_id="e1") is True
    entry = pool.entries()[0]
    # reset_status clears the status (None), it does not stamp "ok".
    assert entry.last_status is None
    assert entry.last_error_reset_at is None
    assert entry.last_status_at is None
    # Second success is a no-op: nothing left to clear.
    assert pool.mark_success(credential_id="e1") is False


def test_mark_success_leaves_dead_and_healthy_entries_alone():
    pool = _real_pool([
        _real_entry(id="dead-1", last_status="dead"),
        _real_entry(id="ok-1", last_status="ok", last_error_reset_at=None),
    ])
    assert pool.mark_success(credential_id="dead-1") is False
    assert pool.mark_success(credential_id="ok-1") is False
    assert pool.mark_success(credential_id="missing") is False
    assert pool.entries()[0].last_status == "dead"
    assert pool.entries()[1].last_status == "ok"


def test_clear_pool_exhaustion_on_success_uses_the_serving_entry():
    from agent.turn_response_check import _clear_pool_exhaustion_on_success

    pool = _real_pool([_real_entry()], provider="openrouter")
    agent = SimpleNamespace(
        _credential_pool=pool, _credential_pool_entry_id="e1",
        provider="openrouter", base_url=None, api_key="sk-test-1",
    )
    assert _clear_pool_exhaustion_on_success(agent) is True
    assert pool.entries()[0].last_status is None


def test_clear_pool_exhaustion_on_success_fails_closed_on_mismatch():
    from agent.turn_response_check import _clear_pool_exhaustion_on_success

    pool = _real_pool([_real_entry()], provider="openrouter")
    # Agent on another provider: the pool is not touched.
    other = SimpleNamespace(
        _credential_pool=pool, _credential_pool_entry_id="e1",
        provider="nous", base_url=None, api_key="sk-test-1",
    )
    assert _clear_pool_exhaustion_on_success(other) is False
    assert pool.entries()[0].last_status == "exhausted"
    # No pool at all: False, never an exception.
    assert _clear_pool_exhaustion_on_success(SimpleNamespace()) is False


def test_badge_with_unknown_reset_still_shows_while_the_cooldown_binds(monkeypatch):
    t0 = 1795500000.0
    # 429, hit thirty minutes ago, no reset: still inside the 1h cooldown
    # (two credentials, so the sole-credential 60s TTL does not apply).
    _exhausted_pool(monkeypatch, [
        _entry(last_status="exhausted", last_status_at=t0 - 30 * 60,
               last_error_code=429, last_error_reset_at=None),
        _entry(last_status="exhausted", last_status_at=t0 - 30 * 60,
               last_error_code=429, last_error_reset_at=None),
    ])
    _freeze_time(monkeypatch, t0)
    assert _limited_badge("nous")[0] == "limited"
