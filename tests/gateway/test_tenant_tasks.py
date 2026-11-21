from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import Principal
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.tenant_commands import handle_tenant_command
from gateway.tenant_runtime import _TransportIdentity, _authorization_stamp
from gateway.tenant_tasks import INDEX_KEY, TenantTaskScheduler, _task_index_key
from gateway.platforms.event import MessageEvent, MessageType


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((str(chat_id), text, metadata))
        return SimpleNamespace(success=True)


def _source(user_id="11", chat_id=None, chat_type="dm", thread_id=None):
    uid = str(user_id)
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(chat_id if chat_id is not None else uid),
        chat_type=chat_type,
        user_id=uid,
        user_name=f"User {uid}",
        thread_id=thread_id,
        profile="default",
    )


def _event(source, text):
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        user_id=source.user_id,
        user_name=source.user_name,
        source=source,
    )


def _store(tmp_path):
    home = tmp_path / "multiuser"
    home.mkdir(exist_ok=True)
    return LocalTenantStore(home, owner_user_id="99", allowed_user_ids=["11", "22"])


def _stamp(store, source):
    identity = _TransportIdentity.capture(source)
    return identity, _authorization_stamp(store, identity)


def _index_key(store):
    return _task_index_key(store.home)


@pytest.mark.asyncio
async def test_heartbeat_persists_per_user_and_survives_store_reopen(tmp_path):
    store = _store(tmp_path)
    adapter = _Adapter()
    source = _source("11")
    identity, stamp = _stamp(store, source)
    scheduler = TenantTaskScheduler(adapter, store.home, store)

    result = await scheduler.handle_command(
        _event(source, "/heartbeat every 1m Check my task list"), source,
        identity=identity, stamp=stamp, command="heartbeat", arguments="every 1m Check my task list",
    )

    assert "Heartbeat set" in result["final_response"]
    principal_11 = store.principal_for_scope("11", "dm", "11")
    principal_22 = store.principal_for_scope("22", "dm", "22")
    saved = store.get(principal_11, "profile", _index_key(store))
    assert saved["items"][0]["kind"] == "heartbeat"
    assert saved["items"][0]["status"] == "active"
    assert store.get(principal_22, "profile", _index_key(store)) is None

    reopened = LocalTenantStore(store.home, owner_user_id="99")
    recovered = reopened.get(reopened.principal_for_scope("11", "dm", "11"), "profile", _index_key(reopened))
    assert recovered == saved


