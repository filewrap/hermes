from __future__ import annotations

import asyncio
import logging
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_identity import RoutingIdentity
from plugins.platforms.telegram.forum_audit_transport import ForumAuditTransport
from tests.plugins.platforms.telegram.test_forum_audit_transport import _NativeForum, _Source, _message


FORUM_ID = -1000000000123


class _RoutedSource(_Source):
    def __init__(self, adapter, profile, home, *, user_id="42", chat_id="42",
                 chat_type="dm", runtime_profile=None, runtime_home=None):
        self.user_id = str(user_id)
        self.chat_id = str(chat_id)
        self.chat_type = chat_type
        self.profile = profile
        self._identity = RoutingIdentity(
            transport_profile='two' if Path(adapter.home).name == 'profile-two' else 'one',
            runtime_profile=runtime_profile or profile,
            authorization_home=Path(adapter.home),
            runtime_home=Path(runtime_home or home),
            multiplexed=True,
            transport=weakref.ref(adapter),
        )


def _profile_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")


def _record_count(controller, kind: str) -> int:
    from plugins.platforms.telegram.forum_audit import _db

    with _db(controller._service._storage_root) as db:
        return db.execute("SELECT count(*) FROM outbox WHERE kind=?", (kind,)).fetchone()[0]


def _make_profiles(tmp_path, monkeypatch):
    native = _NativeForum()
    home1, home2 = tmp_path / "profile-one", tmp_path / "profile-two"
    _profile_config(home1)
    _profile_config(home2)
    adapter = native.adapter(home1)
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _served_profile_homes={"one": home1, "two": home2},
        _primary_profile_name='one',
        _owning_profile=lambda candidate, platform: (
            candidate, 'two' if candidate.home == home2 else 'one'),
    )
    adapter.policy_allowed = True
    adapter.owner_allowed = True
    adapter.sent = []
    adapter._authorized = lambda source: adapter.policy_allowed
    adapter._is_explicitly_allowed = lambda source: True
    adapter._is_owner_user = lambda user_id, source: adapter.owner_allowed
    async def send(chat_id, text, **kwargs):
        adapter.sent.append((str(chat_id), text, kwargs))
        return SimpleNamespace(success=True)
    adapter.send = send

    root = ForumAuditTransport(adapter, home1)
    monkeypatch.setattr(root, "_start_worker", lambda: None)
    child = root._profile_controller(home2)
    assert child is not None
    monkeypatch.setattr(child, "_start_worker", lambda: None)

    async def setup_both():
        assert await root.setup(FORUM_ID)
        assert await child.setup(FORUM_ID)

    asyncio.run(setup_both())
    return native, adapter, root, child, home1, home2


def test_interleaved_same_and_distinct_users_keep_profile_outboxes_isolated(tmp_path, monkeypatch):
    from telethon import functions

    native, adapter, one, two, home1, home2 = _make_profiles(tmp_path, monkeypatch)
    messages = [_message(501, 81), _message(502, 82), _message(503, 83)]

    async def scenario():
        await adapter._process_inbound_event(
            {"source": _RoutedSource(adapter, "one", home1, user_id="42", chat_id="81")},
            [messages[0]],
        )
        await adapter._process_inbound_event(
            {"source": _RoutedSource(adapter, "two", home2, user_id="42", chat_id="82")},
            [messages[1]],
        )
        await adapter._process_inbound_event(
            {"source": _RoutedSource(adapter, "one", home1, user_id="43", chat_id="83")},
            [messages[2]],
        )
        # Inbound user messages are never queued for the audit forum, in
        # either profile, and no per-user topic is created for them.
        assert one._service.status()["pending"] == 0
        assert two._service.status()["pending"] == 0
        assert one._service.user_topic(42) is None
        assert two._service.user_topic(42) is None
        assert one._service.user_topic(43) is None
        # Error notices stay partitioned: each profile drains only its own
        # outbox, to General.
        assert one._service.enqueue_general("error one", key="error:one")
        assert two._service.enqueue_general("error two", key="error:two")
        assert one._service.status()["pending"] == 1
        assert two._service.status()["pending"] == 1
        assert await one._drain_once() == 1
        assert await two._drain_once() == 1

    asyncio.run(scenario())
    assert not [r for r in native.requests if isinstance(r, functions.messages.ForwardMessagesRequest)]
    sends = [r for r in native.requests if isinstance(r, functions.messages.SendMessageRequest)]
    assert len(sends) == 2
    assert {r.reply_to.reply_to_msg_id for r in sends} == {1}
    assert one._service._storage_root != two._service._storage_root
    assert adapter.original_calls == ["trigger-filter-and-routing"] * 3


