"""Telegram clarify polls: a single-select clarify goes out as a native poll,
the first vote from the configured respondent resolves it and stops the poll,
and votes from anyone else are ignored.

Vote observability: every vote update leaves a trace — an entry line on
receipt and a line naming the branch at every early return — so the host log
can tell "the vote never arrived" apart from "it arrived but was dropped".
A vote that carries no usable position still resolves through its option
bytes (current and legacy forms); a vote whose position points nowhere falls
back to the option bytes instead of being dropped silently.

No Telegram connections or real sends: the MTProto transport is a fake that
serializes the real Telethon requests and returns the sent message.
"""
import logging

import pytest
from telethon import events, functions, types
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tools import clarify_gateway as cm

OWNER_ID = "100"
OTHER_ID = "200"
CHAT_ID = "-1000000000042"
THREAD_ID = "77"
RESPONDENT = OWNER_ID


class _FakeRichTransport:
    """Serializes real Telethon requests; returns the sent message like the server would."""

    def __init__(self):
        self.requests: list = []
        self.next_message_id = 100
        # When set, the echoed message carries this poll id instead of the
        # client-generated one in the request — the server assigns its own.
        self.server_poll_id = None
        # When False, the send returns no updates at all (a short update).
        self.echo_updates = True

    async def rich_request(self, request):
        assert bytes(request)
        self.requests.append(request)
        if isinstance(request, functions.messages.SendMediaRequest):
            if not self.echo_updates:
                return SimpleNamespace(updates=[])
            self.next_message_id += 1
            sent_poll = request.media.poll
            if self.server_poll_id is not None:
                sent_poll = types.Poll(id=self.server_poll_id, question=sent_poll.question,
                                       answers=sent_poll.answers, hash=getattr(sent_poll, "hash", 0),
                                       public_voters=sent_poll.public_voters,
                                       multiple_choice=sent_poll.multiple_choice,
                                       quiz=sent_poll.quiz,
                                       close_period=getattr(sent_poll, "close_period", None))
            media = types.MessageMediaPoll(
                poll=sent_poll,
                results=types.PollResults(results=[], total_voters=0))
            reply = getattr(request, "reply_to", None)
            message = types.Message(
                id=self.next_message_id, peer_id=types.PeerChannel(42), message="",
                media=media, date=None, out=True,
                reply_to=types.MessageReplyHeader(
                    reply_to_msg_id=reply.reply_to_msg_id,
                    reply_to_top_id=getattr(reply, "top_msg_id", None),
                    forum_topic=bool(getattr(reply, "top_msg_id", None))) if reply else None)
            return SimpleNamespace(updates=[SimpleNamespace(message=message)])
        if isinstance(request, functions.messages.EditMessageRequest):
            return SimpleNamespace(updates=[])
        return SimpleNamespace(updates=[])


@pytest.fixture
def transport(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": False}))
    adapter._bot_id = 900
    adapter._bot_username = "HermesPrivateBot"
    adapter._client = SimpleNamespace()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT_ID, chat_type="group",
                           user_id=OWNER_ID, thread_id=THREAD_ID, message_id="12")
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerChannel(42, 123))
    fake = _FakeRichTransport()
    adapter._mtproto_rich_request = fake.rich_request
    adapter.send = AsyncMock()
    adapter._observe_telegram_message(source, SimpleNamespace(id=12))
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
        cm._polls.clear()
        cm._poll_index.clear()
    return adapter, source, fake


def _send_kwargs(clarify_id, question, choices):
    return {"chat_id": CHAT_ID, "question": question, "choices": choices,
            "clarify_id": clarify_id, "session_key": "sk1",
            "metadata": {"thread_id": THREAD_ID}}


def _question(raw):
    return raw + "\nReply \"skip\" to skip this question."


def _poll_request(fake):
    return next(r for r in fake.requests if isinstance(r, functions.messages.SendMediaRequest))


