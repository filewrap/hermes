from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.forum_audit_transport import (
    ForumAuditTransport,
    _signed_chat_id,
    _text_chunks,
)
from datetime import UTC


class _Source:
    chat_id = "42"
    user_id = "42"
    chat_type = "dm"
    is_bot = False
    profile_route_rejected = False


def _message(message_id, chat_id):
    from telethon import types
    return SimpleNamespace(id=message_id, peer_id=types.PeerUser(int(chat_id)))


class _FakeAdapter:
    def __init__(self, home, extra=None):
        self.config = SimpleNamespace(extra=extra or {})
        self._client = None
        self.home = Path(home)
        self.original_calls = []
        self.authorized = True
        self.explicit = True

    async def connect(self, *args, **kwargs):
        return True

    async def disconnect(self):
        return None

    async def _process_inbound_event(self, event, messages):
        self.original_calls.append("trigger-filter-and-routing")

    async def _source(self, event):
        return event["source"]

    def _authorized(self, source):
        return self.authorized

    def _is_explicitly_allowed(self, source):
        return self.explicit

    def _settings_home(self, source):
        return self.home


@pytest.fixture
def event_authorized():
    return [True]


@pytest.fixture
def event_explicit():
    return [True]


class _Recorder:
    enabled = True
    forum_id = -1000000000123

    def __init__(self):
        self.queued = []

    def enqueue_inbound(self, **kwargs):
        self.queued.append(kwargs)

    def status(self):
        return {"enabled": True, "pending": 0, "mapped_users": 0}


def test_inbound_is_never_queued_and_still_reaches_original_routing(tmp_path, event_authorized, event_explicit):
    adapter = _FakeAdapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    recorder = _Recorder()
    controller._service = recorder
    source = _Source()
    messages = [_message(101, source.chat_id), _message(102, source.chat_id)]
    asyncio.run(adapter._process_inbound_event({"source": source}, messages))
    assert recorder.queued == []
    assert adapter.original_calls == ["trigger-filter-and-routing"]

    recorder.queued.clear()
    adapter.authorized = False
    asyncio.run(adapter._process_inbound_event({"source": source}, messages))
    assert recorder.queued == []
    adapter.authorized = True
    adapter.explicit = False
    asyncio.run(adapter._process_inbound_event({"source": source}, messages))
    assert recorder.queued == []
    assert adapter.original_calls == ["trigger-filter-and-routing"] * 3


def test_setup_ids_require_signed_numeric_private_chat_id():
    assert _signed_chat_id("-1001234567890") == -1001234567890
    assert _signed_chat_id("@publicgroup") is None
    assert _signed_chat_id("1234") is None
    assert _signed_chat_id("-0") is None
    assert _signed_chat_id(True) is None


def test_text_chunking_respects_utf16_boundaries_and_limit():
    chunks = _text_chunks("a" * 2999 + "😀" + "b", 3000)
    assert [len(chunk.encode("utf-16-le")) // 2 for chunk in chunks] == [2999, 3]
    assert "".join(chunks) == "a" * 2999 + "😀b"


def test_forum_validation_requires_private_forum_and_current_bot_rights(tmp_path):
    from datetime import datetime, timezone
    from telethon import types
    from telethon.tl.custom import ParticipantPermissions

    def rights(manage):
        return ParticipantPermissions(types.ChannelParticipantAdmin(
            user_id=9, promoted_by=1, date=datetime.now(UTC),
            admin_rights=types.ChatAdminRights(manage_topics=manage)), chat=False)

    adapter = _FakeAdapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)

    class Client:
        entity = types.Channel(id=123, title="Private", photo=types.ChatPhotoEmpty(), date=None, forum=True, megagroup=True)
        me = SimpleNamespace(id=9, bot=True)

        def __init__(self):
            self.rights = rights(True)

        async def get_entity(self, chat_id):
            assert chat_id == -1000000000123
            return self.entity

        async def get_me(self):
            return self.me

        async def get_permissions(self, entity, me):
            assert entity is self.entity and me is self.me
            return self.rights

    adapter._client = Client()
    assert asyncio.run(controller._verify_forum(-1000000000123))[0] is adapter._client.entity
    adapter._client.entity = types.Channel(id=123, title="Public", photo=types.ChatPhotoEmpty(), date=None, username="public", forum=True, megagroup=True)
    with pytest.raises(ValueError):
        asyncio.run(controller._verify_forum(-1000000000123))
    adapter._client.entity = types.Channel(id=123, title="Private", photo=types.ChatPhotoEmpty(), date=None, forum=True, megagroup=True)
    adapter._client.rights = rights(False)
    with pytest.raises(PermissionError):
        asyncio.run(controller._verify_forum(-1000000000123))