def test_concurrent_topic_creation_is_deduplicated_within_profile(tmp_path, monkeypatch):
    from plugins.platforms.telegram.forum_audit_transport import _stable_id
    native, _adapter, one, _two, _home1, _home2 = _make_profiles(tmp_path, monkeypatch)

    async def scenario():
        return await asyncio.gather(*(one._ensure_topic(
            FORUM_ID, "probe", "Probe Topic",
            random_id=_stable_id("probe", FORUM_ID)) for _ in range(8)))

    ids = asyncio.run(scenario())
    assert len(set(ids)) == 1
    exact = [topic for topic in native.topics if topic.title == "Probe Topic"]
    assert len(exact) == 1


def test_new_incoming_events_after_service_restart_are_still_not_queued(tmp_path, monkeypatch):
    from telethon import functions

    native, adapter, one, _two, home1, _home2 = _make_profiles(tmp_path, monkeypatch)
    source = _RoutedSource(adapter, "one", home1, user_id="42", chat_id="81")

    async def scenario():
        await adapter._process_inbound_event({"source": source}, [_message(501, source.chat_id)])
        assert one._service.status()["pending"] == 0
        assert one._service.user_topic(42) is None
        assert await one._drain_once() == 0
        creates = [r for r in native.requests if isinstance(r, functions.messages.CreateForumTopicRequest)]
        one._new_service({
            "enabled": True, "chat_id": FORUM_ID, "general_topic_id": 1,
            "operations_topic_id": one._service.operations_topic_id,
            "verified_entity": True, "verified_forum": True, "verified_rights": True,
        }, runtime_verified=True)
        await adapter._process_inbound_event({"source": source}, [_message(502, source.chat_id)])
        assert one._service.status()["pending"] == 0
        assert one._service.user_topic(42) is None
        assert await one._drain_once() == 0
        assert [r for r in native.requests if isinstance(r, functions.messages.CreateForumTopicRequest)] == creates
        assert not [r for r in native.requests if isinstance(r, functions.messages.ForwardMessagesRequest)]
        # The restarted service still drains error notices.
        assert one._service.enqueue_general("error after restart", key="error:restart")
        assert await one._drain_once() == 1
        assert adapter.sent == []

    asyncio.run(scenario())


def test_commands_setup_status_and_off_are_owner_private_and_profile_scoped(tmp_path, monkeypatch):
    native, adapter, one, two, home1, home2 = _make_profiles(tmp_path, monkeypatch)
    one.disable()
    two.disable()
    adapter.sent.clear()
    scopes = []

    def observe_policy(source):
        from hermes_constants import get_hermes_home_override
        scopes.append(Path(get_hermes_home_override()).resolve())
        return True

    adapter._authorized = observe_policy
    source1 = _RoutedSource(adapter, "one", home1, chat_type="dm")
    source2 = _RoutedSource(adapter, "two", home2, chat_type="dm")

    async def scenario():
        assert await one.command(source1, "setup -1000000000123")
        assert await one.command(source1, "status")
        assert await two.command(source2, "status")
        adapter.owner_allowed = False
        assert await two.command(source2, "setup -1000000000123")
        adapter.owner_allowed = True
        assert await one.command(source1, "off")

    asyncio.run(scenario())
    assert scopes == [home1.resolve(), home1.resolve(), home2.resolve(), home2.resolve(), home1.resolve()]
    assert one._service.enabled is False
    assert two._service.enabled is False
    assert "Pending:" in next(text for _chat, text, _ in adapter.sent if "profile " + one.profile_id in text)
    assert any("owner-only" in text for _chat, text, _ in adapter.sent)
    assert any("disabled for this profile" in text for _chat, text, _ in adapter.sent)
    # Profile two's failed owner check must not have caused a setup request or config write.
    assert not (home2 / "platforms").exists()
    assert len([t for t in native.topics if t.title.startswith("Operations · Profile ")]) == 2


def test_legacy_incoming_row_is_dropped_by_drain_before_any_forward(tmp_path, monkeypatch, caplog):
    from telethon import functions
    from plugins.platforms.telegram.forum_audit import _db

    native, _adapter, one, _two, _home1, _home2 = _make_profiles(tmp_path, monkeypatch)
    with _db(one._service._storage_root) as db:
        db.execute("""INSERT INTO outbox(dedupe_key,kind,user_id,source_chat_id,
            source_message_id,chat_type,thread_id,payload,next_attempt,created_at)
            VALUES('legacy:601','incoming',42,42,601,'dm',NULL,'',0,0)""")
    assert one._service.status()["pending"] == 1

    assert asyncio.run(one._drain_once()) == 1
    assert one._service.status()["pending"] == 0
    assert not [r for r in native.requests if isinstance(r, functions.messages.ForwardMessagesRequest)]
    with _db(one._service._storage_root) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert "dropped a queued inbound message row" in caplog.text