def _edit_requests(fake):
    return [r for r in fake.requests if isinstance(r, functions.messages.EditMessageRequest)]


@pytest.mark.asyncio
async def test_single_select_clarify_sends_non_anonymous_poll_with_footer(transport):
    adapter, _source, fake = transport
    entry = cm.register("c1", "sk1", "Which VC should I join?",
                        ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        "c1", _question("Which VC should I join?"), ["Testing Agents", "Test3", "Both"]))
    assert result.success and result.message_id
    request = _poll_request(fake)
    assert request.media.poll.question.text == (
        "❓ Which VC should I join?\n\n—\nReply to this poll to send your answer.")
    assert request.message == ""
    assert [a.text.text for a in request.media.poll.answers] == ["Testing Agents", "Test3", "Both"]
    assert [a.option for a in request.media.poll.answers] == [bytes([0]), bytes([1]), bytes([2])]
    assert request.media.poll.public_voters is True  # non-anonymous: votes reach the bot
    assert request.media.poll.multiple_choice is False
    assert request.media.poll.revoting_disabled is True
    assert request.media.poll.close_period == cm.get_clarify_timeout()
    poll = cm.get_clarify_poll("c1")
    assert poll is not None
    assert poll.poll_id == request.media.poll.id
    assert poll.message_id == int(result.message_id)
    assert poll.chat_id == CHAT_ID and poll.thread_id == THREAD_ID
    assert entry.awaiting_text is True  # typed replies stay open as the custom-answer path
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_question_puts_footer_in_description(transport):
    adapter, _source, fake = transport
    cm.register("c2", "sk1", "Q?", ["A", "B"], respondent_id=RESPONDENT)
    long_q = "x" * 230
    result = await adapter.send_clarify(**_send_kwargs("c2", _question(long_q), ["A", "B"]))
    assert result.success
    request = _poll_request(fake)
    # Question alone fits (255), question + footer does not: the footer rides as the caption.
    assert request.media.poll.question.text == "❓ " + long_q
    assert request.message == "\n\n—\nReply to this poll to send your answer."


@pytest.mark.asyncio
async def test_multi_select_clarify_stays_text_card(transport):
    adapter, _source, fake = transport
    entry = cm.register("c3", "sk1", "Pick some", ["A", "B"],
                        multi_select=True, respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        "c3", _question("Pick some"), ["A", "B"]))
    assert result.success
    assert not [r for r in fake.requests if isinstance(r, functions.messages.SendMediaRequest)]
    assert cm.get_clarify_poll("c3") is None
    adapter.send.assert_awaited_once()
    text = adapter.send.await_args.kwargs["content"]
    assert "1. A" in text and "2. B" in text
    assert entry.awaiting_text is True


@pytest.mark.asyncio
async def test_oversize_question_falls_back_to_text_card(transport):
    adapter, _source, fake = transport
    cm.register("c4", "sk1", "Q?", ["A", "B"], respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        "c4", _question("y" * 300), ["A", "B"]))
    assert result.success
    assert not [r for r in fake.requests if isinstance(r, functions.messages.SendMediaRequest)]
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_create_return_carries_poll_id(transport):
    adapter, source, fake = transport
    ops = adapter._client_operations_ui().operations
    result = await ops.request(source, "poll_create",
                               {"question": "Pick one", "options": ["A", "B"]})
    assert result["ok"] and result["status"] == "complete", result
    assert result["data"]["poll_id"] == _poll_request(fake).media.poll.id


# The poll id Telegram echoed back for the live 2026-11-19 vote that was
# dropped as "unknown poll": the registry held the client-generated id.
SERVER_POLL_ID = 6336838466990506982


@pytest.mark.asyncio
async def test_poll_create_returns_server_echoed_poll_id(transport):
    adapter, source, fake = transport
    fake.server_poll_id = SERVER_POLL_ID
    ops = adapter._client_operations_ui().operations
    result = await ops.request(source, "poll_create",
                               {"question": "Pick one", "options": ["A", "B"]})
    assert result["ok"] and result["status"] == "complete", result
    client_id = _poll_request(fake).media.poll.id
    assert client_id != SERVER_POLL_ID
    assert result["data"]["poll_id"] == SERVER_POLL_ID


