"""GuestBot Thinking message: the guest result itself, edited with the answer.

The query is answered with "Thinking…" the moment it reaches its turn —
the guest result is the one channel that always reaches the guest, so it
is the Thinking message now. The final response edits that inline message
in place instead of sending a second message. If the inline edit fails,
the answer falls back to a fresh guest result. The regular status message
survives only as the fallback when the guest result cannot be sent at
Thinking time (peer resolution, reply fallback). The draft streaming path
is untouched by these tests.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.guestbot import _GUEST_EMPTY_ANSWER_TEXT
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _GuestClient:
    """Fake Telethon client: records raw requests, sent messages, and deletes."""

    def __init__(self, *, fail_edit=False, fail_status=False, fail_thinking_result=False,
                 fail_inline_edit=False, users=None, channels=None):
        self.requests = []
        self.sent_messages = []
        self.deleted = []
        self.fail_edit = fail_edit
        self.fail_status = fail_status
        self.fail_thinking_result = fail_thinking_result
        self.fail_inline_edit = fail_inline_edit
        self.last_inline_id = None
        self._next_message_id = 900
        # Real entities the server returns for a fresh GetUsersRequest /
        # GetChannelsRequest (access_hash=0 bot special case), keyed by id.
        self.users = dict(users or {})
        self.channels = dict(channels or {})

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_edit and type(request).__name__ == "EditMessageRequest":
            raise RuntimeError("edit transport down")
        if self.fail_inline_edit and type(request).__name__ == "EditInlineBotMessageRequest":
            raise RuntimeError("inline edit transport down")
        name = type(request).__name__
        if name == "GetUsersRequest":
            # TL field for the user list is `id`, not `users`.
            return [self.users[u.user_id] for u in request.id if u.user_id in self.users]
        if name == "GetChannelsRequest":
            return SimpleNamespace(
                chats=[self.channels[c.channel_id] for c in request.id if c.channel_id in self.channels])
        if name == "SetBotGuestChatResultRequest":
            # The server hands back the inline message id of the delivered
            # result — the Thinking message the answer later edits.
            markdown = getattr(request.result.send_message.rich_message, "markdown", None)
            if self.fail_thinking_result and markdown == "Thinking…":
                raise RuntimeError("guest result send down")
            self.last_inline_id = SimpleNamespace(dc_id=1, id=4242, access_hash=99)
            return self.last_inline_id
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


def _guest_update(*, query_id=1234, message_id=500, peer_id=None, text="Can you help?"):
    from telethon import types

    message = SimpleNamespace(
        peer_id=peer_id if peer_id is not None else types.PeerChat(chat_id=88),
        from_id=types.PeerUser(user_id=77),
        id=message_id, message=text, reply_to=None, reply_to_msg_id=None,
    )
    return SimpleNamespace(query_id=query_id, message=message)


def _guest_result_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetBotGuestChatResultRequest"]


def _guest_result_markdowns(client):
    return [getattr(r.result.send_message.rich_message, "markdown", None)
            for r in _guest_result_requests(client)]


def _edit_requests(client):
    return [r for r in client.requests if type(r).__name__ == "EditMessageRequest"]


def _inline_edit_requests(client):
    return [r for r in client.requests if type(r).__name__ == "EditInlineBotMessageRequest"]


def _guest_adapter(client, handler, *, authorized=True, extra=None):
    adapter = TelethonAdapter(PlatformConfig(extra=extra or {}))
    adapter._authorized = lambda _source: authorized
    adapter._client = client
    adapter._message_handler = handler
    return adapter


@pytest.mark.asyncio
async def test_guest_query_answers_thinking_first_then_edits_it_with_the_answer():
    client = _GuestClient()
    seen_thinking = []

    async def handle(event):
        # The Thinking guest result must already be delivered when the turn starts.
        seen_thinking.append(_guest_result_markdowns(client))
        return "Hello **guest**"

    adapter = _guest_adapter(client, handle)
    await adapter._on_guest_chat_query(_guest_update())

    assert len(seen_thinking) == 1
    assert seen_thinking[0] == ["Thinking…"]
    assert client.sent_messages == []
    assert _edit_requests(client) == []
    edits = _inline_edit_requests(client)
    assert len(edits) == 1
    assert edits[0].id is client.last_inline_id
    assert edits[0].rich_message.markdown == "Hello **guest**"
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_inline_edit_failure_falls_back_to_guest_result():
    client = _GuestClient(fail_inline_edit=True)
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))
    await adapter._on_guest_chat_query(_guest_update())

    assert client.sent_messages == []
    assert len(_inline_edit_requests(client)) == 1
    assert _guest_result_markdowns(client) == ["Thinking…", "Hello **guest**"]


@pytest.mark.asyncio
async def test_guest_thinking_result_and_status_failures_fall_back_to_guest_result():
    # Guard: when the Thinking guest result cannot be sent and no status
    # message can be sent either, the answer goes out the old guest-result
    # path — no edit is attempted.
    client = _GuestClient(fail_status=True, fail_thinking_result=True)
    adapter = _guest_adapter(client, AsyncMock(return_value="Hello **guest**"))
    await adapter._on_guest_chat_query(_guest_update())

    assert client.sent_messages == []
    assert _edit_requests(client) == []
    assert _inline_edit_requests(client) == []
    assert _guest_result_markdowns(client) == ["Thinking…", "Hello **guest**"]


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
    assert _inline_edit_requests(client) == []


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
    assert _inline_edit_requests(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", "   \n  "])
async def test_guest_empty_response_edits_thinking_with_a_fallback(empty):
    """An empty agent response still answers: the Thinking guest result
    cannot be deleted, so it is edited with a short fallback instead of
    being left dangling — no silence, no second message."""
    client = _GuestClient()
    adapter = _guest_adapter(client, AsyncMock(return_value=empty))
    await adapter._on_guest_chat_query(_guest_update())

    assert client.sent_messages == []
    assert client.deleted == []
    assert _guest_result_markdowns(client) == ["Thinking…"]
    edits = _inline_edit_requests(client)
    assert len(edits) == 1
    assert edits[0].rich_message.markdown == _GUEST_EMPTY_ANSWER_TEXT
    assert _edit_requests(client) == []


@pytest.mark.asyncio
async def test_guest_status_uses_update_peer_when_entity_cache_misses():
    """Guest chats are exactly the chats the bot may not have in its
    entity cache: when the guest result cannot be sent, the status still
    goes out using the update's own peer_id, and the answer edit works
    because the resolved peer is cached."""
    from telethon import types

    client = _GuestClient(fail_thinking_result=True)
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
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_status_dm_user_resolves_fresh_when_entity_cache_misses():
    """A DM guest query from a user the bot has never seen: the update peer
    is a bare PeerUser, which get_input_peer cannot cast, and the entity
    cache misses — when the guest result cannot be sent, the status must
    still send by resolving the user fresh (GetUsersRequest with
    access_hash=0, the bot special case), and the answer edit must land on
    the same message."""
    from telethon import types

    user = types.User(id=77, access_hash=12345, first_name="Guest")
    client = _GuestClient(users={77: user}, fail_thinking_result=True)
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
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_status_group_channel_resolves_fresh_when_entity_cache_misses():
    """A group guest query: the update peer is a bare PeerChannel, which
    get_input_peer cannot cast, and the entity cache misses — when the guest
    result cannot be sent, the status must still send by resolving the
    channel fresh (GetChannelsRequest with access_hash=0), and the answer
    edit must land on the same message."""
    from telethon import types

    channel = types.Channel(id=2397157264, access_hash=67890, title="Test Group", photo=None, date=None)
    client = _GuestClient(channels={2397157264: channel}, fail_thinking_result=True)
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
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_status_reply_failure_retries_without_reply():
    """A reply the server rejects (guest context) falls back to a plain
    status — a status without a reply is still better than no status."""
    client = _GuestClient(fail_thinking_result=True)
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


@pytest.mark.asyncio
async def test_guest_second_mention_while_first_runs_gets_its_own_turn():
    """No 'already processing': a second mention while the first turn runs
    starts its own turn at once and both answers land."""
    client = _GuestClient()
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def handle(event):
        if not first_started.is_set():
            first_started.set()
            await release_first.wait()
            return "First answer"
        return "Second answer"

    adapter = _guest_adapter(client, handle)
    first = asyncio.create_task(adapter._on_guest_chat_query(_guest_update(query_id=1234)))
    await first_started.wait()
    # The second mention runs while the first is still going — it must not
    # be refused, and it finishes before the first does.
    await adapter._on_guest_chat_query(_guest_update(query_id=1235, message_id=501))
    release_first.set()
    await first

    assert _guest_result_markdowns(client) == ["Thinking…", "Thinking…"]
    assert "already processing" not in " ".join(_guest_result_markdowns(client))
    edits = _inline_edit_requests(client)
    assert len(edits) == 2
    assert edits[0].rich_message.markdown == "Second answer"
    assert edits[1].rich_message.markdown == "First answer"
    assert edits[0].id is not edits[1].id


@pytest.mark.asyncio
async def test_guest_each_mention_gets_its_own_thread():
    """Every mention is its own turn: the two threads differ, both guest threads."""
    client = _GuestClient()
    threads = []

    async def handle(event):
        threads.append(event.source.thread_id)
        return "ok"

    adapter = _guest_adapter(client, handle)
    await adapter._on_guest_chat_query(_guest_update(query_id=1234))
    await adapter._on_guest_chat_query(_guest_update(query_id=1235, message_id=501))

    assert len(threads) == 2
    assert threads[0] != threads[1]
    assert all(t.startswith("guest-") for t in threads)


@pytest.mark.asyncio
async def test_guest_failed_turn_answers_with_its_reason_not_a_hang():
    """A turn that fails on a provider error answers with the failure text —
    the reason — instead of hanging or timing out."""
    client = _GuestClient()
    adapter = _guest_adapter(
        client, AsyncMock(return_value="This turn failed: provider limited (reset in 5 minutes)."))
    await adapter._on_guest_chat_query(_guest_update())

    edits = _inline_edit_requests(client)
    assert len(edits) == 1
    assert "provider limited" in edits[0].rich_message.markdown
    assert "took too long" not in edits[0].rich_message.markdown
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_raising_turn_answers_with_a_generic_failure_not_silence():
    """A turn that raises answers with the generic failure text — the guest
    is never left on 'Thinking…'."""
    client = _GuestClient()

    async def handle(event):
        raise RuntimeError("provider blew up")

    adapter = _guest_adapter(client, handle)
    await adapter._on_guest_chat_query(_guest_update())

    edits = _inline_edit_requests(client)
    assert len(edits) == 1
    assert "could not process" in edits[0].rich_message.markdown
    assert _guest_result_markdowns(client) == ["Thinking…"]


def _slow_handler_factory(done: asyncio.Event):
    """A turn that finishes one second in — slow, but a normal long turn."""

    async def slow_handler(event):
        await done.wait()
        return "Slow but done"

    return slow_handler


@pytest.mark.asyncio
async def test_guest_slow_turn_past_timeout_keeps_running_and_delivers(monkeypatch):
    """A turn that outruns the handler timeout is never killed and never
    told 'took too long': the Thinking message notes the wait, and the
    answer lands when the turn finishes. The timeout is patched small here;
    the production value is 30 minutes."""
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

    edits = _inline_edit_requests(client)
    assert len(edits) == 2
    assert edits[0].rich_message.markdown == "Still working on it…"
    assert edits[1].rich_message.markdown == "Slow but done"
    assert "took too long" not in " ".join(e.rich_message.markdown for e in edits)
    assert _guest_result_markdowns(client) == ["Thinking…"]


@pytest.mark.asyncio
async def test_guest_query_after_slow_turn_runs_and_delivers(monkeypatch):
    """After a slow turn that ran past the timeout and delivered, the
    guest's next query runs normally."""
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

    edits = _inline_edit_requests(client)
    assert len(edits) == 3
    assert edits[0].rich_message.markdown == "Still working on it…"
    assert edits[1].rich_message.markdown == "Slow but done"
    assert edits[2].rich_message.markdown == "Second answer"


def _guest_payload_text(event_text):
    """The JSON guest payload embedded in the prompt handed to the turn."""
    import json

    return json.loads(event_text[event_text.index("{"):event_text.rindex("}") + 1])


@pytest.mark.asyncio
async def test_guest_long_question_reaches_the_turn_uncut_to_32000():
    """A guest question longer than 6,000 characters is not cut there: the
    guest result carries 32,000, so the question is cut at 32,000 too."""
    seen = []

    async def handle(event):
        seen.append(event.text)
        return "ok"

    adapter = _guest_adapter(_GuestClient(), handle)
    await adapter._on_guest_chat_query(_guest_update(text="q" * 8000))

    assert len(seen) == 1
    assert len(_guest_payload_text(seen[0])["request_text"]) == 8000


@pytest.mark.asyncio
async def test_guest_question_over_32000_is_cut_at_32000():
    """A question past the guest-result limit is cut at 32,000, never more."""
    seen = []

    async def handle(event):
        seen.append(event.text)
        return "ok"

    adapter = _guest_adapter(_GuestClient(), handle)
    await adapter._on_guest_chat_query(_guest_update(text="q" * 40000))

    assert len(seen) == 1
    assert len(_guest_payload_text(seen[0])["request_text"]) == 32000
