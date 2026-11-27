from __future__ import annotations

import asyncio
import hashlib

from plugins.platforms.telegram.forum_audit import (
    ForumAuditLogger, enqueue_operation, status_for_profile, _db,
)


def configured(tmp_path, *, transport_id=None):
    return ForumAuditLogger(tmp_path, {
        "enabled": True,
        "chat_id": -1001234567890,
        "general_topic_id": 1,
        "operations_topic_id": 20,
        "verified_entity": True,
        "verified_forum": True,
        "verified_rights": True,
    }, transport_id=transport_id)


def test_inbound_is_never_queued(tmp_path):
    service = configured(tmp_path)
    assert not service.enqueue_inbound(user_id=123, source_chat_id=4, message_id=5,
                                       authorized=False)
    assert not service.enqueue_inbound(user_id=123, source_chat_id=4, message_id=5,
                                       authorized=True, guest=True)
    assert not service.enqueue_inbound(user_id=123, source_chat_id=-1001234567890,
                                       message_id=5, authorized=True, chat_type="dm")
    assert not service.enqueue_inbound(user_id=123, source_chat_id=4, message_id=5,
                                       authorized=True, chat_type="dm")
    with _db(service._storage_root) as db:
        assert db.execute("select count(*) from outbox").fetchone()[0] == 0


def test_uid_topic_mapping_and_outbox_are_profile_scoped(tmp_path):
    one = configured(tmp_path / "one")
    two = configured(tmp_path / "two")
    assert one.set_user_topic(123, 456)
    assert one.user_topic("123") == 456
    assert two.user_topic(123) is None
    assert one.enqueue_general("error notice", key="error:unit")
    with _db(one._storage_root) as db:
        assert db.execute("select count(*) from outbox").fetchone()[0] == 1
    with _db(two._storage_root) as db:
        assert db.execute("select count(*) from outbox").fetchone()[0] == 0


def test_failed_delivery_is_durably_deferred_and_floodwait_does_not_sleep(tmp_path):
    service = configured(tmp_path)
    assert service.enqueue_general("error notice", key="error:unit")
    async def fail(*args):
        raise type("FloodWaitError", (Exception,), {"seconds": 120})()
    assert asyncio.run(service.drain(send_text=fail)) == 0
    with _db(service._storage_root) as db:
        attempts, due = db.execute("select attempts,next_attempt from outbox").fetchone()
    assert attempts == 1
    assert due >= __import__("time").time() + 115


def test_operations_hook_only_enqueues_for_registered_enabled_profile(tmp_path, caplog):
    assert enqueue_operation(tmp_path / "unconfigured", "deny policy") is False
    service = configured(tmp_path)
    assert enqueue_operation(tmp_path, "denied user=123 tool=terminal") is True
    with _db(service._storage_root) as db:
        kind, topic, payload = db.execute("select kind,topic_id,payload from outbox").fetchone()
    assert (kind, topic, payload) == ("operations", 20, "denied user=123 tool=terminal")
    assert service.status()["pending"] == 1


def test_disabled_or_incomplete_config_fails_closed(tmp_path):
    service = ForumAuditLogger(tmp_path, {"enabled": True, "chat_id": -1001})
    assert not service.enabled
    assert service.status()["enabled"] is False
    assert not service.enqueue_general("start", key="startup")
    assert not service.enqueue_general("error", key="error:unit")


def test_general_notices_are_error_only(tmp_path):
    service = configured(tmp_path)
    assert not service.enqueue_general("gateway started", key="startup:unit")
    assert not service.enqueue_general("deploy finished", key="deploy:unit")
    assert service.enqueue_general("gateway error", key="error:unit")
    with _db(service._storage_root) as db:
        rows = db.execute("select kind,payload from outbox").fetchall()
    assert rows == [("general", "gateway error")]


def test_topic_routes_never_share_reserved_or_another_user_topic(tmp_path):
    service=configured(tmp_path)
    assert not service.set_user_topic(123,1)
    assert not service.set_user_topic(123,20)
    assert service.set_user_topic(123,777)
    assert not service.set_user_topic(456,777)
    assert service.user_topic(456) is None
    assert service.user_topic(123)==777