def test_unresolved_or_mismatched_routing_identity_fails_closed(tmp_path, monkeypatch):
    _native, adapter, one, two, home1, home2 = _make_profiles(tmp_path, monkeypatch)

    async def scenario():
        for source in (
            _RoutedSource(adapter, "missing", home1, runtime_profile="missing"),
            _RoutedSource(adapter, "one", home1, runtime_profile="one", runtime_home=home2),
        ):
            await adapter._process_inbound_event({"source": source}, [_message(701, 42)])
        unresolved = _RoutedSource(adapter, "one", home1)
        unresolved._identity = None
        await adapter._process_inbound_event({"source": unresolved}, [_message(702, 42)])

    asyncio.run(scenario())
    assert one._service.status()["pending"] == 0
    assert two._service.status()["pending"] == 0
    assert adapter.original_calls == ["trigger-filter-and-routing"] * 3


def test_operations_warnings_are_partitioned_to_correct_profile_topics(tmp_path, monkeypatch):
    _native, _adapter, one, two, home1, home2 = _make_profiles(tmp_path, monkeypatch)
    logger = logging.getLogger("plugins.platforms.telegram.telethon_adapter")
    for home in (home1, home2):
        record = logging.LogRecord(logger.name, logging.WARNING, __file__, 1,
                                   "redacted operations test", (), None)
        record.hermes_home = str(home)
        logger.handle(record)

    from plugins.platforms.telegram.forum_audit import _db
    with _db(one._service._storage_root) as db:
        one_rows = db.execute("SELECT topic_id,payload FROM outbox WHERE kind='operations'").fetchall()
    with _db(two._service._storage_root) as db:
        two_rows = db.execute("SELECT topic_id,payload FROM outbox WHERE kind='operations'").fetchall()
    assert [row[0] for row in one_rows] == [one._service.operations_topic_id]
    assert [row[0] for row in two_rows] == [two._service.operations_topic_id]
    assert one._service.operations_topic_id != two._service.operations_topic_id
    assert all("redacted operations test" not in row[1] for row in one_rows + two_rows)


def test_flood_wait_blocks_shared_transport_and_is_durable_for_new_profile_controller(tmp_path, monkeypatch):
    native, adapter, one, two, _home1, home2 = _make_profiles(tmp_path, monkeypatch)

    class FloodWaitError(Exception):
        seconds = 90

    async def flood(_request):
        raise FloodWaitError("flood wait")

    adapter._mtproto_rich_request = flood

    async def scenario():
        with pytest.raises(FloodWaitError):
            await one._audit_request(SimpleNamespace())
        shared_until = time.time() + 80
        assert one._service.flood_until() >= shared_until
        assert two._service.flood_until() >= shared_until
        before = len(native.requests)
        with pytest.raises(RuntimeError, match="cooldown"):
            await two._audit_request(SimpleNamespace())
        assert len(native.requests) == before

        # A fresh controller/service for the same served profile reads the durable
        # cooldown rather than relying on the original manager's in-memory value.
        fresh = ForumAuditTransport(adapter, home2, parent=one)
        monkeypatch.setattr(fresh, "_start_worker", lambda: None)
        fresh._new_service({
            "enabled": True, "chat_id": FORUM_ID, "general_topic_id": 1,
            "operations_topic_id": two._service.operations_topic_id,
            "verified_entity": True, "verified_forum": True, "verified_rights": True,
        }, runtime_verified=True)
        one._profiles[home2.resolve()] = fresh
        assert fresh._service.flood_until() >= shared_until
        with pytest.raises(RuntimeError, match="cooldown"):
            await fresh._audit_request(SimpleNamespace())

    asyncio.run(scenario())
    # The fake's network request handler was replaced only for the forced wait;
    # all recorded setup traffic remained in the local in-memory fixture.
    assert native.requests