@pytest.mark.asyncio
async def test_due_background_task_recovers_after_scheduler_recreation_and_only_sends_to_its_user(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    adapter = _Adapter()
    src = _source("11", "-100500", "group", "7")
    identity, stamp = _stamp(store, src)
    first_scheduler = TenantTaskScheduler(adapter, store.home, store)
    result = await first_scheduler.handle_command(
        _event(src, "/bg summarize this chat"), src,
        identity=identity, stamp=stamp, command="bg", arguments="summarize this chat",
    )
    assert "queued" in result["final_response"].lower()

    # Emulate a process restart: a new store and scheduler discover the same durable queue.
    reopened = LocalTenantStore(store.home, owner_user_id="99")
    second_scheduler = TenantTaskScheduler(adapter, store.home, reopened)
    run = AsyncMock(return_value={"final_response": "Tenant-only summary.", "messages": [], "usage": {}})
    monkeypatch.setattr("gateway.tenant_tasks.handle_tenant_message", run)

    assert await second_scheduler.poll_once() == 1
    await second_scheduler.wait_idle()

    run.assert_awaited_once()
    run_event = run.await_args.args[1]
    assert run_event.metadata["tenant_history_scope"].startswith("task-")
    assert adapter.sent == [("-100500", "Tenant-only summary.", {"thread_id": "7"})]
    principal = reopened.principal_for_scope("11", "group", "-100500")
    saved = reopened.get(principal, "profile", _index_key(reopened))
    assert saved["items"][0]["status"] == "completed"
    assert saved["items"][0]["run_count"] == 1


@pytest.mark.asyncio
async def test_two_users_cannot_read_or_cancel_each_others_tasks(tmp_path):
    store = _store(tmp_path)
    adapter = _Adapter()
    scheduler = TenantTaskScheduler(adapter, store.home, store)
    src_11 = _source("11")
    src_22 = _source("22")
    id_11, stamp_11 = _stamp(store, src_11)
    id_22, stamp_22 = _stamp(store, src_22)

    await scheduler.handle_command(_event(src_11, "/bg private task for 11"), src_11,
                                   identity=id_11, stamp=stamp_11, command="bg", arguments="private task for 11")
    status_22 = await scheduler.handle_command(_event(src_22, "/bg list"), src_22,
                                               identity=id_22, stamp=stamp_22, command="bg", arguments="list")
    assert "No active background tasks" in status_22["final_response"]
    assert store.get(store.principal_for_scope("22", "dm", "22"), "profile", _index_key(store)) is None
    own = store.get(store.principal_for_scope("11", "dm", "11"), "profile", _index_key(store))
    assert "private task for 11" in own["items"][0]["prompt"]


@pytest.mark.asyncio
async def test_group_policy_change_cancels_queued_loop_before_runtime_or_delivery(tmp_path, monkeypatch):
    store = _store(tmp_path)
    group = "-100600"
    owner = Principal("99")
    store.set_group_settings(owner, group, {"enabled": True})
    src = _source("11", group, "group")
    identity, stamp = _stamp(store, src)
    scheduler = TenantTaskScheduler(_Adapter(), store.home, store)
    result = await scheduler.handle_command(
        _event(src, "/loop Check the shared queue --times 2"), src,
        identity=identity, stamp=stamp, command="loop", arguments="Check the shared queue --times 2",
    )
    assert "Loop set" in result["final_response"]
    store.set_group_settings(owner, group, {"enabled": False})

    run = AsyncMock(side_effect=AssertionError("disabled group reached tenant runtime"))
    monkeypatch.setattr("gateway.tenant_tasks.handle_tenant_message", run)
    assert await scheduler.poll_once() == 1
    await scheduler.wait_idle()

    run.assert_not_awaited()
    assert scheduler.adapter.sent == []
    saved = store.get(store.principal_for_scope("11", "group", group), "profile", _index_key(store))
    assert saved["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_owner_security_command_cannot_write_allowlist_or_start_tenant_worker(tmp_path, monkeypatch):
    store = _store(tmp_path)
    src = _source("11")
    worker = AsyncMock(side_effect=AssertionError("owner security control reached worker"))
    monkeypatch.setattr("gateway.tenant_commands._run_isolated_worker", worker)

    result = await handle_tenant_command(
        _Adapter(), _event(src, "/allow 22"), src, store.home, store=store,
    )

    assert "Only the configured owner" in result["final_response"]
    worker.assert_not_awaited()
    assert store.get_access("22")["allowed"] is True


def test_task_storage_uses_tenant_cas_revisions(tmp_path):
    store = _store(tmp_path)
    principal = store.principal_for_scope("11", "dm", "11")
    payload, revision = store.get_versioned(principal, "profile", INDEX_KEY)
    assert payload is None and revision == 0
    store.put(principal, "profile", INDEX_KEY, {"schema": 1, "items": []}, expected_revision=revision)
    with pytest.raises(Exception, match="revision conflict"):
        store.put(principal, "profile", INDEX_KEY, {"schema": 1, "items": []}, expected_revision=revision)


@pytest.mark.asyncio
async def test_cancelled_background_task_is_not_delivered_or_resurrected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    adapter = _Adapter()
    src = _source("11")
    identity, stamp = _stamp(store, src)
    scheduler = TenantTaskScheduler(adapter, store.home, store)
    await scheduler.handle_command(_event(src, "/bg wait for cancellation"), src,
                                   identity=identity, stamp=stamp, command="bg", arguments="wait for cancellation")
    principal = store.principal_for_scope("11", "dm", "11")
    task_id = store.get(principal, "profile", _index_key(store))["items"][0]["id"]
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_turn(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {"final_response": "stale result", "messages": [], "usage": {}}

    monkeypatch.setattr("gateway.tenant_tasks.handle_tenant_message", blocking_turn)
    assert await scheduler.poll_once() == 1
    await started.wait()
    await scheduler.handle_command(_event(src, f"/bg cancel {task_id[:8]}"), src,
                                   identity=identity, stamp=stamp, command="bg",
                                   arguments=f"cancel {task_id[:8]}")
    release.set()
    await scheduler.wait_idle()

    assert adapter.sent == []
    saved = store.get(principal, "profile", _index_key(store))["items"][0]
    assert saved["status"] == "cancelled"


@pytest.mark.asyncio
async def test_goal_can_resume_after_reaching_its_tenant_run_cap(tmp_path):
    store = _store(tmp_path)
    src = _source("11")
    identity, stamp = _stamp(store, src)
    scheduler = TenantTaskScheduler(_Adapter(), store.home, store)
    await scheduler.handle_command(_event(src, "/goal finish the draft"), src,
                                   identity=identity, stamp=stamp, command="goal",
                                   arguments="finish the draft")
    principal = store.principal_for_scope("11", "dm", "11")
    index = store.get(principal, "profile", _index_key(store))
    index["items"][0].update(
        status="paused", run_count=10,
        last_error="Continuation run limit reached; resume to continue.",
    )
    store.put(principal, "profile", _index_key(store), index)

    result = await scheduler.handle_command(_event(src, "/goal resume"), src,
                                            identity=identity, stamp=stamp,
                                            command="goal", arguments="resume")

    saved = store.get(principal, "profile", _index_key(store))["items"][0]
    assert "resumed" in result["final_response"].lower()
    assert saved["status"] == "active"
    assert saved["run_count"] == 0
    assert saved["last_error"] == ""
