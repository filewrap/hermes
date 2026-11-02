from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon import functions, types

from tests.plugins.platforms.telegram.test_forum_audit_transport import _NativeForum
from plugins.platforms.telegram.forum_audit_transport import ForumAuditTransport


_RAW_CHANNEL_ID = 4_346_203_856
_CANONICAL_RAW_CHANNEL_ID = -100_434_620_3856


def _private_forum(channel_id: int, **overrides):
    values = dict(
        id=channel_id,
        title="Private audit forum",
        photo=types.ChatPhotoEmpty(),
        date=None,
        forum=True,
        megagroup=True,
    )
    values.update(overrides)
    return types.Channel(**values)


def _owner_source(home, *, chat_type="dm", user_id="4346203856"):
    return SimpleNamespace(
        chat_id=user_id,
        user_id=user_id,
        chat_type=chat_type,
        is_bot=False,
        profile_route_rejected=False,
        _unused_home=home,
    )


def _authorize_owner(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_is_owner_user", lambda user_id, source: str(user_id) == "4346203856", raising=False)


def _request_types(native, request_type):
    return [request for request in native.requests if isinstance(request, request_type)]


def test_raw_owner_channel_id_is_normalized_only_after_live_channel_verification(tmp_path, monkeypatch):
    native = _NativeForum()
    native.entity = _private_forum(_RAW_CHANNEL_ID)
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    entity_lookups = []
    peer_lookups = []
    original_get_entity = native.get_entity
    original_peer = adapter._input_peer_for

    async def record_entity_lookup(chat_id):
        entity_lookups.append(chat_id)
        return await original_get_entity(chat_id)

    async def record_peer_lookup(chat_id):
        peer_lookups.append(chat_id)
        return await original_peer(chat_id)

    native.get_entity = record_entity_lookup
    adapter._input_peer_for = record_peer_lookup

    assert asyncio.run(controller.setup(_RAW_CHANNEL_ID)) is True
    assert entity_lookups == [types.PeerChannel(_RAW_CHANNEL_ID)]
    assert adapter.config.extra["forum_audit"]["chat_id"] == _CANONICAL_RAW_CHANNEL_ID
    created = _request_types(native, functions.messages.CreateForumTopicRequest)
    assert created and peer_lookups and set(peer_lookups) == {_CANONICAL_RAW_CHANNEL_ID}
    assert all(request.peer.channel_id for request in created)
    assert all(
        not isinstance(request, functions.messages.GetForumTopicsRequest)
        for request in native.requests
    )


@pytest.mark.parametrize(
    "entity_factory",
    [
        pytest.param(lambda: types.User(id=_RAW_CHANNEL_ID, first_name="not a channel"), id="user"),
        pytest.param(lambda: _private_forum(_RAW_CHANNEL_ID, username="publicforum"), id="public"),
        pytest.param(lambda: _private_forum(_RAW_CHANNEL_ID, forum=False), id="not-forum"),
        pytest.param(lambda: _private_forum(_RAW_CHANNEL_ID - 1), id="wrong-channel-id"),
    ],
)
def test_untrusted_raw_owner_id_rejections_leave_config_untouched(tmp_path, entity_factory):
    native = _NativeForum()
    native.entity = entity_factory()
    adapter = native.adapter(tmp_path)
    before_file = (tmp_path / "config.yaml").read_bytes()
    before_extra = dict(adapter.config.extra)
    controller = ForumAuditTransport(adapter, tmp_path)

    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        asyncio.run(controller.setup(_RAW_CHANNEL_ID))

    assert (tmp_path / "config.yaml").read_bytes() == before_file
    assert adapter.config.extra == before_extra
    assert not _request_types(native, functions.messages.CreateForumTopicRequest)


def test_canonical_channel_peer_id_remains_a_valid_setup_input(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)

    assert asyncio.run(controller.setup(-1000000000123)) is True
    assert adapter.config.extra["forum_audit"]["chat_id"] == -1000000000123
    assert _request_types(native, functions.messages.CreateForumTopicRequest)


def test_owner_private_command_accepts_raw_positive_channel_id(tmp_path, monkeypatch):
    native = _NativeForum()
    native.entity = _private_forum(_RAW_CHANNEL_ID)
    adapter = native.adapter(tmp_path)
    _authorize_owner(adapter, monkeypatch)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    source = _owner_source(tmp_path)

    assert asyncio.run(controller.command(source, f"setup {_RAW_CHANNEL_ID}")) is True
    assert adapter.config.extra["forum_audit"]["chat_id"] == _CANONICAL_RAW_CHANNEL_ID
    assert "verified and enabled" in native.notices[-1][1]


def test_unauthorized_group_command_does_not_probe_or_change_setup(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    source = _owner_source(tmp_path, chat_type="group")
    before_file = (tmp_path / "config.yaml").read_bytes()
    before_extra = dict(adapter.config.extra)

    assert asyncio.run(controller.command(source, "setup -1000000000123")) is True
    assert native.requests == []
    assert (tmp_path / "config.yaml").read_bytes() == before_file
    assert adapter.config.extra == before_extra
    assert "owner-only" in native.notices[-1][1]


@pytest.mark.parametrize("topic_state", ["closed", "hidden", "deleted"])
def test_closed_hidden_or_deleted_general_topic_rejects_setup_before_creation(tmp_path, topic_state):
    native = _NativeForum()
    if topic_state == "deleted":
        native.topics[0] = types.ForumTopicDeleted(id=1)
    else:
        setattr(native.topics[0], topic_state, True)
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    before_file = (tmp_path / "config.yaml").read_bytes()

    with pytest.raises(RuntimeError):
        asyncio.run(controller.setup(-1000000000123))

    assert not _request_types(native, functions.messages.CreateForumTopicRequest)
    assert (tmp_path / "config.yaml").read_bytes() == before_file


def test_topic_creation_lost_ack_retries_same_random_id_without_enumeration(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    original_request = adapter._mtproto_rich_request
    lose_first_ack = [True]

    async def request_with_lost_create_ack(request):
        result = await original_request(request)
        if isinstance(request, functions.messages.CreateForumTopicRequest) and lose_first_ack[0]:
            lose_first_ack[0] = False
            raise ConnectionError("server committed topic but acknowledgement was lost")
        return result

    adapter._mtproto_rich_request = request_with_lost_create_ack

    with pytest.raises(ConnectionError):
        asyncio.run(controller.setup(-1000000000123))
    assert asyncio.run(controller.setup(-1000000000123)) is True

    creates = _request_types(native, functions.messages.CreateForumTopicRequest)
    assert len(creates) == 2
    assert creates[0].random_id == creates[1].random_id
    assert len(native.created) == 1
    assert not _request_types(native, functions.messages.GetForumTopicsRequest)


def test_failed_by_id_verification_reuses_durable_operations_topic_on_retry(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    original_request = adapter._mtproto_rich_request
    fail_operation_lookup = [True]

    async def fail_first_operation_lookup(request):
        if (
            isinstance(request, functions.messages.GetForumTopicsByIDRequest)
            and request.topics != [1]
            and fail_operation_lookup[0]
        ):
            fail_operation_lookup[0] = False
            raise ConnectionError("temporary by-ID lookup failure")
        return await original_request(request)

    adapter._mtproto_rich_request = fail_first_operation_lookup
    with pytest.raises(ConnectionError):
        asyncio.run(controller.setup(-1000000000123))
    first_create = _request_types(native, functions.messages.CreateForumTopicRequest)[0]
    assert asyncio.run(controller.setup(-1000000000123)) is True

    creates = _request_types(native, functions.messages.CreateForumTopicRequest)
    assert len(creates) == 1
    assert creates[0].random_id == first_create.random_id
    assert any(
        isinstance(request, functions.messages.GetForumTopicsByIDRequest)
        and request.topics == [native.created[creates[0].random_id].id]
        for request in native.requests
    )


def test_operations_topic_id_and_user_topic_mapping_survive_restart_and_verification_failure(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    asyncio.run(controller.setup(-1000000000123))
    operations_topic = controller._service.operations_topic_id
    create_count_after_setup = len(_request_types(native, functions.messages.CreateForumTopicRequest))
    assert controller._service.set_user_topic(42, 777)

    original_request = adapter._mtproto_rich_request
    fail_first_operation_lookup = [True]

    async def fail_first_operation_verification(request):
        if (isinstance(request, functions.messages.GetForumTopicsByIDRequest)
                and request.topics != [1]
                and fail_first_operation_lookup[0]):
            fail_first_operation_lookup[0] = False
            raise ConnectionError("operations topic verification failed")
        return await original_request(request)

    adapter._mtproto_rich_request = fail_first_operation_verification
    restarted = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(restarted, "_start_worker", lambda: None)
    asyncio.run(restarted.connected())
    # A failed live verification disables the service but never replaces the
    # persisted Operations topic or the saved per-user mapping. The disabled
    # service points at a different storage root, so read the mapping from
    # the forum database directly.
    assert not restarted._service.enabled
    assert adapter.config.extra["forum_audit"]["operations_topic_id"] == operations_topic
    from plugins.platforms.telegram.forum_audit import _db
    with _db(controller._service._storage_root) as db:
        assert db.execute("SELECT topic_id FROM user_topics WHERE user_id=42").fetchone()[0] == 777

    adapter._mtproto_rich_request = original_request
    asyncio.run(restarted.connected())
    assert restarted._service.enabled
    assert restarted._service.operations_topic_id == operations_topic
    assert restarted._service.user_topic(42) == 777

    creates = _request_types(native, functions.messages.CreateForumTopicRequest)
    assert len(creates) == create_count_after_setup
    assert not _request_types(native, functions.messages.GetForumTopicsRequest)


def test_unconfirmed_topic_create_updates_are_not_enabled_and_retry_is_deduplicated(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    original_request = adapter._mtproto_rich_request
    return_unconfirmed = [True]

    async def unconfirmed_once(request):
        result = await original_request(request)
        if isinstance(request, functions.messages.CreateForumTopicRequest) and return_unconfirmed[0]:
            return_unconfirmed[0] = False
            return types.Updates(
                updates=[types.UpdateMessageID(id=999, random_id=request.random_id + 1)],
                users=[], chats=[], date=None, seq=0,
            )
        return result

    adapter._mtproto_rich_request = unconfirmed_once
    before_file = (tmp_path / "config.yaml").read_bytes()
    with pytest.raises(RuntimeError, match="confirm"):
        asyncio.run(controller.setup(-1000000000123))
    assert not controller._service.enabled
    assert (tmp_path / "config.yaml").read_bytes() == before_file

    assert asyncio.run(controller.setup(-1000000000123)) is True
    creates = _request_types(native, functions.messages.CreateForumTopicRequest)
    assert len(creates) == 2
    assert creates[0].random_id == creates[1].random_id
    assert len(native.created) == 1


def test_bot_method_invalid_command_has_specific_diagnostic_and_preserves_config(tmp_path, monkeypatch):
    from telethon.errors import BotMethodInvalidError
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    _authorize_owner(adapter, monkeypatch)
    controller = ForumAuditTransport(adapter, tmp_path)
    source = _owner_source(tmp_path)
    original_request = adapter._mtproto_rich_request

    async def reject_create(request):
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            raise BotMethodInvalidError(request)
        return await original_request(request)

    adapter._mtproto_rich_request = reject_create
    before_file = (tmp_path / "config.yaml").read_bytes()

    assert asyncio.run(controller.command(source, "setup -1000000000123")) is True
    diagnostic = native.notices[-1][1]
    assert "unsupported bot method" in diagnostic
    assert "BotMethodInvalidError" in diagnostic
    assert "could not verify" not in diagnostic
    assert "unchanged" in diagnostic
    assert (tmp_path / "config.yaml").read_bytes() == before_file


@pytest.mark.parametrize("fault", ["missing-map", "wrong-random", "wrong-peer", "wrong-title", "wrong-message-id", "conflicting-map"])
def test_unrelated_create_updates_cannot_confirm_setup(tmp_path, monkeypatch, fault):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    original = adapter._mtproto_rich_request

    async def unrelated_receipt(request):
        result = await original(request)
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            mapping, service = result.updates
            if fault == "missing-map":
                result.updates = [service]
            elif fault == "wrong-random":
                mapping.random_id += 1
            elif fault == "wrong-peer":
                service.message.peer_id = types.PeerChannel(999)
            elif fault == "wrong-title":
                service.message.action.title = "Unrelated"
            elif fault == "wrong-message-id":
                # Same peer and exact title still must match this request's mapped ID.
                service.message.id += 1
            else:
                result.updates.append(types.UpdateMessageID(id=mapping.id + 1, random_id=request.random_id))
        return result

    adapter._mtproto_rich_request = unrelated_receipt
    before = (tmp_path / "config.yaml").read_bytes()
    with pytest.raises(RuntimeError, match="confirm"):
        asyncio.run(controller.setup(-1000000000123))
    assert (tmp_path / "config.yaml").read_bytes() == before
    assert not controller._service.enabled


def test_raw_positive_saved_config_reuses_known_operations_id(tmp_path, monkeypatch):
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    native.topics.append(SimpleNamespace(id=22, title=f"Operations · Profile {controller.profile_id}"))
    adapter.config.extra["forum_audit"] = {"enabled": True, "chat_id": 123, "operations_topic_id": 22}
    asyncio.run(controller.connected())
    assert controller._service.enabled
    assert adapter.config.extra["forum_audit"]["chat_id"] == -1000000000123
    assert controller._service.operations_topic_id == 22
    assert not _request_types(native, functions.messages.CreateForumTopicRequest)


@pytest.mark.parametrize("mapping_kind", ["operations", "user"])
def test_invalid_persisted_topic_is_rejected_without_replacement(tmp_path, monkeypatch, mapping_kind):
    from plugins.platforms.telegram.forum_audit import _db
    native = _NativeForum()
    adapter = native.adapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    monkeypatch.setattr(controller, "_start_worker", lambda: None)
    asyncio.run(controller.setup(-1000000000123))
    creates_before = len(_request_types(native, functions.messages.CreateForumTopicRequest))
    before = (tmp_path / "config.yaml").read_bytes()
    if mapping_kind == "operations":
        # Valid numeric ID but not a live server topic: never silently replace it.
        adapter.config.extra["forum_audit"]["operations_topic_id"] = 999
        with _db(controller._service._storage_root) as db:
            db.execute("DELETE FROM forum_topics WHERE topic_key='operations'")
        with pytest.raises(RuntimeError):
            asyncio.run(controller.setup(-1000000000123))
    else:
        # Topic 1 is General: an invalid saved per-user mapping is rejected
        # without creating a replacement topic.
        with _db(controller._service._storage_root) as db:
            db.execute("INSERT INTO user_topics VALUES(42,1,0)")
        with pytest.raises(RuntimeError):
            controller._service.user_topic(42, strict=True)
        assert controller._service.user_topic(42) is None
    assert len(_request_types(native, functions.messages.CreateForumTopicRequest)) == creates_before
    assert (tmp_path / "config.yaml").read_bytes() == before
