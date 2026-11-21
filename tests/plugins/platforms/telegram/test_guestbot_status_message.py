"""GuestBot status message: a real reply to the guest query, edited with the answer.

The status goes out the moment a guest query reaches its turn (``reply_to`` is
the query message id); the final response edits that message in place instead
of sending a second message. If the edit fails, the answer falls back to the
proven guest-result delivery, so the guest always gets it. The draft streaming
path is untouched by these tests.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _GuestClient:
    """Fake Telethon client: records raw requests, sent messages, and deletes."""

    def __init__(self, *, fail_edit=False, fail_status=False, users=None, channels=None):
        self.requests = []
        self.sent_messages = []
        self.deleted = []
        self.fail_edit = fail_edit
        self.fail_status = fail_status
        self._next_message_id = 900
        # Real entities the server returns for a fresh GetUsersRequest /
        # GetChannelsRequest (access_hash=0 bot special case), keyed by id.
        self.users = dict(users or {})
        self.channels = dict(channels or {})

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_edit and type(request).__name__ == "EditMessageRequest":
            raise RuntimeError("edit transport down")
        name = type(request).__name__
        if name == "GetUsersRequest":
            # TL field for the user list is `id`, not `users`.
            return [self.users[u.user_id] for u in request.id if u.user_id in self.users]
        if name == "GetChannelsRequest":
            return SimpleNamespace(
                chats=[self.channels[c.channel_id] for c in request.id if c.channel_id in self.channels])
        return SimpleNamespace()

    async def send_message(self, peer, text, *, reply_to=None):
        if self.fail_status:
            raise RuntimeError("status send down")
        self._next_message_id += 1
        record = {"peer": peer, "text": text, "reply_to": reply_to, "id": self._next_message_id}
        self.sent_messages.append(record)
        return SimpleNamespace(id=self._next_message_id)

    async def delete_messages(self, peer, message_ids):
        self.deleted.append({"peer": peer, "message_ids": list(message_ids)})


def _guest_update(*, query_id=1234, message_id=500, peer_id=None):
    from telethon import types

    message = SimpleNamespace(
        peer_id=peer_id if peer_id is not None else types.PeerChat(chat_id=88),
        from_id=types.PeerUser(user_id=77),
        id=message_id, message="Can you help?", reply_to=None, reply_to_msg_id=None,
    )
    return SimpleNamespace(query_id=query_id, message=message)


def _guest_result_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetBotGuestChatResultRequest"]


def _edit_requests(client):
    return [r for r in client.requests if type(r).__name__ == "EditMessageRequest"]


def _guest_adapter(client, handler, *, authorized=True, extra=None):
    adapter = TelethonAdapter(PlatformConfig(extra=extra or {}))
    adapter._authorized = lambda _source: authorized
    adapter._client = client
    adapter._message_handler = handler
    return adapter


@pytest.mark.asyncio
async def test_guest_query_sends_status_reply_then_edits_it_with_the_answer():
    client = _GuestClient()
    seen_status = []

    async def handle(event):
        # The status must already be in the chat when the turn starts.
        seen_status.append(list(client.sent_messages))
        return "Hello **guest**"

    adapter = _guest_adapter(client, handle)
    await adapter._on_guest_chat_query(_guest_update())

    assert len(seen_status) == 1
    assert len(seen_status[0]) == 1
    status = seen_status[0][0]
    assert status["text"] == "Thinking…"
    assert status["reply_to"] == 500
    edits = _edit_requests(client)
    assert len(edits) == 1
    assert edits[0].id == status["id"]
    assert edits[0].rich_message.markdown == "Hello **guest**"
    assert _guest_result_requests(client) == []


@pytest.mark.asyncio
async def test_guest_status_edit_failure_falls_back_to_guest_result():
    client = _GuestClient(fail_edit=True)
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))
    await adapter._on_guest_chat_query(_guest_update())

    assert len(client.sent_messages) == 1
    status_id = client.sent_messages[0]["id"]
    assert len(client.deleted) == 1
    assert client.deleted[0]["message_ids"] == [status_id]
    results = _guest_result_requests(client)
    assert len(results) == 1
    assert results[0].result.send_message.rich_message.markdown == "Hello **guest**"


@pytest.mark.asyncio
async def test_guest_status_send_failure_falls_back_to_guest_result():
    # Guard: when no status message can be sent, delivery is exactly the old
    # guest-result path — no edit is attempted.
    client = _GuestClient(fail_status=True)
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))
    await adapter._on_guest_chat_query(_guest_update())

    assert client.sent_messages == []
    assert _edit_requests(client) == []
    results = _guest_result_requests(client)
    assert len(results) == 1
    assert results[0].result.send_message.rich_message.markdown == "Hello **guest**"


@pytest.mark.asyncio
async def test_guest_query_from_non_allowed_account_is_silent():
    """A non-allowed guest query gets nothing: no guest result, no status
    message, and the turn never runs."""
    client = _GuestClient()
    handler = AsyncMock(return_value="Hello **guest**")
    adapter = _guest_adapter(client, handler, authorized=False)
    await adapter._on_guest_chat_query(_guest_update())

    handler.assert_not_awaited()
    assert client.sent_messages == []
    assert client.deleted == []
    assert _guest_result_requests(client) == []
    assert _edit_requests(client) == []


@pytest.mark.asyncio
async def test_guest_query_while_disabled_is_silent():
    """Guestbot disabled for the profile: an allowed guest query gets
    nothing — no guest result, no status message, no turn."""
    client = _GuestClient()
    handler = AsyncMock(return_value="Hello **guest**")
    adapter = _guest_adapter(client, handler, extra={"guest_mode": False})
    await adapter._on_guest_chat_query(_guest_update())

    handler.assert_not_awaited()
    assert client.sent_messages == []
    assert client.deleted == []
    assert _guest_result_requests(client) == []
    assert _edit_requests(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", "   \n  "])
async def test_guest_empty_response_is_silent_and_removes_status(empty):
    """An empty agent response delivers nothing — no guest result, no edit —
    and the "Thinking…" status message is removed instead of left dangling."""
    client = _GuestClient()
    adapter = _guest_adapter(client, AsyncMock(return_value=empty))
    await adapter._on_guest_chat_query(_guest_update())

    assert len(client.sent_messages) == 1
    status_id = client.sent_messages[0]["id"]
    assert client.sent_messages[0]["text"] == "Thinking…"
    assert len(client.deleted) == 1
    assert client.deleted[0]["message_ids"] == [status_id]
    assert _guest_result_requests(client) == []
    assert _edit_requests(client) == []


@pytest.mark.asyncio
async def test_guest_status_uses_update_peer_when_entity_cache_misses():
    """Guest chats are exactly the chats the bot may not have in its
    entity cache: when the client cannot resolve the chat, the status is
    still sent using the update's own peer_id, and the answer edit works
    because the resolved peer is cached."""
    from telethon import types

    client = _GuestClient()
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))

    async def _never_resolves(target):
        raise ValueError("Could not find the input entity")

    # The client's entity cache misses for this guest chat — the failure
    # mode live. The adapter's own cache lookup still works.
    client.get_input_entity = _never_resolves
    await adapter._on_guest_chat_query(_guest_update())

    assert len(client.sent_messages) == 1
    status = client.sent_messages[0]
    assert status["text"] == "Thinking…"
    assert status["reply_to"] == 500
    assert type(status["peer"]).__name__ == "InputPeerChat"
    assert status["peer"].chat_id == 88
    assert len(_edit_requests(client)) == 1


@pytest.mark.asyncio
async def test_guest_status_dm_user_resolves_fresh_when_entity_cache_misses():
    """A DM guest query from a user the bot has never seen: the update peer
    is a bare PeerUser, which get_input_peer cannot cast, and the entity
    cache misses — the status must still send by resolving the user fresh
    (GetUsersRequest with access_hash=0, the bot special case), and the
    answer edit must land on the same message."""
    from telethon import types

    user = types.User(id=77, access_hash=12345, first_name="Guest")
    client = _GuestClient(users={77: user})
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))

    async def _never_resolves(target):
        raise ValueError("Could not find the input entity")

    client.get_input_entity = _never_resolves
    await adapter._on_guest_chat_query(_guest_update(peer_id=types.PeerUser(user_id=77)))

    assert len(client.sent_messages) == 1
    status = client.sent_messages[0]
    assert status["text"] == "Thinking…"
    assert status["reply_to"] == 500
    assert type(status["peer"]).__name__ == "InputPeerUser"
    assert status["peer"].user_id == 77
    assert len(_edit_requests(client)) == 1
    assert _guest_result_requests(client) == []


@pytest.mark.asyncio
async def test_guest_status_group_channel_resolves_fresh_when_entity_cache_misses():
    """A group guest query: the update peer is a bare PeerChannel, which
    get_input_peer cannot cast, and the entity cache misses — the status
    must still send by resolving the channel fresh (GetChannelsRequest
    with access_hash=0), and the answer edit must land on the same
    message."""
    from telethon import types

    channel = types.Channel(id=2397157264, access_hash=67890, title="Test Group", photo=None, date=None)
    client = _GuestClient(channels={2397157264: channel})
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))

    async def _never_resolves(target):
        raise ValueError("Could not find the input entity")

    client.get_input_entity = _never_resolves
    await adapter._on_guest_chat_query(
        _guest_update(peer_id=types.PeerChannel(channel_id=2397157264)))

    assert len(client.sent_messages) == 1
    status = client.sent_messages[0]
    assert status["text"] == "Thinking…"
    assert status["reply_to"] == 500
    assert type(status["peer"]).__name__ == "InputPeerChannel"
    assert status["peer"].channel_id == 2397157264
    assert len(_edit_requests(client)) == 1
    assert _guest_result_requests(client) == []


@pytest.mark.asyncio
async def test_guest_status_reply_failure_retries_without_reply():
    """A reply the server rejects (guest context) falls back to a plain
    status — a status without a reply is still better than no status."""
    client = _GuestClient()
    real_send = client.send_message

    async def _fail_reply(peer, text, *, reply_to=None):
        if reply_to is not None:
            raise RuntimeError("reply message not visible to bot")
        return await real_send(peer, text, reply_to=None)

    client.send_message = _fail_reply
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))
    await adapter._on_guest_chat_query(_guest_update())

    assert len(client.sent_messages) == 1
    status = client.sent_messages[0]
    assert status["text"] == "Thinking…"
    assert status["reply_to"] is None
    assert len(_edit_requests(client)) == 1


def _slow_handler_factory(done: asyncio.Event):
    """A turn that finishes one second in — slow, but a normal long turn."""

    async def slow_handler(event):
        await done.wait()
        return "Slow but done"

    return slow_handler


@pytest.mark.asyncio
async def test_guest_slow_turn_times_out_with_an_answer_not_silence(monkeypatch):
    """A turn that outruns the handler timeout tells the guest it took too
    long — and releases the per-user lock — instead of hanging. The timeout
    is patched small here; the production value covers a normal long turn."""
    from plugins.platforms.telegram import guestbot as guestbot_module

    monkeypatch.setattr(guestbot_module, "_GUEST_HANDLER_TIMEOUT_SECONDS", 0.05,
                        raising=False)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    timer = loop.call_later(1.0, done.set)
    try:
        client = _GuestClient()
        adapter = _guest_adapter(client, _slow_handler_factory(done))
        await adapter._on_guest_chat_query(_guest_update())
    finally:
        timer.cancel()

    edits = _edit_requests(client)
    assert len(edits) == 1
    assert "took too long" in edits[0].rich_message.markdown
    assert _guest_result_requests(client) == []
    assert adapter._guestbot_processing == {}


@pytest.mark.asyncio
async def test_guest_query_after_timeout_runs_and_delivers(monkeypatch):
    """After a timed-out turn the per-user lock is released: the guest's next
    query runs normally instead of hitting 'already processing'."""
    from plugins.platforms.telegram import guestbot as guestbot_module

    monkeypatch.setattr(guestbot_module, "_GUEST_HANDLER_TIMEOUT_SECONDS", 0.05,
                        raising=False)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    timer = loop.call_later(1.0, done.set)
    try:
        client = _GuestClient()
        adapter = _guest_adapter(client, _slow_handler_factory(done))
        await adapter._on_guest_chat_query(_guest_update())
        adapter._message_handler = AsyncMock(return_value="Second answer")
        await adapter._on_guest_chat_query(_guest_update(query_id=1235, message_id=501))
    finally:
        timer.cancel()

    edits = _edit_requests(client)
    assert len(edits) == 2
    assert "took too long" in edits[0].rich_message.markdown
    assert edits[1].rich_message.markdown == "Second answer"
