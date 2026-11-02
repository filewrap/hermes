"""Gateway loop selection must not affect other CLI loops or retry a consumer."""

import asyncio
import selectors
import sys
from types import SimpleNamespace

import pytest

from gateway import event_loop


async def _running_loop():
    return asyncio.get_running_loop()


def test_uses_real_uvloop_without_changing_policy():
    uvloop = pytest.importorskip("uvloop")
    policy = asyncio.get_event_loop_policy()
    loop = event_loop.run_gateway_async(_running_loop())
    assert isinstance(loop, uvloop.Loop)
    assert loop.is_closed()
    assert asyncio.get_event_loop_policy() is policy
    with pytest.raises(RuntimeError):
        asyncio.get_event_loop()


@pytest.mark.parametrize("failure", [ImportError, OSError, RuntimeError, NotImplementedError])
def test_native_unavailable_preserves_selector_policy(monkeypatch, failure):
    def unavailable():
        raise failure("not supported")

    monkeypatch.setitem(sys.modules, "uvloop", SimpleNamespace(new_event_loop=unavailable))
    policy = asyncio.get_event_loop_policy()
    created = []

    def selector_loop():
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        created.append(loop)
        return loop

    monkeypatch.setattr(policy, "new_event_loop", selector_loop)
    loop = event_loop.run_gateway_async(_running_loop())
    assert loop is created[0]
    assert loop._selector is None  # closed by Runner
    assert loop.is_closed()
    assert asyncio.get_event_loop_policy() is policy


def test_missing_uvloop_import_falls_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", None)
    loop = event_loop.run_gateway_async(_running_loop())
    assert isinstance(loop, asyncio.AbstractEventLoop)
    assert not type(loop).__module__.startswith("uvloop")
    assert loop.is_closed()


@pytest.mark.parametrize("platform", ["win32", "cygwin"])
def test_unsupported_platform_never_constructs_uvloop(monkeypatch, platform):
    def unexpected():
        pytest.fail("uvloop must not be constructed on this platform")

    monkeypatch.setattr(event_loop.sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "uvloop", SimpleNamespace(new_event_loop=unexpected))
    loop = event_loop.run_gateway_async(_running_loop())
    assert not type(loop).__module__.startswith("uvloop")


@pytest.mark.parametrize("error", [RuntimeError("consumer failed"), SystemExit(78), asyncio.CancelledError()])
def test_gateway_failure_is_not_retried(error):
    calls = []

    async def fail():
        calls.append(1)
        raise error

    with pytest.raises(type(error)) as raised:
        event_loop.run_gateway_async(fail())
    assert raised.value is error
    assert calls == [1]


def test_runner_cancels_tasks_and_finalizes_async_generators():
    events = []

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    async def stream():
        try:
            yield 1
        finally:
            events.append("finalized")

    async def gateway():
        asyncio.create_task(background())
        generator = stream()
        await anext(generator)
        await asyncio.sleep(0)
        return generator

    generator = event_loop.run_gateway_async(gateway())
    assert sorted(events) == ["cancelled", "finalized"]
    assert generator.ag_frame is None


def test_cli_gateway_entrypoint_runs_uvloop(monkeypatch):
    uvloop = pytest.importorskip("uvloop")
    import gateway.run as gateway_run
    import hermes_cli.gateway as gateway_cli

    seen = []

    async def fake_start_gateway(*, replace, verbosity, force, idle_exit):
        seen.append(asyncio.get_running_loop())
        assert replace is False
        assert verbosity is None
        assert force is False
        assert idle_exit is False
        return True

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", lambda code: seen.append(code))
    for name in ("_guard_official_docker_root_gateway", "_guard_named_profile_under_multiplexer",
                 "_guard_supervised_gateway_conflict", "_guard_existing_gateway_process_conflict",
                 "_apply_startup_watchdog_config", "_respawn_storm_backoff"):
        monkeypatch.setattr(gateway_cli, name, lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli, "_make_exit_diag", lambda: lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: None)
    monkeypatch.setattr(gateway_cli, "_windows_gateway_should_absorb_console_controls", lambda: False)
    gateway_cli.run_gateway(quiet=True)
    assert isinstance(seen[0], uvloop.Loop)
    assert seen[0].is_closed()
    assert seen[1:] == [0]


def test_standalone_gateway_entrypoint_runs_uvloop(monkeypatch):
    uvloop = pytest.importorskip("uvloop")
    import gateway.run as gateway_run

    seen = []

    async def fake_start_gateway(config):
        seen.append(asyncio.get_running_loop())
        return True

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run, "_guard_corrupt_user_config", lambda: None)
    monkeypatch.setattr(gateway_run, "_best_effort", lambda step: None)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", lambda code: seen.append(code))
    monkeypatch.setattr(sys, "argv", ["gateway.run"])
    gateway_run.main()
    assert isinstance(seen[0], uvloop.Loop)
    assert seen[0].is_closed()
    assert seen[1:] == [0]