def test_send_text_request_has_topic_anchor_and_restart_stable_random_id(tmp_path):
    pytest.importorskip("telethon")
    adapter = _FakeAdapter(tmp_path)
    peer_calls, requests = [], []

    async def input_peer(chat_id):
        from telethon import types, utils
        peer_calls.append(chat_id)
        if int(chat_id) > 0:
            return types.InputPeerUser(int(chat_id), 1)
        channel_id = utils.resolve_id(int(chat_id))[0]
        return types.InputPeerChannel(channel_id, 1)

    async def request(value):
        requests.append(value)
        return "ok"

    adapter._input_peer_for = input_peer
    adapter._mtproto_rich_request = request
    controller = ForumAuditTransport(adapter, tmp_path)
    controller._new_service({'enabled':True,'chat_id':-1000000000123,'general_topic_id':1,
        'operations_topic_id':2,'verified_entity':True,'verified_forum':True,
        'verified_rights':True},runtime_verified=True)
    first = asyncio.run(controller._send_text(-1000000000123, 44, "hello", "event-key"))
    first_request = requests[-1]
    second = asyncio.run(controller._send_text(-1000000000123, 44, "hello", "event-key"))
    second_request = requests[-1]
    assert first == second == "ok"
    assert type(first_request).__name__ == "SendMessageRequest"
    assert first_request.message == "hello"
    assert first_request.reply_to.reply_to_msg_id == 44
    assert first_request.reply_to.top_msg_id == 44
    assert first_request.random_id == second_request.random_id
    assert peer_calls == [-1000000000123, -1000000000123]


class _NativeForum:
    """Pinned native request objects, with a tiny in-memory Telegram server."""
    def __init__(self):
        from datetime import datetime, timezone
        from telethon import types
        from telethon.tl.custom import ParticipantPermissions
        self.entity=types.Channel(id=123,title="Private",photo=types.ChatPhotoEmpty(),date=None,forum=True,megagroup=True)
        self.me=SimpleNamespace(id=9,bot=True)
        self.permissions=ParticipantPermissions(types.ChannelParticipantAdmin(
            user_id=9,promoted_by=1,date=datetime.now(UTC),
            admin_rights=types.ChatAdminRights(manage_topics=True)),chat=False)
        self.topics=[SimpleNamespace(id=1,title='Renamed default topic',closed=False,hidden=False)]
        self.requests=[]
        self.notices=[]
        self.created={}

    async def get_entity(self,chat_id): return self.entity
    async def get_me(self): return self.me
    async def get_permissions(self,entity,me): return self.permissions

    async def peer(self,chat_id):
        from telethon import types, utils
        channel_id = utils.resolve_id(int(chat_id))[0] if abs(int(chat_id)) >= 10**12 else abs(int(chat_id))
        return types.InputPeerUser(int(chat_id),1) if int(chat_id)>0 else types.InputPeerChannel(channel_id,1)

    async def request(self,request):
        from datetime import datetime,timezone
        from telethon import functions,types
        self.requests.append(request)
        if isinstance(request,functions.messages.GetForumTopicsByIDRequest):
            return SimpleNamespace(topics=[topic for topic in self.topics if topic.id in request.topics])
        if isinstance(request,functions.messages.GetForumTopicsRequest):
            raise AssertionError("Bots cannot enumerate forum topics")
        if isinstance(request,functions.messages.CreateForumTopicRequest):
            topic=self.created.get(request.random_id)
            if topic is None:
                topic=SimpleNamespace(id=max(item.id for item in self.topics)+1,title=request.title)
                self.topics.append(topic)
                self.created[request.random_id]=topic
            message=types.MessageService(id=topic.id,peer_id=types.PeerChannel(request.peer.channel_id),
                action=types.MessageActionTopicCreate(title=request.title,icon_color=0))
            return types.Updates(updates=[types.UpdateMessageID(id=topic.id,random_id=request.random_id),
                types.UpdateNewChannelMessage(message=message,pts=1,pts_count=1)],
                users=[],chats=[],date=datetime.now(UTC),seq=0)
        if isinstance(request,(functions.messages.SendMessageRequest,functions.messages.ForwardMessagesRequest)):
            return SimpleNamespace(id=len(self.requests)+100)
        raise AssertionError(type(request).__name__)

    def adapter(self,home,config=None):
        import json
        home.mkdir(exist_ok=True)
        if not (home/'config.yaml').exists(): (home/'config.yaml').write_text(json.dumps({}))
        adapter=_FakeAdapter(home,extra=config)
        adapter._client=self
        adapter._input_peer_for=self.peer
        adapter._mtproto_rich_request=self.request
        async def send(chat,text,**kwargs):
            self.notices.append((chat,text))
            return SimpleNamespace(success=True)
        adapter.send=send
        return adapter


