"""Startup gates: an exit that never reaches running mode must not look like a clean shutdown —
and an open DM policy with no allowlist no longer refuses startup at all (it fails closed at
message level instead, because the allow-all opt-in the refusal used to demand is gone)."""
import asyncio
import logging
import threading

import pytest


def test_open_dm_policy_no_longer_refuses_startup(monkeypatch, caplog):
    """The open-DM refusal gate is gone: the same config that used to abort the
    start now warns and proceeds (the preflight check returns False)."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    for name in ('GATEWAY_ALLOW_ALL_USERS', 'WHATSAPP_ALLOW_ALL_USERS', 'WHATSAPP_DM_POLICY'):
        monkeypatch.delenv(name, raising=False)
    # Previously this config refused startup (startup_failed + clean exit before
    # recovery) because no allow-all opt-in was set; that gate is removed.
    config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(
        enabled=True, extra={'dm_policy': 'open'})})
    runner = object.__new__(GatewayRunner)
    runner.config = config

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        refused = runner._start_check_access_policy()

    assert refused is False
    assert "No env user allowlists configured" in caplog.text


@pytest.mark.asyncio
async def test_ready_publication_failure_stops_cron_and_housekeeping(monkeypatch):
    from gateway import run_bootstrap, run_runtime
    from gateway.config import GatewayConfig
    from hermes_constants import get_hermes_home

    get_hermes_home().chmod(0o700)
    stopped = threading.Event()
    threads = []

    def start_owned_threads(runner):
        for _ in range(2):
            thread = threading.Thread(target=stopped.wait, daemon=True)
            thread.start()
            threads.append(thread)
        return stopped, None, *threads

    def refuse_ready(runner):
        raise RuntimeError('gateway stopped before session API readiness')

    monkeypatch.setattr(run_bootstrap, '_start_gateway_start_cron_and_housekeeping', start_owned_threads)
    monkeypatch.setattr(run_runtime, 'publish_gateway_runtime_ready', refuse_ready)
    try:
        with pytest.raises(RuntimeError, match='before session API readiness'):
            await asyncio.wait_for(run_bootstrap.start_gateway(GatewayConfig()), 30)
        assert threads
        assert stopped.is_set(), 'READY failure left the cron ticker running'
        assert all(not thread.is_alive() for thread in threads)
    finally:
        stopped.set()
        for thread in threads:
            thread.join(timeout=2)
