"""Admission-generation checks for restricted tenant turns.

These tests intentionally exercise only the tenant runtime's lifecycle boundary;
they do not claim that the restricted agent worker is an OS security boundary.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import Principal
from gateway.tenant_runtime import TenantRuntime, TenantRuntimeDenied, room_key


class _Broker:
    def __init__(self, gate=None):
        self.gate = gate
        self.calls = 0

    async def for_tenant(self, **_kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return {"provider": "fake", "model": "fake-model"}


class _Run:
    def __init__(self, gate=None):
        self.gate = gate
        self.calls = 0

    async def __call__(self, **_kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return {"final_response": "private answer", "messages": [], "usage": {}}


class _Flush:
    def __init__(self, gate=None):
        self.gate = gate
        self.calls = 0

    async def __call__(self, **_kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()


def _setup(tmp_path, *, broker_gate=None, run_gate=None, flush=None):
    home = tmp_path / "home"
    home.mkdir()
    store = LocalTenantStore(home, owner_user_id="99", allowed_user_ids=["11", "22"])
    broker = _Broker(broker_gate)
    runner = _Run(run_gate)

    def factory(**_kwargs):
        return SimpleNamespace(system_message="", session_input_tokens=0, session_output_tokens=0)

    runtime = TenantRuntime(home, store, broker, factory, runner, flush)
    return home, store, broker, runner, runtime


def _source(user_id="11", chat_id=None, chat_type="dm", thread_id=None):
    return SimpleNamespace(
        user_id=str(user_id), chat_id=str(chat_id if chat_id is not None else user_id),
        chat_type=chat_type, thread_id=thread_id,
    )


def _event(text="hello", message_id=None):
    return SimpleNamespace(text=text, message_id=message_id, message=None)


def _owner():
    return Principal("99")


async def _denied(task):
    with pytest.raises(TenantRuntimeDenied):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["provider", "run_agent"])
async def test_revoke_then_regrant_during_await_discards_turn_without_history_or_completion(
    tmp_path, monkeypatch, phase
):
    gate = asyncio.Event()
    flush = _Flush()
    _home, store, broker, runner, runtime = _setup(
        tmp_path, broker_gate=gate if phase == "provider" else None,
        run_gate=gate if phase == "run_agent" else None, flush=flush,
    )
    accounted = []
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: accounted.append((args, kwargs))
    ))
    src = _source()
    task = asyncio.create_task(runtime.handle_message(None, _event(message_id="turn-1"), src))
    for _ in range(100):
        if (broker.calls if phase == "provider" else runner.calls):
            break
        await asyncio.sleep(0)
    assert broker.calls == 1
    if phase == "run_agent":
        assert runner.calls == 1

    store.allow_user(_owner(), "11", False)
    store.allow_user(_owner(), "11", True)
    gate.set()
    await _denied(task)

    principal = store.principal_for_scope("11", "dm", "11")
    assert store.get(principal, "profile", "history:" + room_key("11", "11")) is None
    receipt = store.get(principal, "profile", "turn:telegram:11:11:turn-1")
    assert not (isinstance(receipt, dict) and receipt.get("state") == "complete")
    assert accounted == []
    assert runner.calls == (0 if phase == "provider" else 1)
    assert flush.calls == 0


@pytest.mark.asyncio
async def test_ban_then_unban_during_run_agent_denies_old_turn(tmp_path):
    gate = asyncio.Event()
    _, store, _, runner, runtime = _setup(tmp_path, run_gate=gate)
    task = asyncio.create_task(runtime.handle_message(None, _event(), _source()))
    for _ in range(100):
        if runner.calls:
            break
        await asyncio.sleep(0)
    assert runner.calls == 1
    store.ban_user(_owner(), "11", True)
    store.ban_user(_owner(), "11", False)
    gate.set()
    await _denied(task)
    assert store.get(store.principal_for_scope("11", "dm", "11"), "profile", "history:" + room_key("11", "11")) is None


@pytest.mark.asyncio
async def test_queued_duplicate_cannot_replay_cached_answer_after_revoke_regrant(tmp_path, monkeypatch):
    # First persist a complete receipt, then queue its duplicate behind an in-flight
    # room turn. The duplicate's admission predates the revoke/regrant.
    _, store, broker, runner, runtime = _setup(tmp_path)
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: None
    ))
    src = _source()
    await runtime.handle_message(None, _event(message_id="cached"), src)
    holder_gate = asyncio.Event()
    runner.gate = holder_gate
    holder = asyncio.create_task(runtime.handle_message(None, _event("other"), src))
    for _ in range(100):
        if runner.calls >= 2:
            break
        await asyncio.sleep(0)
    assert runner.calls == 2

    duplicate = asyncio.create_task(runtime.handle_message(None, _event(message_id="cached"), src))
    await asyncio.sleep(0)
    store.allow_user(_owner(), "11", False)
    store.allow_user(_owner(), "11", True)
    holder_gate.set()
    await _denied(holder)
    await _denied(duplicate)
    # No provider resolution or replay response for the queued duplicate.
    assert broker.calls == 2


@pytest.mark.asyncio
async def test_guest_policy_off_on_invalidates_inflight_group_turn(tmp_path):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    gate = asyncio.Event()
    home, _store, broker, runner, runtime = _setup(tmp_path, broker_gate=gate)
    group = "-1004242"
    guest = _source("33", group, "group")
    update_chat_settings(home, "group", group, {"allow_all": True})
    task = asyncio.create_task(runtime.handle_message(None, _event(), guest))
    for _ in range(100):
        if broker.calls:
            break
        await asyncio.sleep(0)
    assert broker.calls == 1
    update_chat_settings(home, "group", group, {"allow_all": False})
    update_chat_settings(home, "group", group, {"allow_all": True})
    gate.set()
    await _denied(task)
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_guest_policy_update_does_not_downgrade_globally_allowed_member(tmp_path):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    gate = asyncio.Event()
    home, _store, broker, runner, runtime = _setup(tmp_path, broker_gate=gate)
    group = "-1004243"
    member = _source("11", group, "group")
    task = asyncio.create_task(runtime.handle_message(None, _event(), member))
    for _ in range(100):
        if broker.calls:
            break
        await asyncio.sleep(0)
    assert broker.calls == 1
    update_chat_settings(home, "group", group, {"allow_all": True})
    update_chat_settings(home, "group", group, {"allow_all": False})
    gate.set()
    result = await task
    assert result["final_response"] == "private answer"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_revoke_during_flush_denies_return_even_if_history_was_persisted(tmp_path):
    gate = asyncio.Event()
    flush = _Flush(gate)
    _, store, _, runner, runtime = _setup(tmp_path, flush=flush)
    task = asyncio.create_task(runtime.handle_message(None, _event(), _source()))
    for _ in range(100):
        if flush.calls:
            break
        await asyncio.sleep(0)
    assert flush.calls == 1
    store.allow_user(_owner(), "11", False)
    gate.set()
    await _denied(task)
    # History persistence can precede the awaited flush; the response is still denied.
    assert runner.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("user_id", "22"), ("chat_id", "22"), ("thread_id", "new-thread"),
])
async def test_source_identity_mutation_during_run_denies_old_result(tmp_path, field, value):
    gate = asyncio.Event()
    _, _store, _, runner, runtime = _setup(tmp_path, run_gate=gate)
    src = _source(thread_id="old-thread")
    task = asyncio.create_task(runtime.handle_message(None, _event(), src))
    for _ in range(100):
        if runner.calls:
            break
        await asyncio.sleep(0)
    assert runner.calls == 1
    setattr(src, field, value)
    gate.set()
    await _denied(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "missing"])
async def test_generation_api_failure_fails_closed_before_provider(tmp_path, failure):
    _, store, broker, runner, runtime = _setup(tmp_path)
    if failure == "raises":
        store.authorization_generation = lambda _uid: (_ for _ in ()).throw(RuntimeError("broken"))
    else:
        store.authorization_generation = None
    await _denied(runtime.handle_message(None, _event(), _source()))
    assert broker.calls == 0
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_same_message_id_in_distinct_threads_has_independent_history_and_receipt(tmp_path, monkeypatch):
    home, store, _, _, runtime = _setup(tmp_path)
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: None
    ))
    event = _event(message_id="thread-bound")
    await runtime.handle_message(None, event, _source(thread_id="thread-A"))
    await runtime.handle_message(None, event, _source(thread_id="thread-B"))
    principal = store.principal_for_scope("11", "dm", "11")
    thread_a = room_key("11", "11", "thread-A", profile=home)
    thread_b = room_key("11", "11", "thread-B", profile=home)
    assert thread_a != thread_b
    assert store.get(principal, "profile", "history:" + thread_a)
    assert store.get(principal, "profile", "history:" + thread_b)
    assert store.get(principal, "profile", f"turn:telegram:{thread_a}:thread-bound")
    assert store.get(principal, "profile", f"turn:telegram:{thread_b}:thread-bound")


@pytest.mark.asyncio
async def test_room_history_isolated_across_profile_user_chat_and_thread(tmp_path, monkeypatch):
    home_a, store, broker, runner, runtime = _setup(tmp_path)
    home_b = tmp_path / "other-profile"
    home_b.mkdir()
    runtime_b = TenantRuntime(home_b, store, broker, runtime.agent_factory, runner)
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: None
    ))
    identities = [
        (home_a, _source("11", "-1007", "forum", "71")),
        (home_a, _source("22", "-1007", "forum", "71")),
        (home_a, _source("11", "-1008", "forum", "71")),
        (home_a, _source("11", "-1007", "forum", "72")),
        (home_b, _source("11", "-1007", "forum", "71")),
    ]
    # Same-shaped turns across identities must each begin with a clean history.
    for index, (home, source) in enumerate(identities):
        target_runtime = runtime if home == home_a else runtime_b
        await target_runtime.handle_message(None, _event(f"turn-{index}"), source)
    keys = [room_key(src.user_id, src.chat_id, src.thread_id, profile=home)
            for home, src in identities]
    assert len(set(keys)) == len(identities)
    for (home, source), key in zip(identities, keys):
        principal = store.principal_for_scope(source.user_id, source.chat_type, source.chat_id)
        history = store.get(principal, "profile", "history:" + key)
        assert history and [item["content"] for item in history] == [
            next(f"turn-{i}" for i, identity in enumerate(identities) if identity == (home, source)),
            "private answer",
        ]


@pytest.mark.asyncio
async def test_group_enabled_off_on_change_invalidates_inflight_turn(tmp_path):
    gate = asyncio.Event()
    _, store, broker, runner, runtime = _setup(tmp_path, broker_gate=gate)
    group = "-1004244"
    # The group policy generation applies to both group guests and globally
    # allowed members, independently of the guest-only YAML policy stamp.
    task = asyncio.create_task(runtime.handle_message(
        None, _event(), _source("11", group, "group")
    ))
    for _ in range(100):
        if broker.calls:
            break
        await asyncio.sleep(0)
    assert broker.calls == 1
    store.set_group_settings(_owner(), group, {"enabled": False})
    store.set_group_settings(_owner(), group, {"enabled": True})
    gate.set()
    await _denied(task)
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_disabled_group_does_not_resolve_provider_or_leave_pending_receipt(tmp_path, monkeypatch):
    _, store, broker, runner, runtime = _setup(tmp_path)
    group = "-1009999"
    source = _source("11", group, "group")
    message = _event(message_id="disabled-message")
    store.set_group_settings(_owner(), group, {"enabled": False})
    for _ in range(2):
        result = await runtime.handle_message(None, message, source)
        assert "disabled" in result["final_response"]
    assert broker.calls == runner.calls == 0
    principal = store.principal_for_scope("11", "group", group)
    assert store.get(principal, "profile", f"turn:telegram:{group}:11:disabled-message") is None
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: None,
    ))
    store.set_group_settings(_owner(), group, {"enabled": True})
    assert (await runtime.handle_message(None, message, source))["final_response"] == "private answer"
    assert broker.calls == runner.calls == 1


@pytest.mark.asyncio
async def test_continuation_history_scope_does_not_pollute_main_tenant_chat(tmp_path, monkeypatch):
    import hashlib

    home, store, _broker, _runner, runtime = _setup(tmp_path)
    monkeypatch.setattr("agent.mongo_accounting.account_store", lambda _root: SimpleNamespace(
        record_turn=lambda *args, **kwargs: None
    ))
    source = _source()
    event = _event("/bg summarize this chat", message_id="background-1")
    event.metadata = {
        "tenant_command_prompt": "Summarize only this user's conversation.",
        "tenant_history_scope": "task-0123456789abcdef0123456789abcdef",
        "tenant_history_seed": [],
    }

    await runtime.handle_message(None, event, source)

    room = room_key("11", "11", profile=home)
    suffix = hashlib.sha256(event.metadata["tenant_history_scope"].encode()).hexdigest()[:24]
    background_history = store.get(store.principal_for_scope("11", "dm", "11"), "profile",
                                  "history:" + room + ":continuation:" + suffix)
    main_history = store.get(store.principal_for_scope("11", "dm", "11"), "profile", "history:" + room)
    assert background_history is not None
    assert [row["content"] for row in background_history if row["role"] == "user"] == [event.text]
    assert main_history is None

    await runtime.handle_message(None, _event("ordinary chat message", message_id="main-1"), source)
    main_history = store.get(store.principal_for_scope("11", "dm", "11"), "profile", "history:" + room)
    assert [row["content"] for row in main_history if row["role"] == "user"] == ["ordinary chat message"]