def test_inbound_dm_is_never_queued_or_forwarded_after_setup(tmp_path,monkeypatch):
    from telethon import functions
    native=_NativeForum()
    adapter=native.adapter(tmp_path)
    controller=ForumAuditTransport(adapter,tmp_path)
    started=[]
    monkeypatch.setattr(controller,'_start_worker',lambda:started.append(True))
    async def scenario():
        assert await controller.setup(-1000000000123)
        assert controller._service.enabled
        assert adapter.config.extra['forum_audit']['enabled']
        source=_Source()
        await adapter._process_inbound_event({'source':source},[_message(101, source.chat_id)])
        assert controller._service.status()['pending']==0
        assert controller._service.user_topic(42) is None
        # Replayed inbound after a transport restart is still never queued.
        assert not controller._service.enqueue_inbound(user_id=42,source_chat_id=42,
            message_id=101,authorized=True,chat_type="dm")
        assert await controller._service.drain(send_text=controller._send_text)==0
        assert not [request for request in native.requests
                    if isinstance(request,(functions.messages.SendMessageRequest,
                                          functions.messages.ForwardMessagesRequest))]
        assert native.notices == []
        # Audit-forum traffic is suppressed before the normal trigger/model path.
        source.chat_id='-1000000000123'
        original=len(adapter.original_calls)
        await adapter._process_inbound_event({'source':source},[_message(102, source.chat_id)])
        assert len(adapter.original_calls)==original
        await controller.disconnected()
    asyncio.run(scenario())
    assert started==[True]


def test_persisted_verified_bits_cannot_enable_logger_without_real_rights(tmp_path,monkeypatch):
    from telethon import types
    from telethon.tl.custom import ParticipantPermissions
    native=_NativeForum()
    native.permissions=ParticipantPermissions(types.ChannelParticipant(user_id=9,date=None),chat=False)
    adapter=native.adapter(tmp_path,{'forum_audit':{'enabled':True,'chat_id':-1000000000123,
        'general_topic_id':1,'operations_topic_id':2,'verified_entity':True,
        'verified_forum':True,'verified_rights':True}})
    controller=ForumAuditTransport(adapter,tmp_path)
    assert not controller._service.enabled
    monkeypatch.setattr(controller,'_start_worker',lambda:pytest.fail('must not start worker'))
    asyncio.run(controller.connected())
    assert not controller._service.enabled
    assert native.requests==[]


