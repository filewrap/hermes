"""Restart recovery uses current routing and never borrows another profile's heartbeat."""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionStore, SessionSource
from hermes_cli import goals
from hermes_cli.heartbeat import HeartbeatManager, HeartbeatState
from persistence_assembly import open_persistence, profile_scope


@pytest.mark.asyncio
async def test_restore_retries_persisted_routes_in_their_own_profiles(tmp_path, monkeypatch):
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    home = tmp_path / '.hermes'
    named = home / 'profiles' / 'work'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, named)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    entries = []
    try:
        for profile, status, topic in [(None, 'active', '11'), ('work', 'active', '22'),
                                       ('work', 'paused', '33'), (None, 'cleared', '44')]:
            source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat',
                                   thread_id=topic, profile=profile, scope_id='workspace')
            with _profile_runtime_scope(named if profile else home):
                entry = store.get_or_create_session(source)
                manager = HeartbeatManager(entry.session_id)
                manager.set('check ' + topic, 60)
                if status == 'paused':
                    manager.pause()
                elif status == 'cleared':
                    manager.clear()
            entries.append(entry)
        # Reload the real persisted routing index as a fresh process would.
        store.close_all_db_handles()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = SessionStore(home / 'sessions', config)
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._profile_name_for_source = lambda source, adapter_profile=None: source.profile
        runner._delivery_adapter_for = lambda source: object()
        runner._run_in_executor_with_context = asyncio.to_thread
        original = runner.session_store.list_sessions
        with monkeypatch.context() as patch:
            patch.setattr(runner.session_store, 'list_sessions', lambda: (_ for _ in ()).throw(OSError('cold index')))
            await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == {}
        assert original()
        # A poller inherited from work must still restore the default profile too.
        with _profile_runtime_scope(named):
            await restore_heartbeat_watches(runner)
        expected = {e.session_key: (e.origin, e.session_id) for e in entries[:2]}
        assert runner._heartbeat_watch == expected
        with monkeypatch.context() as patch:
            patch.setattr('hermes_cli.heartbeat.load_heartbeat', lambda sid: None)
            await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == expected
        runner._heartbeat_watch.clear()
        await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == expected
    finally:
        store.close_all_db_handles()
        if 'runner' in locals():
            runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_restore_skips_session_sweep_when_no_heartbeats_exist(tmp_path, monkeypatch):
    """Idle case: no ACTIVE ``heartbeat:*`` row in any served profile → the restore poll must not
    sweep the routing index (each swept origin re-parses that profile's config/secrets). An active
    row, or a store the probe cannot read, brings the sweep back."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    home = tmp_path / '.hermes'
    named = home / 'profiles' / 'work'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, named)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    try:
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat',
                               thread_id='7', profile=None, scope_id=None)
        store.get_or_create_session(source)
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = store
        runner._heartbeat_watch = {}
        runner._run_in_executor_with_context = asyncio.to_thread
        sweeps = []
        original = store.list_sessions
        monkeypatch.setattr(store, 'list_sessions',
                            lambda: (sweeps.append(1), original())[1])
        await restore_heartbeat_watches(runner)
        assert sweeps == []
        assert runner._heartbeat_watch == {}

        # A cleared heartbeat keeps its row (status=cleared): still nothing to restore.
        dbs[str(named)].state_meta.set('heartbeat:old', HeartbeatState(
            prompt='p', interval_seconds=60, status='cleared').to_json())
        await restore_heartbeat_watches(runner)
        assert sweeps == []

        # An ACTIVE row in the secondary store opens the gate.
        dbs[str(named)].state_meta.set('heartbeat:live', HeartbeatState(
            prompt='p', interval_seconds=60, status='active').to_json())
        await restore_heartbeat_watches(runner)
        assert sweeps == [1]

        # A `-p work` multiplexer's routing home is the named profile; an active heartbeat that
        # lives only in the DEFAULT store must still bring the sweep back.
        dbs[str(named)].state_meta.set('heartbeat:live', HeartbeatState(
            prompt='p', interval_seconds=60, status='cleared').to_json())
        store._routing_home = named
        await restore_heartbeat_watches(runner)
        assert sweeps == [1], 'no active row anywhere: still idle'
        dbs[str(home)].state_meta.set('heartbeat:root', HeartbeatState(
            prompt='p', interval_seconds=60, status='active').to_json())
        await restore_heartbeat_watches(runner)
        assert sweeps == [1, 1], 'active row in the default store must be seen from a named routing home'
        dbs[str(home)].state_meta.set('heartbeat:root', HeartbeatState(
            prompt='p', interval_seconds=60, status='cleared').to_json())

        # Fail OPEN: a store the probe cannot open must not suppress the sweep.
        monkeypatch.setattr('gateway.run_idle_gates._profile_session_db_probe', lambda _home: None)
        await restore_heartbeat_watches(runner)
        assert sweeps == [1, 1, 1]
    finally:
        store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_restore_registers_gc_wakeup_when_origin_carries_no_profile(tmp_path, monkeypatch):
    """An allowgc group session's persisted origin carries no profile: at ingress the gc
    runtime lived only in the wire-invisible identity (which never persists), while the
    session key took the gc namespace from it. The scan must therefore group the entry by
    its session-key namespace — the ground truth for where the session's wakeup rows live —
    not by the origin's fallback to the launch home. Without that, ``has_pending`` reads
    the launch store, finds nothing, and the wakeup never fires — with no warning either,
    because the session is already in ``scanned`` so the orphan sweep skips it. No
    allowgc.json here on purpose: the restore must not depend on the allowgc fallback."""
    import time

    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    from gateway.session import SessionEntry
    from hermes_cli.wakeups import WakeupManager

    home = tmp_path / '.hermes'
    gc_home = home / 'profiles' / 'gc-1002397157264'
    gc_home.mkdir(parents=True)
    (gc_home / 'config.yaml').write_text('{}')
    (gc_home / '.env').write_text('')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, gc_home)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    try:
        gc_source = SessionSource(platform=Platform.TELEGRAM, chat_id='-1002397157264',
                                  chat_type='group', profile='gc-1002397157264',
                                  user_id='6802929470')
        with _profile_runtime_scope(gc_home):
            gc_entry = store.get_or_create_session(gc_source)
            wk, err, _ = WakeupManager(gc_entry.session_id).schedule(
                'after rate-limit reset', delay='1m', now=time.time() - 3600)
            assert wk is not None, err
        # The live shape: the key came from the ingress identity's gc namespace, but the
        # persisted origin never carried the profile.
        key = gc_entry.session_key
        raw = store._entries[key]
        origin = dict(raw.origin.to_dict())
        origin['profile'] = None
        store._entries[key] = SessionEntry(
            session_key=raw.session_key, session_id=raw.session_id,
            created_at=raw.created_at, updated_at=raw.updated_at,
            origin=SessionSource.from_dict(origin), platform=raw.platform,
            chat_type=raw.chat_type, transport_profile=raw.transport_profile)
        store._save_entry(key)
        launch_source = SessionSource(platform=Platform.TELEGRAM, chat_id='6802929470',
                                      chat_type='dm', profile=None, user_id='6802929470')
        with _profile_runtime_scope(home):
            launch_entry = store.get_or_create_session(launch_source)
            wk2, err2, _ = WakeupManager(launch_entry.session_id).schedule(
                'launch check', delay='1m', now=time.time() - 3600)
            assert wk2 is not None, err2
        # Reload as a fresh process would.
        store.close_all_db_handles()
        for db in dbs.values():
            db.close()
        dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, gc_home)}
        monkeypatch.setattr(goals, '_DB_CACHE', dbs)
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = SessionStore(home / 'sessions', config)
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._run_in_executor_with_context = asyncio.to_thread
        await restore_heartbeat_watches(runner)
        assert gc_entry.session_key in runner._heartbeat_watch
        assert launch_entry.session_key in runner._heartbeat_watch
    finally:
        store.close_all_db_handles()
        if 'runner' in locals():
            runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_restore_sweep_reads_wakeups_without_the_profile_scope(tmp_path, monkeypatch, caplog):
    """The orphan sweep only needs the wakeup rows, never the profile's secrets: it must
    read them through the lightweight session-db probe, so a profile whose runtime scope
    cannot be entered still surfaces its orphan rows instead of staying silent."""
    import gateway.run as run_mod
    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    from hermes_cli.wakeups import Wakeup, WakeupList

    home = tmp_path / '.hermes'
    gc_home = home / 'profiles' / 'gc-1002397157264'
    gc_home.mkdir(parents=True)
    (gc_home / 'config.yaml').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, gc_home)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    try:
        dbs[str(gc_home)].state_meta.set('wakeups:ghost-gc', WakeupList(pending=[
            Wakeup(id='wk_ghostgc', prompt='check', due_at=1.0, created_at=1.0)]).to_json())
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = store
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._run_in_executor_with_context = asyncio.to_thread
        real_scope = run_mod._profile_runtime_scope

        def failing_scope(home_arg, *args, **kwargs):
            if Path(home_arg) == gc_home:
                raise RuntimeError('bind failed for test')
            return real_scope(home_arg, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(run_mod, '_profile_runtime_scope', failing_scope)
            with caplog.at_level(logging.WARNING, logger='gateway.run'):
                await restore_heartbeat_watches(runner)
        assert any('ghost-gc' in record.message and 'orphan' in record.message.lower()
                   for record in caplog.records)
        assert runner._heartbeat_watch == {}
    finally:
        store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_restore_warns_for_scanned_session_with_no_watch(tmp_path, monkeypatch, caplog):
    """A pending wakeup whose session the routing index DOES list, but for which no watch
    was registered (here the profile scope cannot be entered, so the group is skipped),
    must still surface as one orphan WARNING — the sweep must not skip it just because
    the main loop already saw the entry."""
    import time

    import gateway.run as run_mod
    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    from gateway.session import SessionEntry
    from hermes_cli.wakeups import WakeupManager

    home = tmp_path / '.hermes'
    gc_home = home / 'profiles' / 'gc-1002397157264'
    gc_home.mkdir(parents=True)
    (gc_home / 'config.yaml').write_text('{}')
    (gc_home / '.env').write_text('')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, gc_home)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    try:
        gc_source = SessionSource(platform=Platform.TELEGRAM, chat_id='-1002397157264',
                                  chat_type='group', profile='gc-1002397157264',
                                  user_id='6802929470')
        with _profile_runtime_scope(gc_home):
            gc_entry = store.get_or_create_session(gc_source)
            wk, err, _ = WakeupManager(gc_entry.session_id).schedule(
                'after rate-limit reset', delay='1m', now=time.time() - 3600)
            assert wk is not None, err
        key = gc_entry.session_key
        raw = store._entries[key]
        origin = dict(raw.origin.to_dict())
        origin['profile'] = None
        store._entries[key] = SessionEntry(
            session_key=raw.session_key, session_id=raw.session_id,
            created_at=raw.created_at, updated_at=raw.updated_at,
            origin=SessionSource.from_dict(origin), platform=raw.platform,
            chat_type=raw.chat_type, transport_profile=raw.transport_profile)
        store._save_entry(key)
        launch_source = SessionSource(platform=Platform.TELEGRAM, chat_id='6802929470',
                                      chat_type='dm', profile=None, user_id='6802929470')
        with _profile_runtime_scope(home):
            launch_entry = store.get_or_create_session(launch_source)
            wk2, err2, _ = WakeupManager(launch_entry.session_id).schedule(
                'launch check', delay='1m', now=time.time() - 3600)
            assert wk2 is not None, err2
        store.close_all_db_handles()
        for db in dbs.values():
            db.close()
        dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, gc_home)}
        monkeypatch.setattr(goals, '_DB_CACHE', dbs)
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = SessionStore(home / 'sessions', config)
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._run_in_executor_with_context = asyncio.to_thread
        real_scope = run_mod._profile_runtime_scope

        def failing_scope(home_arg, *args, **kwargs):
            if Path(home_arg) == gc_home:
                raise RuntimeError('bind failed for test')
            return real_scope(home_arg, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(run_mod, '_profile_runtime_scope', failing_scope)
            with caplog.at_level(logging.WARNING, logger='gateway.run'):
                await restore_heartbeat_watches(runner)
        assert launch_entry.session_key in runner._heartbeat_watch
        assert gc_entry.session_key not in runner._heartbeat_watch
        assert any(gc_entry.session_id in record.message
                   and 'orphan' in record.message.lower()
                   for record in caplog.records)
    finally:
        store.close_all_db_handles()
        if 'runner' in locals():
            runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_startup_arms_retry_poller_even_without_any_watches(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._heartbeat_watch = {}
    runner._background_tasks = set()
    runner._ensure_hosted_room_worker = AsyncMock()
    runner._hosted_room_worker_watcher = AsyncMock()
    runner._spawn_supervised = lambda *args: None
    runner._start_loop_heartbeat_task = lambda: None
    runner.hooks = SimpleNamespace(loaded_hooks=[], emit=AsyncMock())
    runner.adapters = {}
    runner._send_update_notification = AsyncMock(return_value=True)
    runner.session_store = SimpleNamespace(list_sessions=list)
    runner._run_in_executor_with_context = asyncio.to_thread
    monkeypatch.setattr('gateway.channel_directory.build_channel_directory', AsyncMock(return_value={}))
    await runner._start_post_connect_services(0)
    task = getattr(runner, '_heartbeat_poll_task', None)
    try:
        assert task is not None and not task.done()
        assert runner._heartbeat_watch == {}
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restore_enters_each_profile_scope_once_per_scan(tmp_path, monkeypatch):
    """N routed sessions in one profile cost one scope entry, not N (scope entry re-parses config)."""
    from gateway import run_heartbeat_restore
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    home = tmp_path / '.hermes'
    named = home / 'profiles' / 'work'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('{}')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(p): open_persistence(profile_scope(home=p)) for p in (home, named)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / 'sessions', config)
    try:
        expected = {}
        for profile in (None, 'work', 'work', None, 'work'):
            source = SessionSource(platform=Platform.TELEGRAM, chat_id='chat',
                                   thread_id=str(len(expected)), profile=profile, scope_id='workspace')
            with _profile_runtime_scope(named if profile else home):
                entry = store.get_or_create_session(source)
                HeartbeatManager(entry.session_id).set('check', 60)
            expected[entry.session_key] = (entry.origin, entry.session_id)
        store.close_all_db_handles()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = SessionStore(home / 'sessions', config)
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._profile_name_for_source = lambda source: source.profile
        runner._delivery_adapter_for = lambda source: object()
        runner._run_in_executor_with_context = asyncio.to_thread
        entered = []
        real = GatewayRunner._profile_scope_for_source

        def counting(self, source):
            entered.append(source.profile)
            return real(self, source)

        monkeypatch.setattr(GatewayRunner, '_profile_scope_for_source', counting)
        await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == expected
        assert sorted(entered, key=str) == [None, 'work']
    finally:
        store.close_all_db_handles()
        if 'runner' in locals():
            runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_restore_warns_once_for_wakeup_row_with_no_route(tmp_path, monkeypatch, caplog):
    """A wakeup row whose session the routing index no longer lists (a /new replaced the
    route) can never fire through the restore scan; it must surface as one WARNING per row
    per process instead of sitting silent forever."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    from hermes_cli.wakeups import Wakeup, WakeupList

    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    dbs = {str(home): open_persistence(profile_scope(home=home))}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    config = GatewayConfig(multiplex_profiles=False)
    store = SessionStore(home / 'sessions', config)
    try:
        dbs[str(home)].state_meta.set('wakeups:ghost-session', WakeupList(pending=[
            Wakeup(id='wk_deadbeef', prompt='check', due_at=1.0, created_at=1.0)]).to_json())
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = config
        runner.session_store = store
        runner._heartbeat_watch = {}
        runner._start_heartbeat_poller = lambda: None
        runner._run_in_executor_with_context = asyncio.to_thread
        with caplog.at_level(logging.WARNING, logger='gateway.run'):
            await restore_heartbeat_watches(runner)
            assert any('ghost-session' in record.message and 'orphan' in record.message.lower()
                       for record in caplog.records)
            assert runner._heartbeat_watch == {}
            caplog.clear()
            await restore_heartbeat_watches(runner)
            assert not [record for record in caplog.records if 'ghost-session' in record.message]
    finally:
        store.close_all_db_handles()
        for db in dbs.values():
            db.close()