def test_full_floodwait_is_durable_and_stops_other_due_rows(tmp_path,monkeypatch):
    from plugins.platforms.telegram import forum_audit as audit
    monkeypatch.setattr(audit.time,'time',lambda:1000.)
    service=configured(tmp_path)
    assert service.enqueue_general('error one',key='error:one')
    assert service.enqueue_general('error two',key='error:two')
    calls=[]
    async def send(*args):
        calls.append(args)
        raise type('FloodWaitError',(Exception,),{'seconds':7200})()
    assert asyncio.run(service.drain(send_text=send))==0
    assert len(calls)==1
    restarted=configured(tmp_path)
    assert restarted.enqueue_general('error other',key='error:other-event')
    assert asyncio.run(restarted.drain(send_text=send))==0
    assert len(calls)==1
    with audit._db(restarted._storage_root) as db:
        assert db.execute("SELECT value FROM transport_state WHERE key='flood_until'").fetchone()[0]==8200.


def test_text_retry_keeps_its_durable_event_identity(tmp_path):
    service=configured(tmp_path)
    assert service.enqueue_general('error notice',key='error:unit')
    identities=[]
    async def send(chat,topic,text,event_key):
        identities.append(event_key)
        if len(identities)==1: raise ConnectionError('lost acknowledgement')
    asyncio.run(service.drain(send_text=send))
    from plugins.platforms.telegram import forum_audit as audit
    with _db(service._storage_root) as db:
        db.execute('UPDATE outbox SET next_attempt=0')
    asyncio.run(service.drain(send_text=send))
    assert identities[:2]==['general:error:unit','general:error:unit']


def test_copied_profile_never_reuses_old_user_topics_or_pending_messages(tmp_path):
    import shutil
    original=configured(tmp_path/'one')
    assert original.set_user_topic(123,777)
    assert original.enqueue_general('error notice',key='error:unit')
    shutil.copytree(original.root,tmp_path/'copied')
    copied=configured(tmp_path/'copied')
    assert copied.user_topic(123) is None
    assert copied.status()['pending']==0
    assert original.user_topic(123)==777
    assert original.status()['pending']==1


def test_destination_change_does_not_reroute_previous_outbox(tmp_path):
    original=configured(tmp_path)
    assert original.set_user_topic(123,777)
    assert original.enqueue_general('error notice',key='error:unit')
    changed=ForumAuditLogger(tmp_path,{**original.config,'chat_id':-1009999999999})
    assert changed.status()['pending']==0
    assert changed.user_topic(123) is None
    restored=configured(tmp_path)
    assert restored.status()['pending']==1 and restored.user_topic(123)==777


def test_retry_exhaustion_reports_abandonment_without_exception_values(tmp_path):
    from plugins.platforms.telegram import forum_audit as audit
    service=configured(tmp_path)
    assert service.enqueue_general('error notice',key='error:unit')
    with _db(service._storage_root) as db:
        db.execute('UPDATE outbox SET attempts=?',(audit._MAX_RETRIES-1,))
    async def fail(*args): raise RuntimeError('SECRET-CANARY')
    asyncio.run(service.drain(send_text=fail))
    with _db(service._storage_root) as db:
        rows=db.execute('SELECT kind,payload FROM outbox').fetchall()
    assert len(rows)==1 and rows[0][0]=='operations'
    assert 'abandoned' in rows[0][1] and 'SECRET-CANARY' not in rows[0][1]
    assert 'retry is scheduled' not in rows[0][1]
    assert not service.enqueue_inbound(user_id=123, source_chat_id=4, message_id=5, authorized=True, chat_type="dm")


def test_inbound_is_never_persisted(tmp_path):
    service = configured(tmp_path)
    assert not service.enqueue_inbound(user_id=123, source_chat_id=99, message_id=8,
                                       authorized=True, chat_type="dm")
    assert not service.enqueue_inbound(user_id=124, source_chat_id=99, message_id=9,
                                       authorized=True, chat_type="group")
    assert not service.enqueue_inbound(user_id=125, source_chat_id=99, message_id=10,
                                       authorized=True, chat_type="dm", thread_id="bad")
    assert not service.enqueue_inbound(user_id=126, source_chat_id=-10099, message_id=11,
                                       authorized=True, chat_type="dm")
    with _db(service._storage_root) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_cooldown_api_survives_restart(tmp_path):
    service = configured(tmp_path)
    assert service.flood_until() == 0
    service.defer_flood(123456.0)
    service.defer_flood(120000.0)  # cooldown cannot be shortened

    restarted = configured(tmp_path)
    assert restarted.flood_until() == 123456.0
    restarted.defer_flood(200000.0)
    assert service.flood_until() == 200000.0


