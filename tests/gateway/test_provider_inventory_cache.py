"""Provider-inventory snapshot cache: repeated opens reuse the snapshot.

The /model, /provider and /models cards all funnel through
``GatewayRunner._provider_inventory_snapshot``. The snapshot re-reads config,
pool and catalogs on every open; a 60s cache with stale-serve background
refresh covers repeated opens, and any input-file change busts the key so a
fresh credential or catalog refresh shows on the next open.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.provider_inventory_cache import ProviderInventoryCache
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.waiting import async_wait_until


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="123", chat_type="dm"),
    )


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"
    return runner


class _CountingLaps:
    """Fake list_authenticated_providers that counts rebuilds."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_kwargs):
        self.calls += 1
        return []


@pytest.fixture
def snapshot_home(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    return home


@pytest.mark.asyncio
async def test_snapshot_cache_serves_repeated_opens_without_rebuilding(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()

    first = await runner._provider_inventory_snapshot(_event("/providers").source)
    second = await runner._provider_inventory_snapshot(_event("/providers").source)

    assert laps.calls == 1
    assert [row["slug"] for row in first[0]] == [row["slug"] for row in second[0]]
    assert first[1:] == second[1:]


@pytest.mark.asyncio
async def test_snapshot_cache_isolates_caller_mutations(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()

    first = await runner._provider_inventory_snapshot(_event("/providers").source)
    first[0][0]["configured"] = "MUTATED"
    first[0].append({"slug": "fake-row", "configured": True})

    second = await runner._provider_inventory_snapshot(_event("/providers").source)

    assert laps.calls == 1
    assert not any(row.get("configured") == "MUTATED" for row in second[0])
    assert not any(row.get("slug") == "fake-row" for row in second[0])


@pytest.mark.asyncio
async def test_snapshot_cache_busts_on_config_change(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()

    _first = await runner._provider_inventory_snapshot(_event("/providers").source)
    assert laps.calls == 1

    config_path = snapshot_home / "config.yaml"
    config_path.write_text(
        "model:\n  default: model-b\n  provider: openrouter\n",
        encoding="utf-8",
    )
    stat = config_path.stat()
    os.utime(config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000))

    _providers, current_model, _current_provider, _key, _home = (
        await runner._provider_inventory_snapshot(_event("/providers").source)
    )

    assert laps.calls == 2
    assert current_model == "model-b"


@pytest.mark.asyncio
async def test_snapshot_stale_entry_serves_then_refreshes_in_background(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()
    runner._provider_inventory_cache = ProviderInventoryCache(ttl_seconds=0)

    first = await runner._provider_inventory_snapshot(_event("/providers").source)
    assert laps.calls == 1

    second = await runner._provider_inventory_snapshot(_event("/providers").source)
    # The stale rows are served immediately; the rebuild happens behind them.
    assert [row["slug"] for row in second[0]] == [row["slug"] for row in first[0]]
    await async_wait_until(lambda: laps.calls >= 2, what="background snapshot refresh")

    # The background rebuild stored fresh rows under the same key.
    cache = runner._provider_inventory_cache
    key = cache.key_for(
        profile_home=snapshot_home,
        launch_home=snapshot_home,
        session_key="session-1",
        override={},
        user_id="123",
    )
    _state, refreshed = cache.get(key)
    assert refreshed is not None
    assert [row["slug"] for row in refreshed[0]] == [row["slug"] for row in first[0]]


@pytest.mark.asyncio
async def test_snapshot_force_rebuild_bypasses_cache(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()

    await runner._provider_inventory_snapshot(_event("/providers").source)
    await runner._provider_inventory_snapshot(_event("/providers").source, force_rebuild=True)

    assert laps.calls == 2


def test_provider_inventory_cache_key_busts_on_input_file_change(tmp_path):
    cache = ProviderInventoryCache()
    home = tmp_path / "profile"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("model:\n  default: model-a\n", encoding="utf-8")

    kwargs = dict(
        profile_home=home,
        launch_home=home,
        session_key="session-1",
        override={},
        user_id="123",
    )
    first_key = cache.key_for(**kwargs)
    assert cache.key_for(**kwargs) == first_key

    stat = config_path.stat()
    os.utime(config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000))
    assert cache.key_for(**kwargs) != first_key


@pytest.mark.asyncio
async def test_startup_warm_up_calls_authenticated_providers_once(snapshot_home, monkeypatch):
    laps = _CountingLaps()
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", laps)
    runner = _runner()

    await runner._warm_provider_inventory_memos()

    assert laps.calls == 1
