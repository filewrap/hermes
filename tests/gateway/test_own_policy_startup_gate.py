"""Startup access behaviour after the access-control redesign.

The old own-policy open startup gate is gone: there is no allow-all switch to
opt into, so startup never refuses to start for access reasons. Instead:

- ``_start_check_access_policy`` only warns about missing allowlists and always
  returns False (startup proceeds; access is owner-or-allowlist only);
- ``_start_check_owner_configured`` warns LOUDLY when no owner is configured
  (``OWNER_TELEGRAM_ID`` missing or invalid in the profile ``.env``) but never
  refuses startup — the gateway still starts and serves allowlisted senders.
"""

import json
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner


def _ownerless_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("OWNER_TELEGRAM_ID", "DEEP_OWNER_TELEGRAM_ID", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_owner_check_warns_without_owner_and_never_raises(monkeypatch, tmp_path, caplog):
    """No owner configured → a loud WARNING naming the config key; no exception."""
    _ownerless_home(monkeypatch, tmp_path)
    runner = object.__new__(GatewayRunner)

    with caplog.at_level(logging.WARNING, logger="gateway.run_startup"):
        runner._start_check_owner_configured()

    warning = "\n".join(rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING)
    assert "WARNING: no owner configured" in warning
    assert "OWNER_TELEGRAM_ID" in warning
    assert "Starting anyway" in warning


def test_owner_check_is_silent_with_owner_configured(monkeypatch, tmp_path, caplog):
    """A numeric ``OWNER_TELEGRAM_ID`` in the profile .env → no warning."""
    home = _ownerless_home(monkeypatch, tmp_path)
    (home / ".env").write_text("OWNER_TELEGRAM_ID=123456\n", encoding="utf-8")
    runner = object.__new__(GatewayRunner)

    with caplog.at_level(logging.WARNING, logger="gateway.run_startup"):
        runner._start_check_owner_configured()

    assert not [rec for rec in caplog.records if "no owner configured" in rec.getMessage()]


def test_owner_warning_is_stamped_into_runtime_status(monkeypatch, tmp_path):
    """The ``owner_warning`` field reaches ``gateway_state.json`` via the status
    surface, so ``gateway status`` can surface it."""
    from gateway.status import _prepare_runtime_status_update

    home = _ownerless_home(monkeypatch, tmp_path)
    warning = (
        "WARNING: no owner configured — OWNER_TELEGRAM_ID is missing or invalid in the "
        f"profile .env ({home}/.env). Owner-only commands will refuse everyone until it is "
        "set to a numeric Telegram user ID. Starting anyway; access is restricted to the allowlist."
    )
    _path, payload, _previous = _prepare_runtime_status_update(
        load_existing=False, owner_warning=warning
    )
    assert payload["owner_warning"] == warning


def test_access_policy_check_never_refuses_startup(monkeypatch, tmp_path):
    """An "open" policy with the (now dead) allow-all env set must not refuse
    startup: the check returns False and the gateway starts fail-closed."""
    _ownerless_home(monkeypatch, tmp_path)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    config = GatewayConfig(
        platforms={
            Platform.YUANBAO: PlatformConfig(
                enabled=True,
                extra={"dm_policy": "open"},
            ),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    assert runner._start_check_access_policy() is False


@pytest.mark.asyncio
async def test_startup_proceeds_without_owner(monkeypatch, tmp_path, caplog):
    """End to end: an ownerless gateway with no platforms enabled starts
    (returns True, no clean-exit refusal) and the owner warning is logged."""
    _ownerless_home(monkeypatch, tmp_path)
    config = GatewayConfig(platforms={}, sessions_dir=tmp_path / "sessions")
    runner = GatewayRunner(config)

    try:
        with caplog.at_level(logging.WARNING, logger="gateway.run_startup"):
            ok = await runner.start()
    finally:
        await runner.stop()

    assert ok is True
    assert runner.should_exit_cleanly is False
    assert any("no owner configured" in rec.getMessage() for rec in caplog.records)
    # The status writer flushes asynchronously; poll for the stamped file.
    from tests.waiting import wait_until

    state_path = tmp_path / ".hermes" / "gateway_state.json"

    def _warning_stamped() -> bool:
        if not state_path.exists():
            return False
        state = json.loads(state_path.read_text())
        return "no owner configured" in str(state.get("owner_warning") or "")

    assert wait_until(_warning_stamped, timeout=10.0)