def test_legacy_incoming_rows_fail_closed_for_unknown_scope(tmp_path):
    service = configured(tmp_path)
    with _db(service._storage_root) as db:
        db.execute("""INSERT INTO outbox(dedupe_key,kind,user_id,source_chat_id,
            source_message_id,chat_type,thread_id,payload,next_attempt,created_at)
            VALUES('legacy:1','incoming',123,4,5,'dm',NULL,'',0,0)""")
        db.execute("ALTER TABLE outbox DROP COLUMN thread_id")
        db.execute("ALTER TABLE outbox DROP COLUMN chat_type")

    # Reopening migrates the legacy schema, then eagerly purges the unverifiable
    # transcript without copying its content or metadata to Operations.
    with _db(service._storage_root) as db:
        assert db.execute("SELECT chat_type,thread_id FROM outbox").fetchone() is None
    async def forbidden(*args):
        raise AssertionError("legacy scope must not be resolved or forwarded")
    assert asyncio.run(service.drain(send_text=forbidden)) == 0
    with _db(service._storage_root) as db:
        assert db.execute("SELECT kind FROM outbox").fetchall() == []


def test_transport_partitions_isolate_same_profile_destination_state(tmp_path):
    first = configured(tmp_path, transport_id="telegram-adapter-one")
    second = configured(tmp_path, transport_id="telegram-adapter-two")
    assert first._storage_root != second._storage_root
    assert first._storage_root.name == hashlib.sha256(b"telegram-adapter-one").hexdigest()
    assert second._storage_root.name == hashlib.sha256(b"telegram-adapter-two").hexdigest()
    assert first.set_user_topic(123, 777)
    assert second.set_user_topic(123, 888)
    assert first.enqueue_general('error one',key='error:one')
    assert second.enqueue_general('error two',key='error:two')
    assert first.status()["pending"] == second.status()["pending"] == 1
    assert first.user_topic(123) == 777 and second.user_topic(123) == 888
    assert status_for_profile(tmp_path)["pending"] == 2


def test_registry_prefers_enabled_primary_and_falls_back_to_enabled_child(tmp_path):
    from plugins.platforms.telegram import forum_audit as audit
    primary = configured(tmp_path)
    child = configured(tmp_path, transport_id="child")
    disabled_sibling = ForumAuditLogger(tmp_path, {"enabled": False}, transport_id="disabled")
    assert audit._SERVICES[str(primary.root)] is primary
    assert status_for_profile(tmp_path)["enabled"] is True
    primary.enabled = False
    assert status_for_profile(tmp_path)["enabled"] is True
    assert audit._SERVICES[str(primary.root)] is child
    child.enabled = False
    assert status_for_profile(tmp_path)["enabled"] is False
    assert audit._SERVICES[str(primary.root)] is disabled_sibling


def test_drain_stops_if_disabled_mid_send(tmp_path):
    service = configured(tmp_path)
    assert service.enqueue_general('error notice',key='error:unit')
    calls = []

    async def send(*args):
        calls.append(args)
        service.enabled = False
        raise RuntimeError('boom')

    assert asyncio.run(service.drain(send_text=send)) == 0
    assert len(calls) == 1
    with _db(service._storage_root) as db:
        assert db.execute("SELECT kind FROM outbox").fetchall() == [("general",)]


def test_duplicate_services_share_drain_lock_and_send_once(tmp_path):
    first = configured(tmp_path)
    second = configured(tmp_path)
    assert first._storage_root == second._storage_root
    assert first._drain_lock is second._drain_lock
    assert first.enqueue_general("notice", key="error:shared")
    sends = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args):
        sends.append(args)
        entered.set()
        await release.wait()

    async def run_both():
        one = asyncio.create_task(first.drain(send_text=send))
        await entered.wait()
        two = asyncio.create_task(second.drain(send_text=send))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(one, two)

    assert asyncio.run(run_both()) == [1, 0]
    assert len(sends) == 1