@pytest.mark.asyncio
async def test_poll_create_falls_back_to_client_poll_id_without_echo(transport):
    adapter, source, fake = transport
    fake.echo_updates = False
    ops = adapter._client_operations_ui().operations
    result = await ops.request(source, "poll_create",
                               {"question": "Pick one", "options": ["A", "B"]})
    assert result["ok"] and result["status"] == "complete", result
    assert result["data"]["message_id"] is None
    assert result["data"]["poll_id"] == _poll_request(fake).media.poll.id


@pytest.mark.asyncio
async def test_vote_on_server_echoed_poll_id_resolves(transport, caplog):
    adapter, _source, fake = transport
    fake.server_poll_id = SERVER_POLL_ID
    entry = cm.register("c10", "sk1", "Which VC should I join?",
                        ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    caplog.set_level(logging.INFO, logger=_VOTE_LOG)
    result = await adapter.send_clarify(**_send_kwargs(
        "c10", _question("Which VC should I join?"), ["Testing Agents", "Test3", "Both"]))
    assert result.success
    poll = cm.get_clarify_poll("c10")
    assert poll is not None
    assert poll.poll_id == SERVER_POLL_ID
    assert "Clarify poll registered for clarify c10" in caplog.text
    assert str(SERVER_POLL_ID) in caplog.text
    # A vote carrying the client-generated id is unknown: the registry keys
    # on the echoed id, so the poll stays open.
    client_id = _poll_request(fake).media.poll.id
    assert client_id != SERVER_POLL_ID
    await adapter._on_poll_vote(_vote(client_id, RESPONDENT, [1]))
    assert entry.response is None
    assert not entry.event.is_set()
    assert cm.get_clarify_poll("c10") is poll
    # The same vote carrying the server-echoed id resolves.
    await adapter._on_poll_vote(_vote(SERVER_POLL_ID, RESPONDENT, [1]))
    assert entry.response == "Test3"
    assert entry.event.is_set()
    assert cm.get_clarify_poll("c10") is None


async def _send_poll(transport, clarify_id="c5"):
    adapter, _source, fake = transport
    entry = cm.register(clarify_id, "sk1", "Which VC should I join?",
                        ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        clarify_id, _question("Which VC should I join?"), ["Testing Agents", "Test3", "Both"]))
    assert result.success
    return adapter, fake, entry, cm.get_clarify_poll(clarify_id)


def _vote(poll_id, user_id, positions, options=None):
    return types.UpdateMessagePollVote(
        poll_id=poll_id, peer=types.PeerUser(int(user_id)),
        options=list(options) if options is not None else [bytes([p]) for p in positions],
        positions=list(positions), qts=1)


@pytest.mark.asyncio
async def test_vote_from_respondent_resolves_and_stops_poll(transport):
    adapter, fake, entry, poll = await _send_poll(transport)
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, [1]))
    assert entry.response == "Test3"
    assert entry.event.is_set()
    edits = _edit_requests(fake)
    assert len(edits) == 1
    assert edits[0].id == poll.message_id
    assert edits[0].media.poll.closed is True
    assert cm.get_clarify_poll("c5") is None


@pytest.mark.asyncio
async def test_vote_from_other_user_is_ignored_poll_stays_open(transport):
    adapter, fake, entry, poll = await _send_poll(transport)
    await adapter._on_poll_vote(_vote(poll.poll_id, OTHER_ID, [0]))
    assert entry.response is None
    assert not entry.event.is_set()
    assert _edit_requests(fake) == []
    assert cm.get_clarify_poll("c5") is poll


