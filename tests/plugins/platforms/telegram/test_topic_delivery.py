"""Dedicated-topic delivery must survive real TL envelopes and foreign reply anchors."""
from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def updates(*items):
    return types.Updates(updates=list(items), users=[], chats=[], date=NOW, seq=1)


def message(mid, topic, *, out=True):
    return types.Message(
        id=mid, peer_id=types.PeerUser(42), message="reply", date=NOW, out=out,
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic),
    )


def test_topic_creation_receipt_rejects_unmatched_random_id_and_foreign_service_peer():
    from plugins.platforms.telegram.deep_research_ui import created_topic_id_from_updates

    unrelated_mapping = updates(types.UpdateMessageID(id=777, random_id=1234))
    assert created_topic_id_from_updates(
        unrelated_mapping, random_id=5678, peer=types.PeerUser(42),
    ) is None
    foreign_service = types.MessageService(
        id=777, peer_id=types.PeerUser(43), date=NOW,
        action=types.MessageActionTopicCreate(title="Foreign", icon_color=0),
    )
    assert created_topic_id_from_updates(
        updates(types.UpdateNewMessage(foreign_service, pts=1, pts_count=1)),
        random_id=5678, peer=types.PeerUser(42),
    ) is None


@pytest.mark.asyncio
async def test_created_topic_welcome_and_replies_use_valid_headers_and_own_receipts():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return message(ids, 777)

        async def __call__(self, request):
            calls.append(request)
            if isinstance(request, functions.messages.CreateForumTopicRequest):
                service = types.MessageService(
                    id=777, peer_id=types.PeerUser(42), date=NOW,
                    action=types.MessageActionTopicCreate(title="Research", icon_color=0),
                )
                return types.UpdateShort(types.UpdateNewMessage(service, pts=1, pts_count=1), date=NOW)
            header = request.reply_to
            # Every send into the topic carries top_msg_id, including a send
            # addressed directly at the topic root: without it the message
            # lands outside the topic and can never be edited in place.
            assert header.top_msg_id == 777
            mid = 800 + len(calls)
            return updates(
                types.UpdateNewMessage(message(66, 555, out=False), pts=2, pts_count=1),
                types.UpdateMessageID(id=mid, random_id=request.random_id),
            )

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    source = SimpleNamespace(chat_id="42", chat_type="dm", thread_id=None)
    topic = await adapter._create_topic_from_message(source, "Research")
    assert topic == 777
    assert adapter._topic_welcome_message_id("42", topic) == "802"
    state = {"deep_research": {"session_id": "abc123abc123", "latest_reply_message_id": "802"}}
    metadata = {
        "thread_id": str(topic), "deep_research_content": True,
        "telegram_strict_thread": True, "telegram_task_reply_chain": True,
        "deep_research": state["deep_research"], "telegram_task_event_metadata": state,
    }
    result = await adapter.send("42", "Research progress", metadata=metadata)
    assert result.success and result.message_id == "803"
    assert calls[-1].reply_to.reply_to_msg_id == 802
    assert calls[-1].reply_to.top_msg_id == topic
    assert state["deep_research"]["latest_reply_message_id"] == "803"


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_key", ["direct_messages_topic_id", "telegram_direct_messages_topic_id"])
async def test_strict_dm_topic_status_uses_topic_id_not_parent_command_anchor(topic_key):
    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            # The command reply belongs to the parent DM, not the new topic.
            return types.Message(
                id=ids, peer_id=types.PeerUser(42), message="/deepresearch start", date=NOW,
            )

        async def __call__(self, request):
            requests.append(request)
            return types.UpdateShort(types.UpdateMessageID(id=901, random_id=request.random_id), date=NOW)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    result = await adapter.send(
        "42", "Deep research is starting in its dedicated topic.", reply_to="12",
        metadata={
            topic_key: "777",
            "telegram_strict_thread": True,
            "deep_research_status_card": True,
        },
    )

    assert result.success and result.message_id == "901"
    assert len(requests) == 1
    assert requests[0].reply_to.reply_to_msg_id == 777
    assert requests[0].reply_to.top_msg_id == 777