def test_removed_profile_is_revoked_without_sending_or_recreating_its_home(tmp_path, monkeypatch):
    from telethon import functions
    from plugins.platforms.telegram.forum_audit import _db

    native, adapter, one, two, _home1, home2 = _make_profiles(tmp_path, monkeypatch)
    with _db(two._service._storage_root) as db:
        db.execute("""INSERT INTO outbox(dedupe_key,kind,user_id,source_chat_id,
            source_message_id,chat_type,thread_id,payload,next_attempt,created_at)
            VALUES('legacy:801','incoming',42,82,801,'dm',NULL,'',0,0)""")
    assert two._service.status()["pending"] == 1
    # Simulate the runner having deleted the served-profile registry before teardown.
    adapter.gateway_runner._served_profile_homes.pop("two")
    import shutil
    shutil.rmtree(home2)
    sent_before = len([r for r in native.requests if isinstance(r, functions.messages.SendMessageRequest)])

    async def scenario():
        await one.revoke_profile(home2)
        assert one._profile_controller(home2) is None
        assert not two._service.enqueue_inbound(user_id=42, source_chat_id=82,
                                                message_id=802, authorized=True, chat_type="dm")
        assert await two._drain_once() == 0

    asyncio.run(scenario())
    sent_after = len([r for r in native.requests if isinstance(r, functions.messages.SendMessageRequest)])
    assert sent_after == sent_before
    assert two._revoked is True
    assert not home2.exists()  # Revoked enqueue/drain cannot recreate the deleted profile home.