@pytest.mark.asyncio
async def test_retracted_vote_is_ignored(transport):
    adapter, fake, entry, poll = await _send_poll(transport)
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, [], options=[]))
    assert entry.response is None
    assert _edit_requests(fake) == []
    assert cm.get_clarify_poll("c5") is poll


@pytest.mark.asyncio
async def test_retire_stops_poll_and_unknown_clarify_is_a_noop(transport):
    adapter, fake, _entry, _poll = await _send_poll(transport)
    await adapter.retire_clarify_card("c5", "expired")
    edits = _edit_requests(fake)
    assert len(edits) == 1 and edits[0].media.poll.closed is True
    assert cm.get_clarify_poll("c5") is None
    await adapter.retire_clarify_card("never-registered", "expired")
    assert len(_edit_requests(fake)) == 1  # no second stop attempted


@pytest.mark.asyncio
async def test_poll_replies_to_triggering_message_id(transport):
    adapter, _source, fake = transport
    cm.register("c6", "sk1", "Which VC should I join?",
                ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    kwargs = _send_kwargs("c6", _question("Which VC should I join?"),
                          ["Testing Agents", "Test3", "Both"])
    kwargs["metadata"] = {"thread_id": THREAD_ID, "clarify_reply_to_message_id": "12"}
    result = await adapter.send_clarify(**kwargs)
    assert result.success
    request = _poll_request(fake)
    assert request.reply_to is not None
    assert request.reply_to.reply_to_msg_id == 12  # the triggering message
    assert request.reply_to.top_msg_id == int(THREAD_ID)  # still in the thread
    assert request.message == ""  # no tag needed: the reply already addresses the user


@pytest.mark.asyncio
async def test_poll_replies_to_observed_source_message_without_metadata_anchor(transport):
    adapter, _source, fake = transport
    cm.register("c7", "sk1", "Which VC should I join?",
                ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        "c7", _question("Which VC should I join?"), ["Testing Agents", "Test3", "Both"]))
    assert result.success
    request = _poll_request(fake)
    # No runner-stamped anchor: the last observed thread message (id 12) is the anchor.
    assert request.reply_to.reply_to_msg_id == 12
    assert request.reply_to.top_msg_id == int(THREAD_ID)