@pytest.mark.asyncio
async def test_explicit_research_topic_route_uses_topic_root_not_parent_anchor(tmp_path):
    from gateway.platforms.base import _augment_telegram_task_metadata

    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return types.Message(id=ids, peer_id=types.PeerUser(42), message="parent", date=NOW)

        async def upload_file(self, path):
            return types.InputFile(id=1, parts=1, name="report.md", md5_checksum="")

        async def _parse_message_text(self, text, mode):
            return text, []

        async def __call__(self, request):
            requests.append(request)
            return types.UpdateShort(
                types.UpdateMessageID(id=900 + len(requests), random_id=request.random_id), date=NOW,
            )

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    source = SimpleNamespace(platform="telegram", chat_id="42", user_id="42", chat_type="dm", thread_id="777")
    event_metadata = {
        "thread_id": "777",
        "deep_research": {"session_id": "abc123abc123", "latest_reply_message_id": "12"},
    }
    metadata = _augment_telegram_task_metadata(SimpleNamespace(source=source, metadata=event_metadata), {"thread_id": "777"})

    assert metadata["telegram_send_in_topic"] is True
    assert metadata["direct_messages_topic_id"] == "777"
    text_result = await adapter.send("42", "Research findings", reply_to="12", metadata=metadata)
    assert text_result.success
    assert requests[0].reply_to.reply_to_msg_id == 777
    assert requests[0].reply_to.top_msg_id == 777

    report = tmp_path / "report.md"
    report.write_text("# Report\n")
    document_result = await adapter.send_document("42", str(report), caption="Report", metadata=metadata)
    assert document_result.success
    assert requests[1].reply_to.reply_to_msg_id == 777
    assert requests[1].reply_to.top_msg_id == 777

    ordinary_metadata = dict(metadata)
    ordinary_metadata.pop("telegram_send_in_topic")
    ordinary = await adapter.send("42", "Ordinary reply", reply_to="12", metadata=ordinary_metadata)
    assert not ordinary.success
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["mapping", "message"])
@pytest.mark.parametrize("kind", ["progress", "failure", "reply", "report", "edit"])
@pytest.mark.parametrize("anchor_topic", [777, 555, None])
async def test_strict_research_delivery_never_follows_an_anchor_outside_its_topic(tmp_path, kind, anchor_topic, receipt):
    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return message(ids, anchor_topic) if anchor_topic else None

        async def upload_file(self, path):
            return types.InputFile(id=1, parts=1, name="report.md", md5_checksum="")

        async def _parse_message_text(self, text, mode):
            return text, []

        async def __call__(self, request):
            requests.append(request)
            if isinstance(request, functions.messages.EditMessageRequest):
                return updates()
            if receipt == "message":
                return types.UpdateShort(types.UpdateNewMessage(message(901, 777), pts=1, pts_count=1), date=NOW)
            return types.UpdateShort(types.UpdateMessageID(id=901, random_id=request.random_id), date=NOW)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    state = {"deep_research": {"session_id": "abc123abc123", "latest_reply_message_id": "66"}}
    metadata = {
        "thread_id": "777", "deep_research_content": True,
        "telegram_strict_thread": True, "telegram_task_reply_chain": True,
        "deep_research": state["deep_research"], "telegram_task_event_metadata": state,
        "telegram_progress_card": kind == "progress",
    }
    if kind == "edit":
        result = await adapter.edit_message("42", "66", "Updated progress", metadata=metadata)
        assert result.success is (anchor_topic == 777)
        assert len(requests) == (1 if anchor_topic == 777 else 0)
        assert state["deep_research"]["latest_reply_message_id"] == "66"
        return
    if kind == "report":
        report = tmp_path / "report.md"
        report.write_text("# Report\n")
        result = await adapter.send_document("42", str(report), caption="Report", metadata=metadata)
    else:
        result = await adapter.send("42", kind, reply_to="12", metadata=metadata)
    assert result.success is (anchor_topic == 777)
    assert len(requests) == (1 if anchor_topic == 777 else 0)
    if anchor_topic != 777:
        assert state["deep_research"]["latest_reply_message_id"] == "66"
        return
    header = requests[0].reply_to
    assert header.reply_to_msg_id == 66
    assert header.top_msg_id == 777
    assert state["deep_research"]["latest_reply_message_id"] == "901"


@pytest.mark.asyncio
async def test_strict_research_anchor_from_another_peer_is_rejected_even_with_matching_topic():
    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            foreign = message(ids, 777)
            foreign.peer_id = types.PeerUser(43)
            return foreign

        async def __call__(self, request):
            requests.append(request)
            return types.UpdateShort(types.UpdateMessageID(id=901, random_id=request.random_id), date=NOW)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    result = await adapter.send(
        "42", "Research progress", reply_to="66",
        metadata={"thread_id": "777", "deep_research_content": True, "telegram_strict_thread": True},
    )
    assert not result.success
    assert requests == []