def test_dm_events_are_never_queued_regardless_of_policy_or_identity(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home_override
    _native, adapter, one, two, home1, home2 = _make_profiles(tmp_path, monkeypatch)
    adapter._authorized = lambda source: Path(get_hermes_home_override()).resolve() == home1.resolve()
    adapter._is_explicitly_allowed = lambda source: str(source.user_id) == '42'

    async def scenario():
        for profile, home, uid in [('one', home1, '42'), ('two', home2, '42'),
                                   ('one', home1, '43'), ('one', home1, 'not-numeric')]:
            await adapter._process_inbound_event(
                {'source': _RoutedSource(adapter, profile, home, user_id=uid,
                                         chat_id='42', chat_type='dm')},
                [_message(901, 42)],
            )
        assert one._service.status()['pending'] == 0
        assert two._service.status()['pending'] == 0
        assert await one._drain_once() == 0
        assert await two._drain_once() == 0

    asyncio.run(scenario())
    assert adapter.original_calls == ['trigger-filter-and-routing'] * 4


def test_text_delivery_failure_is_sent_only_to_failed_profiles_operations(tmp_path, monkeypatch):
    from telethon import functions
    native, _adapter, one, two, _home1, _home2 = _make_profiles(tmp_path, monkeypatch)
    assert two._service.enqueue_general("error two", key="error:two")
    original_send = two._send_text
    failed_once = [True]

    async def fail_first_send(*args, **kwargs):
        if failed_once[0]:
            failed_once[0] = False
            raise RuntimeError('SECRET-USER-CONTENT-CANARY')
        return await original_send(*args, **kwargs)

    two._send_text = fail_first_send

    async def scenario():
        assert await two._drain_once() == 0
        assert _record_count(two, 'operations') == 1
        assert _record_count(one, 'operations') == 0
        assert await two._drain_once() == 1

    asyncio.run(scenario())
    errors = [request for request in native.requests
              if isinstance(request, functions.messages.SendMessageRequest)
              and 'delivery failure' in request.message]
    assert len(errors) == 1
    assert errors[0].reply_to.reply_to_msg_id == two._service.operations_topic_id
    assert errors[0].reply_to.reply_to_msg_id not in {1, one._service.operations_topic_id}
    assert 'CANARY' not in errors[0].message


def test_audit_forum_is_suppressed_even_after_logging_is_disabled(tmp_path, monkeypatch):
    _native, adapter, one, two, home1, _home2 = _make_profiles(tmp_path, monkeypatch)
    two.disable()
    before = len(adapter.original_calls)
    asyncio.run(adapter._process_inbound_event(
        {'source': _RoutedSource(adapter, 'one', home1, chat_id=str(FORUM_ID))},
        [SimpleNamespace(id=903, peer_id=None)],
    ))
    assert len(adapter.original_calls) == before
    assert one._service.status()['pending'] == 0


def test_two_adapters_share_operations_topic_but_not_transport_outboxes(tmp_path, monkeypatch):
    from telethon import functions
    from plugins.platforms.telegram.forum_audit import enqueue_operation, status_for_profile
    native, adapter_a, _root_a, child_a, home1, home2 = _make_profiles(tmp_path, monkeypatch)
    adapter_b = native.adapter(home2)
    adapter_b.gateway_runner = adapter_a.gateway_runner
    root_b = ForumAuditTransport(adapter_b, home2)
    monkeypatch.setattr(root_b, '_start_worker', lambda: None)

    async def scenario():
        await root_b.setup(FORUM_ID)
        assert child_a._service.enabled and root_b._service.enabled
        assert child_a._service._storage_root != root_b._service._storage_root
        assert child_a._service.operations_topic_id == root_b._service.operations_topic_id
        assert child_a._service.enqueue_general("error a", key="error:a")
        assert root_b._service.enqueue_general("error b", key="error:b")
        assert status_for_profile(home2)['pending'] == 2
        assert await root_b._drain_once() == 1
        assert child_a._service.status()['pending'] == 1
        assert await child_a._drain_once() == 1
        assert child_a._service.user_topic(42) is None
        assert root_b._service.user_topic(42) is None
        # A disabled extra adapter cannot steal the live profile Operations service.
        fresh = ForumAuditTransport(native.adapter(home1), home1)
        fresh.adapter.gateway_runner = adapter_a.gateway_runner
        extra = fresh._profile_controller(home2)
        assert extra is not None and not extra._service.enabled
        assert enqueue_operation(home2, 'safe profile operation')
        assert _record_count(root_b, 'operations') == 1

    asyncio.run(scenario())
    assert not [r for r in native.requests if isinstance(r, functions.messages.ForwardMessagesRequest)]
    sends = [r for r in native.requests if isinstance(r, functions.messages.SendMessageRequest)]
    assert len(sends) == 2
    assert {r.reply_to.reply_to_msg_id for r in sends} == {1}
    assert len([topic for topic in native.topics
                if topic.title == f'Operations · Profile {child_a.profile_id}']) == 1


def test_restart_honors_durable_cooldown_before_any_forum_probe(tmp_path, monkeypatch):
    native, adapter, one, _two, home1, _home2 = _make_profiles(tmp_path, monkeypatch)
    one._service.defer_flood(time.time() + 90)
    fresh_adapter = native.adapter(home1, {'forum_audit': one._raw_config()})
    fresh_adapter.gateway_runner = adapter.gateway_runner
    fresh = ForumAuditTransport(fresh_adapter, home1)
    monkeypatch.setattr(fresh, '_start_worker', lambda: None)
    probes = []

    async def forbidden_probe(chat_id):
        probes.append(chat_id)
        raise AssertionError('persisted FloodWait must block even live verification')

    monkeypatch.setattr(native, 'get_entity', forbidden_probe)
    asyncio.run(fresh.connected())
    assert probes == []
    assert not fresh._service.enabled
    assert fresh._flood_until > time.time() + 80


def test_revoke_detaches_cancellation_resistant_worker_within_bound(tmp_path, monkeypatch):
    _native, adapter, one, two, _home1, home2 = _make_profiles(tmp_path, monkeypatch)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        outcomes = []

        async def resistant():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            try:
                await two._send_text(FORUM_ID, two._service.operations_topic_id, 'must not send', 'revoked')
            except RuntimeError:
                outcomes.append('revoked')

        task = asyncio.create_task(resistant())
        two._task = task
        await started.wait()
        adapter.gateway_runner._served_profile_homes.pop('two')
        before = time.monotonic()
        await one.revoke_profile(home2)
        assert time.monotonic() - before < 4
        release.set()
        await task
        assert outcomes == ['revoked']

    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['setup', 'reconnect'])
def test_inflight_verification_cannot_resurrect_revoked_profile(tmp_path, monkeypatch, operation):
    import shutil
    from plugins.platforms.telegram.forum_audit import enqueue_operation
    _native, adapter, one, two, _home1, home2 = _make_profiles(tmp_path, monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        operations_topic = two._service.operations_topic_id

        async def paused_verification(chat_id):
            entered.set()
            await release.wait()
            return chat_id, 1, operations_topic

        monkeypatch.setattr(two, '_verify_and_prepare', paused_verification)
        pending = asyncio.create_task(two.setup(FORUM_ID) if operation == 'setup' else two._connect_profile())
        await entered.wait()
        adapter.gateway_runner._served_profile_homes.pop('two')
        await one.revoke_profile(home2)
        shutil.rmtree(home2)
        release.set()
        if operation == 'setup':
            with pytest.raises(RuntimeError, match='lifecycle changed'):
                await pending
        else:
            await pending  # Reconnect reports failed verification locally and stays disabled.
        assert not two._service.enabled
        assert two._operations_handler is None
        assert two._task is None
        assert not enqueue_operation(home2, 'must not recreate removed profile')
        assert not home2.exists()

    asyncio.run(scenario())