def _observe_no_id_source(adapter, user_name=None):
    """Re-observe the thread's source with no message id, so the poll cannot reply."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT_ID, chat_type="group",
                           user_id=OWNER_ID, user_name=user_name, thread_id=THREAD_ID,
                           message_id=None)
    adapter._observe_telegram_message(source, SimpleNamespace(id=None))
    return source


@pytest.mark.asyncio
async def test_poll_without_message_id_tags_respondent_by_name(transport):
    adapter, _source, fake = transport
    _observe_no_id_source(adapter)
    cm.register("c8", "sk1", "Which VC should I join?",
                ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    kwargs = _send_kwargs("c8", _question("Which VC should I join?"),
                          ["Testing Agents", "Test3", "Both"])
    kwargs["metadata"] = {"thread_id": THREAD_ID, "respondent_name": "Kailash"}
    result = await adapter.send_clarify(**kwargs)
    assert result.success
    request = _poll_request(fake)
    # No message to reply to: the poll lands on the thread root and tags the user.
    assert request.reply_to.reply_to_msg_id == int(THREAD_ID)
    assert request.message == (
        "Kailash — Hermes wants the answer from you. Only your message/vote is needed; "
        "this poll expires in 10 minutes.")
    assert request.entities == [types.MessageEntityMentionName(offset=0, length=7, user_id=100)]


@pytest.mark.asyncio
async def test_poll_without_message_id_or_name_tags_bare_you(transport):
    adapter, _source, fake = transport
    _observe_no_id_source(adapter)
    cm.register("c9", "sk1", "Which VC should I join?",
                ["Testing Agents", "Test3", "Both"], respondent_id=RESPONDENT)
    result = await adapter.send_clarify(**_send_kwargs(
        "c9", _question("Which VC should I join?"), ["Testing Agents", "Test3", "Both"]))
    assert result.success
    request = _poll_request(fake)
    assert request.message.startswith("you — Hermes wants the answer from you.")
    assert request.entities == [types.MessageEntityMentionName(offset=0, length=3, user_id=100)]


@pytest.mark.asyncio
async def test_vote_from_respondent_resolves_and_deletes_poll(transport):
    adapter, fake, entry, poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, [1]))
    assert entry.response == "Test3"
    assert len(_edit_requests(fake)) == 1  # poll stopped
    adapter._client.delete_messages.assert_awaited_once()
    assert adapter._client.delete_messages.await_args.args[1] == [poll.message_id]
    assert cm.get_clarify_poll("c5") is None


@pytest.mark.asyncio
async def test_retire_deletes_poll_message(transport):
    adapter, fake, _entry, poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    await adapter.retire_clarify_card("c5", "expired")
    assert len(_edit_requests(fake)) == 1
    adapter._client.delete_messages.assert_awaited_once()
    assert adapter._client.delete_messages.await_args.args[1] == [poll.message_id]
    assert cm.get_clarify_poll("c5") is None


@pytest.mark.asyncio
async def test_stale_vote_stops_and_deletes_poll(transport):
    adapter, fake, entry, poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    cm.resolve_gateway_clarify(entry.clarify_id, "Testing Agents")  # entry gone, poll record stays
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, [0]))
    assert len(_edit_requests(fake)) == 1
    adapter._client.delete_messages.assert_awaited_once()
    assert adapter._client.delete_messages.await_args.args[1] == [poll.message_id]
    assert cm.get_clarify_poll("c5") is None


def test_vote_handler_registered_for_poll_vote_updates(transport):
    adapter, _source, _fake = transport
    calls: list = []
    adapter._client = SimpleNamespace(
        add_event_handler=lambda cb, ev: calls.append((cb, ev)))
    adapter._handlers_registered = False
    adapter._register_handlers(events)
    vote_handlers = [(cb, ev) for cb, ev in calls
                     if getattr(ev, "types", None) is types.UpdateMessagePollVote]
    assert len(vote_handlers) == 1
    assert vote_handlers[0][0] == adapter._on_poll_vote


# --- Vote observability: every vote leaves a trace -----------------------

_VOTE_LOG = "plugins.platforms.telegram.operations_routing"


@pytest.mark.asyncio
async def test_resolving_vote_logs_entry_and_resolution(transport, caplog):
    adapter, _fake, entry, poll = await _send_poll(transport)
    caplog.set_level(logging.INFO, logger=_VOTE_LOG)
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, [1]))
    assert entry.response == "Test3"
    assert "vote received" in caplog.text
    assert "resolved clarify" in caplog.text
    assert str(poll.poll_id) in caplog.text


@pytest.mark.parametrize("case,needle", [
    ("unknown_poll", "unknown poll"),
    ("other_user", "respondent is"),
    ("retracted", "no usable choice"),
    ("out_of_range", "no usable choice"),
    ("no_peer", "could not be resolved"),
])
@pytest.mark.asyncio
async def test_dropped_vote_leaves_a_named_log_line(transport, caplog, case, needle):
    adapter, _fake, entry, poll = await _send_poll(transport)
    caplog.set_level(logging.INFO, logger=_VOTE_LOG)
    if case == "unknown_poll":
        update = _vote(987654321, RESPONDENT, [0])
        needle = "987654321"
    elif case == "other_user":
        update = _vote(poll.poll_id, OTHER_ID, [0])
    elif case == "retracted":
        update = _vote(poll.poll_id, RESPONDENT, [], options=[])
    elif case == "out_of_range":
        update = _vote(poll.poll_id, RESPONDENT, [9], options=[])
    else:  # no_peer
        update = types.UpdateMessagePollVote(
            poll_id=poll.poll_id, peer=None, options=[], positions=[], qts=1)
    await adapter._on_poll_vote(update)
    assert entry.response is None
    assert not entry.event.is_set()
    assert needle in caplog.text
    if case == "other_user":
        assert OTHER_ID in caplog.text
    assert cm.get_clarify_poll("c5") is poll  # dropped votes leave the poll open


@pytest.mark.asyncio
async def test_vote_with_unusable_poll_id_is_logged(transport, caplog):
    adapter, _fake, entry, _poll = await _send_poll(transport)
    caplog.set_level(logging.WARNING, logger=_VOTE_LOG)
    await adapter._on_poll_vote(SimpleNamespace(poll_id="not-a-poll-id",
                                                peer=types.PeerUser(100)))
    assert entry.response is None
    assert "unusable poll id" in caplog.text


@pytest.mark.parametrize("positions,options,expected", [
    ([], [bytes([2])], "Both"),        # current option-byte form
    ([], [bytes([65 + 2])], "Both"),   # legacy option-byte form (older polls)
    ([9], [bytes([1])], "Test3"),      # position points nowhere: options decide
])
@pytest.mark.asyncio
async def test_vote_without_usable_position_resolves_via_option_bytes(
        transport, positions, options, expected):
    adapter, _fake, entry, poll = await _send_poll(transport)
    await adapter._on_poll_vote(_vote(poll.poll_id, RESPONDENT, positions, options=options))
    assert entry.response == expected
    assert entry.event.is_set()
    assert cm.get_clarify_poll("c5") is None


@pytest.mark.asyncio
async def test_retire_stop_failure_is_warned(transport, caplog, monkeypatch):
    adapter, _fake, _entry, _poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    caplog.set_level(logging.WARNING, logger=_VOTE_LOG)

    class _ExplodingUI:
        async def request(self, *args, **kwargs):
            raise RuntimeError("poll stop exploded")

    monkeypatch.setattr(adapter, "_client_operations_ui", lambda: _ExplodingUI())
    await adapter.retire_clarify_card("c5", "expired")
    assert "stop failed" in caplog.text
    assert cm.get_clarify_poll("c5") is None  # record dropped either way


@pytest.mark.asyncio
async def test_retire_delete_failure_is_warned(transport, caplog):
    adapter, _fake, _entry, _poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock(side_effect=RuntimeError("delete exploded"))
    caplog.set_level(logging.WARNING, logger=_VOTE_LOG)
    await adapter.retire_clarify_card("c5", "expired")
    assert "delete failed" in caplog.text
    assert cm.get_clarify_poll("c5") is None  # record dropped either way


@pytest.mark.asyncio
async def test_retire_logs_summary_with_stop_and_delete_outcomes(transport, caplog):
    adapter, _fake, _entry, _poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    caplog.set_level(logging.INFO, logger=_VOTE_LOG)
    await adapter.retire_clarify_card("c5", "expired")
    assert "Clarify poll retired for clarify c5" in caplog.text
    assert "stop=ok delete=ok" in caplog.text


@pytest.mark.asyncio
async def test_retire_with_no_observed_source_warns_stop_skipped(transport, caplog):
    adapter, _fake, _entry, _poll = await _send_poll(transport)
    adapter._client.delete_messages = AsyncMock()
    adapter._telegram_observed_sources.clear()  # the send's source is gone
    caplog.set_level(logging.WARNING, logger=_VOTE_LOG)
    await adapter.retire_clarify_card("c5", "expired")
    assert "stop skipped" in caplog.text
    assert cm.get_clarify_poll("c5") is None  # record dropped either way


@pytest.mark.asyncio
async def test_retire_unknown_clarify_logs_skip(transport, caplog):
    adapter, _fake, _entry, _poll = await _send_poll(transport)
    caplog.set_level(logging.INFO, logger=_VOTE_LOG)
    await adapter.retire_clarify_card("never-registered", "expired")
    assert "Clarify retire skipped for never-registered" in caplog.text