@pytest.mark.asyncio
async def test_research_pause_failure_never_falls_back_to_invocation_topic(tmp_path):
    from gateway.deep_research_sessions import create_session
    from gateway.session import SessionSource
    from gateway.config import Platform

    adapter = TelethonAdapter(PlatformConfig())
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=False, error="topic unavailable"))
    ui = adapter._research_ui()
    ui._edit_card = AsyncMock()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm", thread_id="555")
    topic_source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm", thread_id="777")
    record = create_session(tmp_path, "42", "Question", chat_id="42", chat_type="dm", thread_id="777")
    await ui._pause_before_start(
        source=source, home=tmp_path, session_id=record["session_id"],
        trigger_message_id="12", confirmation_card_id="13", topic_source=topic_source,
        reason="topic_send_permission", message="Topic delivery is unavailable.",
    )
    adapter.send.assert_awaited_once()
    sent = adapter.send.await_args
    assert sent.kwargs["reply_to"] == "777"
    assert sent.kwargs["metadata"]["thread_id"] == "777"
    assert sent.kwargs["metadata"]["telegram_strict_thread"] is True
    ui._edit_card.assert_not_awaited()


def test_topic_reply_header_carries_quote_excerpt_and_offset():
    from plugins.platforms.telegram.topic_delivery import topic_reply_header

    header = topic_reply_header(9, 44, quote_text="brown fox", quote_offset=10)
    assert header.reply_to_msg_id == 9
    assert header.top_msg_id == 44
    assert header.quote_text == "brown fox"
    assert header.quote_offset == 10
    assert header.quote_entities is None


def test_topic_reply_header_without_quote_is_unchanged():
    from plugins.platforms.telegram.topic_delivery import topic_reply_header

    header = topic_reply_header(55, 44)
    assert header.reply_to_msg_id == 55
    assert header.top_msg_id == 44
    assert header.quote_text is None
    assert header.quote_offset is None
    assert topic_reply_header(None, None) is None


def test_topic_reply_header_keeps_topic_on_root_addressed_send():
    """A send addressed at the topic root still carries top_msg_id.

    Without it the message lands outside the topic (observed live in
    threaded DMs) and the echoed message carries no reply_to_top_id, so
    strict edit verification refuses every later progress-card edit.
    """
    from plugins.platforms.telegram.topic_delivery import topic_reply_header

    header = topic_reply_header(777, 777)
    assert header.reply_to_msg_id == 777
    assert header.top_msg_id == 777
    omitted = topic_reply_header(None, 777)
    assert omitted.reply_to_msg_id == 777
    assert omitted.top_msg_id == 777
    assert topic_reply_header(777, 1).top_msg_id is None


@pytest.mark.asyncio
async def test_sent_message_id_accepts_dm_topic_echo_without_top_id():
    """Threaded DMs may echo no top id and no forum flag. An outgoing
    message replying to the topic root is still the sent message: without
    this the progress card gets no ID and freezes at its initial state."""
    from plugins.platforms.telegram.topic_delivery import sent_message_id

    echoed = types.Message(
        id=901, peer_id=types.PeerUser(42), message="card", date=NOW, out=True,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=777),
    )
    response = updates(types.UpdateNewMessage(echoed, pts=1, pts_count=1))
    assert sent_message_id(response, random_id=4242, peer=types.PeerUser(42), topic_id=777) == "901"


@pytest.mark.asyncio
async def test_verified_topic_reply_accepts_own_root_reply_without_top_id():
    """Edit verification must accept our own message replying to the topic
    root when the server echoed no top id, or every card edit is refused."""
    from plugins.platforms.telegram.topic_delivery import verified_topic_reply

    echoed = types.Message(
        id=901, peer_id=types.PeerUser(42), message="card", date=NOW, out=True,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=777),
    )

    class Client:
        async def get_messages(self, peer, *, ids):
            assert ids == 901
            return echoed

    adapter = SimpleNamespace(_client=Client(), _message_thread_id=staticmethod(lambda _m: None))
    assert await verified_topic_reply(adapter, types.PeerUser(42), 901, 777, {}) == 901