def test_same_uid_in_two_profiles_uses_distinct_operations_topics_and_topic_storage(tmp_path,monkeypatch):
    native=_NativeForum()
    controllers=[ForumAuditTransport(native.adapter(tmp_path/name),tmp_path/name) for name in ('one','two')]
    for controller in controllers: monkeypatch.setattr(controller,'_start_worker',lambda:None)
    async def scenario():
        for controller in controllers:
            await controller.setup(-1000000000123)
    asyncio.run(scenario())
    one, two = controllers
    assert one._service.enabled and two._service.enabled
    assert one._service.operations_topic_id != two._service.operations_topic_id
    assert one._service._storage_root != two._service._storage_root
    assert one._service.user_topic(42) is None and two._service.user_topic(42) is None
    # Per-user topic storage stays profile-scoped: a mapping saved in one
    # profile is invisible to the other, and setup creates no user topic.
    assert one._service.set_user_topic(42, 777)
    assert one._service.user_topic(42) == 777
    assert two._service.user_topic(42) is None
    titles=[topic.title for topic in native.topics if topic.title.startswith('Operations · Profile ')]
    assert len(titles)==2 and titles[0]!=titles[1]
    assert not [topic for topic in native.topics if 'User 42' in topic.title]
    asyncio.run(one.disconnected())
    asyncio.run(two.disconnected())


def test_reconnect_enqueues_no_startup_notice(tmp_path,monkeypatch):
    from plugins.platforms.telegram.forum_audit import _db
    native=_NativeForum()
    adapter=native.adapter(tmp_path,{'forum_audit':{'enabled':True,'chat_id':-1000000000123}})
    controller=ForumAuditTransport(adapter,tmp_path)
    monkeypatch.setattr(controller,'_start_worker',lambda:None)
    async def scenario():
        await controller.connected()
        assert controller._service.enabled
        assert controller._service.status()['pending']==0
        await controller.connected()
        assert controller._service.enabled
        assert controller._service.status()['pending']==0
        with _db(controller._service._storage_root) as db:
            assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
        await controller.disconnected()
    asyncio.run(scenario())


def test_cross_profile_source_is_not_rerouted_to_adapter_forum(tmp_path,monkeypatch):
    native=_NativeForum()
    adapter=native.adapter(tmp_path)
    controller=ForumAuditTransport(adapter,tmp_path)
    monkeypatch.setattr(controller,'_start_worker',lambda:None)
    async def scenario():
        await controller.setup(-1000000000123)
        adapter._settings_home=lambda source:tmp_path/'other-profile'
        source=_Source()
        await adapter._process_inbound_event({'source':source},[_message(101, source.chat_id)])
        assert controller._service.status()['pending']==0
        assert adapter.original_calls==['trigger-filter-and-routing']
        await controller.disconnected()
    asyncio.run(scenario())


def test_operations_capture_uses_profile_context_and_never_raw_exception_text(tmp_path,monkeypatch):
    import logging
    import hermes_constants
    from plugins.platforms.telegram import forum_audit
    from plugins.platforms.telegram.forum_audit_transport import _OperationsLogHandler
    calls=[]
    monkeypatch.setattr(hermes_constants,'get_hermes_home_override',lambda:str(tmp_path))
    monkeypatch.setattr(forum_audit,'enqueue_operation',lambda home,text:calls.append((home,text)))
    handler=_OperationsLogHandler(tmp_path)
    record=logging.LogRecord('plugins.platforms.telegram.telethon_adapter',logging.ERROR,
        __file__,1,'provider error SECRET-CANARY',(),None)
    handler.emit(record)
    assert len(calls)==1 and calls[0][0]==tmp_path
    assert 'SECRET-CANARY' not in calls[0][1]
    record.hermes_home=str(tmp_path/'other')
    handler.emit(record)
    assert len(calls)==1
    del record.hermes_home
    monkeypatch.setattr(hermes_constants,'get_hermes_home_override',lambda:None)
    handler.emit(record)
    assert len(calls)==1  # Unscoped process warnings must not leak into a profile.
