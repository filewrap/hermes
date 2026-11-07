"""Native Telegram operation coverage. No network clients or real profile credentials."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.client_operations import TelegramClientOperations
from plugins.platforms.telegram.client_operations_ui import TelegramClientOperationsUI, CONFIRM_TTL
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class FakeAdapter:
    def __init__(self, home: Path):
        self.home, self.allowed, self.owner = home, True, True
        self.requests = []
        self._client = SimpleNamespace(get_input_entity=self.get_input_entity,
                                       get_messages=AsyncMock(return_value=[]),
                                       download_media=self.download_media)
        self.peer = types.InputPeerChannel(channel_id=321, access_hash=987654321)
        self.next_message_id = 100

    async def get_input_entity(self, user_id):
        user_id = getattr(user_id, "user_id", user_id)
        return types.InputPeerUser(user_id=int(user_id), access_hash=98765)

    async def download_media(self, message, file, progress_callback=None):
        if progress_callback:
            await progress_callback(10, 10)
        Path(file).write_bytes(b"file-bytes")
        return file

    def _authorized(self, source):
        return self.allowed and not getattr(source, "is_bot", False)

    def _is_owner_user(self, user_id, source):
        return self.owner and str(user_id) == "7"

    def _settings_home(self, source):
        return self.home

    _message_thread_id = staticmethod(TelethonAdapter._message_thread_id)

    async def _input_peer_for(self, chat_id):
        assert chat_id == "-1000000000321"
        return self.peer

    def _sent_message_id(self, response, *, random_id=None, peer=None, topic_id=None):
        from plugins.platforms.telegram.topic_delivery import sent_message_id

        return sent_message_id(response, random_id=random_id, peer=peer, topic_id=topic_id)

    async def _mtproto_rich_request(self, request):
        # Exercise the installed Telethon serializer, not a fabricated RPC vocabulary.
        assert bytes(request)
        self.requests.append(request)
        if isinstance(request, (functions.messages.SendMediaRequest, functions.messages.SendMessageRequest)):
            self.next_message_id += 1
            media = types.MessageMediaPoll(poll=request.media.poll,
                results=types.PollResults(results=[], total_voters=0)) if isinstance(request, functions.messages.SendMediaRequest) else None
            reply = getattr(request, "reply_to", None)
            message = types.Message(id=self.next_message_id, peer_id=types.PeerChannel(321),
                                    message="", media=media, date=None, out=True,
                                    reply_to=types.MessageReplyHeader(
                                        reply_to_msg_id=reply.reply_to_msg_id,
                                        reply_to_top_id=getattr(reply, "top_msg_id", None),
                                        forum_topic=bool(getattr(reply, "top_msg_id", None))) if reply else None)
            return SimpleNamespace(updates=[SimpleNamespace(message=message)])
        if isinstance(request, functions.bots.GetBotInfoRequest):
            return types.bots.BotInfo(name="Hermes", about="Bot about", description="Bot description")
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(users=[SimpleNamespace(id=request.id.user_id, first_name="Owner",
                last_name=None, username="owner", phone="not-exposed", bot=False)],
                full_user=SimpleNamespace(about="Visible about"))
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(chats=[SimpleNamespace(id=321, title="Current group", username=None)],
                                   users=[], full_chat=SimpleNamespace(about="Group about", participants_count=4))
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return SimpleNamespace(topics=[SimpleNamespace(id=t, title=f"Topic {t}", closed=False, pinned=False)
                                           for t in request.topics])
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            return SimpleNamespace(topics=[SimpleNamespace(id=i, title=f"Topic {i}", closed=False, pinned=i == 1)
                                           for i in range(1, request.limit + 1)])
        return SimpleNamespace(updates=[])


def source(*, user_id="7", chat_id="-1000000000321", thread_id=None, profile="alpha", is_bot=False):
    return SessionSource(platform=Platform.TELEGRAM, user_id=user_id, chat_id=chat_id,
                         thread_id=thread_id, profile=profile, is_bot=is_bot,
                         chat_type="group", message_id="55")


def poll_message(message_id=6, *, thread=None):
    poll = types.Poll(id=887, question=types.TextWithEntities(text="Q?", entities=[]),
        answers=[types.PollAnswer(text=types.TextWithEntities(text="A", entities=[]), option=b"A"),
                 types.PollAnswer(text=types.TextWithEntities(text="B", entities=[]), option=b"B")], hash=12)
    return types.Message(id=message_id, peer_id=types.PeerChannel(321), message="", date=None,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=int(thread), reply_to_top_id=int(thread), forum_topic=True) if thread else None,
        media=types.MessageMediaPoll(poll=poll, results=types.PollResults(total_voters=4,
            results=[types.PollAnswerVoters(option=b"B", voters=3), types.PollAnswerVoters(option=b"A", voters=1)])))


def callback(message_id, answer=None):
    return SimpleNamespace(message_id=message_id, answer=answer or AsyncMock())


def _walk_tl(node):
    yield node
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk_tl(child)
    elif isinstance(node, types.TLObject):
        for child in vars(node).values():
            if isinstance(child, (types.TLObject, list, tuple)):
                yield from _walk_tl(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("quiz", [False, True])
async def test_poll_send_uses_real_request_serializes_and_caches_outgoing_poll(tmp_path, quiz):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    params = {"question": "Pick a color", "options": ["Red", "Blue"], "anonymous": False, "quiz": quiz}
    if quiz:
        params.update(correct_answers=[1], solution="Blue is correct")
    pending = await ops.request(src, "poll_create", params)
    assert pending["status"] == "confirmation_required" and not adapter.requests
    result = await ops.execute_confirmed(src, "poll_create", pending["params"])
    request = adapter.requests[-1]
    assert result["ok"] and isinstance(request, functions.messages.SendMediaRequest)
    assert request.media.poll.question.text == "Pick a color"
    assert [answer.text.text for answer in request.media.poll.answers] == ["Red", "Blue"]
    assert [answer.option for answer in request.media.poll.answers] == [bytes([0]), bytes([1])]
    assert request.media.correct_answers == ([1] if quiz else None)
    assert ops._observed_messages(src)[0].id == result["data"]["message_id"]
    assert (await ops.request(src, "poll_results", {"message_id": result["data"]["message_id"]}))["ok"]


@pytest.mark.asyncio
async def test_poll_results_are_cached_counts_matched_by_option_never_user_only_rpc(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, poll_message())
    result = await ops.request(src, "poll_results", {"message_id": 6})
    assert [row["voters"] for row in result["data"]["answers"]] == [1, 3]
    assert result["data"]["total_voters"] == 4
    assert not adapter.requests
    update = types.UpdateMessagePoll(poll_id=887, msg_id=6,
        results=types.PollResults(total_voters=8, results=[types.PollAnswerVoters(option=b"A", voters=8)]))
    assert ops.observe_poll_update(src, update)
    assert not ops.observe_poll_update(source(thread_id="9"), update)
    assert (await ops.request(src, "poll_results", {"message_id": 6}))["data"]["total_voters"] == 8


@pytest.mark.asyncio
async def test_poll_stop_serializes_and_updates_cached_state(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, poll_message())
    result = await ops.execute_confirmed(src, "poll_stop", {"message_id": 6})
    assert result["ok"] and isinstance(adapter.requests[-1], functions.messages.EditMessageRequest)
    assert adapter.requests[-1].media.poll.closed is True
    assert (await ops.request(src, "poll_results", {"message_id": 6}))["data"]["closed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"quiz": True}, {"quiz": True, "correct_answers": [8]},
    {"quiz": True, "correct_answers": [0, 0]}, {"quiz": True, "correct_answers": "1"},
    {"correct_answers": [1]}, {"correct_option": 1}, {"anonymous": "false"},
    {"options": [f"opt {i}" for i in range(13)]},
    {"description": "x" * 1025}, {"open_period": 4}, {"open_period": 5, "close_date": int(time.time()) + 60},
    {"close_date": int(time.time()) + 4}, {"country_codes": ["USA"]},
    {"country_codes": [f"C{i}" for i in range(13)]},
    {"allow_adding_options": True}, {"quiz": True, "allow_adding_options": True, "correct_answers": [0]},
    {"revoting": "yes"}])
async def test_invalid_poll_settings_fail_before_confirmation(tmp_path, params):
    adapter = FakeAdapter(tmp_path)
    result = await TelegramClientOperations(adapter).request(source(), "poll_create",
        {"question": "Q?", "options": ["A", "B"], **params})
    assert not result["ok"] and not adapter.requests


@pytest.mark.asyncio
async def test_poll_create_twelve_options_description_quiz_multiple_answers(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    options = [f"Option {i}" for i in range(12)]
    params = {"question": "Pick all that apply", "options": options,
              "description": "Backlog, release plans, what to do tomorrow.",
              "quiz": True, "multiple_choice": True, "correct_answers": [0, 2],
              "solution": "First and third", "open_period": 3600,
              "shuffle_options": True, "country_codes": ["IN", "US"]}
    pending = await ops.request(src, "poll_create", params)
    assert pending["ok"] and pending["status"] == "confirmation_required"
    result = await ops.execute_confirmed(src, "poll_create", pending["params"])
    assert result["ok"]
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert len(request.media.poll.answers) == 12
    assert request.message == "Backlog, release plans, what to do tomorrow."
    assert request.media.poll.quiz is True
    assert request.media.poll.multiple_choice is True
    assert request.media.correct_answers == [0, 2]
    assert request.media.poll.close_period == 3600
    assert request.media.poll.shuffle_answers is True
    assert request.media.poll.revoting_disabled is True  # quizzes default to no revoting
    assert request.media.poll.countries_iso2 == ["IN", "US"]


@pytest.mark.asyncio
async def test_poll_stop_preserves_poll_flags(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    poll = types.Poll(id=888, question=types.TextWithEntities(text="Q?", entities=[]),
        answers=[types.PollAnswer(text=types.TextWithEntities(text="A", entities=[]), option=bytes([0]))],
        hash=12, multiple_choice=True, open_answers=True, shuffle_answers=True,
        hide_results_until_close=True, close_period=600, countries_iso2=["IN"])
    ops.observe(src, types.Message(id=6, peer_id=types.PeerChannel(321), message="", date=None,
        media=types.MessageMediaPoll(poll=poll, results=types.PollResults(total_voters=0, results=[]))))
    result = await ops.execute_confirmed(src, "poll_stop", {"message_id": 6})
    assert result["ok"]
    closed = adapter.requests[-1].media.poll
    assert closed.closed is True
    assert closed.multiple_choice is True
    assert closed.open_answers is True
    assert closed.shuffle_answers is True
    assert closed.hide_results_until_close is True
    assert closed.close_period == 600
    assert closed.countries_iso2 == ["IN"]


@pytest.mark.asyncio
async def test_rich_send_uses_native_actions_and_preserves_current_chat_topic(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(user_id="8", thread_id="44")
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="member", sender_id=8))
    result = await ops.request(src, "rich_message_send", {
        "content": "Read [docs](https://example.com/guide), then run `/start`.\n\n```bash\nprintf ready\n```",
        "format": "markdown",
        "buttons": [
            {"type": "url", "label": "Docs", "value": "https://example.com"},
            {"type": "copy", "label": "Copy value", "value": "clipboard value"},
            {"type": "profile", "label": "Member", "value": "8"},
        ],
    })
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert request.peer == adapter.peer
    assert request.reply_to.reply_to_msg_id == 55 and request.reply_to.top_msg_id == 44
    nodes = list(_walk_tl(request.rich_message))
    assert any(isinstance(node, types.TextUrl) and node.url == "https://example.com/guide" for node in nodes)
    assert any(isinstance(node, types.TextButton) and isinstance(node.type, types.InlineButtonTypeCopy)
               and node.type.copy_text == "/start" for node in nodes)
    rich_buttons = [node for node in nodes if isinstance(node, types.PageButton)]
    assert any(isinstance(node.type, types.InlineButtonTypeUrl) and node.type.url == "https://example.com"
               for node in rich_buttons)
    assert any(isinstance(node.type, types.InlineButtonTypeCopy) and node.type.copy_text == "clipboard value"
               for node in rich_buttons)
    assert any(isinstance(node.type, types.InputInlineButtonTypeUserProfile)
               and node.type.user_id.user_id == 8 for node in rich_buttons)
    assert any(isinstance(node.type, types.InlineButtonTypeCopy) and node.type.copy_text == "printf ready"
               for node in rich_buttons)
    assert result["data"]["button_count"] == 3
    assert len(request._bytes()) > 0


@pytest.mark.asyncio
async def test_rich_send_keeps_html_hrefs_native_without_pagebutton_rows(tmp_path):
    adapter = FakeAdapter(tmp_path)
    content = '<p>Open <a href="https://example.com/docs">the guide</a>.</p>'
    result = await TelegramClientOperations(adapter).request(source(), "rich_message_send", {
        "content": content, "format": "html",
    })
    assert result["ok"]
    rich = adapter.requests[-1].rich_message
    assert isinstance(rich, types.InputRichMessageHTML)
    assert rich.html == content
    assert len(adapter.requests[-1]._bytes()) > 0


@pytest.mark.asyncio
async def test_rich_send_html_buttons_render_as_in_body_pills(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(user_id="8", thread_id="44")
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="member", sender_id=8))
    result = await ops.request(src, "rich_message_send", {
        "content": "<p>Read the guide.</p>", "format": "html",
        "buttons": [
            {"type": "url", "label": "Docs", "value": "https://example.com", "style": "primary"},
            {"type": "copy", "label": "Copy value", "value": "clipboard value"},
            {"type": "profile", "label": "Member", "value": "8"},
        ],
    })
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    html = request.rich_message.html
    assert html.startswith("<p>Read the guide.</p>")
    assert '<tg-button type="url" style="primary" url="https://example.com">Docs</tg-button>' in html
    assert '<tg-button type="copy_text" text="clipboard value">Copy value</tg-button>' in html
    assert '<tg-button type="url" url="tg://user?id=8">Member</tg-button>' in html
    assert "<tg-button-row>" in html and "PageButton" not in html
    assert result["data"]["button_count"] == 3
    assert len(request._bytes()) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"content": "Open", "buttons": [{"type": "callback", "label": "Go", "value": "x"}]},
    {"content": "<p>Open</p>", "format": "html", "buttons": [{"type": "url", "label": "Go", "value": "https://example.com", "style": "link"}]},
    {"content": "Open", "buttons": [{"type": "url", "label": "Go", "value": "https://example.com", "style": "bogus"}]},
    {"content": "Open", "buttons": [{"type": "copy", "label": "Copy", "value": "x" * 257}]},
    {"content": "Open", "buttons": [{"type": "url", "label": "Unsafe", "value": "javascript:alert(1)"}]},
    {"content": "Open", "chat_id": "-10042"},
])
async def test_rich_send_rejects_unsupported_actions_and_routes_before_rpc(tmp_path, params):
    adapter = FakeAdapter(tmp_path)
    result = await TelegramClientOperations(adapter).request(source(), "rich_message_send", params)
    assert not result["ok"] and result["error"]["code"] == "INVALID_ARGUMENT"
    assert not adapter.requests


@pytest.mark.asyncio
async def test_rich_send_profile_button_requires_current_or_observed_sender(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    params = {"content": "Member", "buttons": [{"type": "profile", "label": "Profile", "value": "8"}]}
    denied = await ops.request(src, "rich_message_send", params)
    assert not denied["ok"] and "observed in this exact chat/topic" in denied["error"]["message"]
    assert not adapter.requests
    ops.observe(src, SimpleNamespace(id=9, message="member", sender_id=8))
    assert (await ops.request(src, "rich_message_send", params))["ok"]
    assert adapter.requests
    other_topic = source(thread_id="45")
    other_topic_result = await ops.request(other_topic, "rich_message_send", params)
    assert not other_topic_result["ok"]
    assert "observed in this exact chat/topic" in other_topic_result["error"]["message"]
    assert len(adapter.requests) == 1
    adapter.owner = False
    result = await ops.request(src, "rich_message_send", {"content": "Admitted non-owner reply"})
    assert result["ok"] and result["status"] == "complete"
    assert len(adapter.requests) == 2
    denied_mutation = await ops.request(src, "chat_title", {"title": "No change"})
    assert denied_mutation["error"]["code"] == "FORBIDDEN"
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["not_admitted", "bot", "guest"])
async def test_rich_send_requires_an_admitted_human(tmp_path, rejection):
    adapter, src = FakeAdapter(tmp_path), source(user_id="8")
    adapter.owner = False
    if rejection == "not_admitted":
        adapter.allowed = False
    elif rejection == "bot":
        src.is_bot = True
    else:
        src.is_guest = True
    result = await TelegramClientOperations(adapter).request(src, "rich_message_send", {"content": "No send"})
    assert result["error"]["code"] == "FORBIDDEN"
    assert not adapter.requests


@pytest.mark.asyncio
async def test_rich_send_observed_profile_button_cannot_cross_profile(tmp_path):
    adapter = FakeAdapter(tmp_path)
    adapter._settings_home = lambda src: tmp_path / str(src.profile)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(profile="alpha"), SimpleNamespace(id=9, message="member", sender_id=8))
    result = await ops.request(source(profile="beta"), "rich_message_send", {
        "content": "Member", "buttons": [{"type": "profile", "label": "Profile", "value": "8"}],
    })
    assert not result["ok"]
    assert "observed in this exact chat/topic" in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["chat_info", "bot_profile", "user_info"])
async def test_metadata_read_schemas_are_usable_and_phone_not_exposed(tmp_path, action):
    adapter = FakeAdapter(tmp_path)
    result = await TelegramClientOperations(adapter).request(source(), action)
    assert result["ok"], result
    assert "phone" not in result["data"]
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_user_info_is_current_actor_or_sender_observed_in_exact_scope(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    assert not (await ops.request(source(), "user_info", {"user_id": "8000000000"}))["ok"]
    ops.observe(source(), SimpleNamespace(id=1, message="hello", sender_id=8000000000))
    assert (await ops.request(source(), "user_info", {"user_id": "8000000000"}))["ok"]
    assert not (await ops.request(source(thread_id="9"), "user_info", {"user_id": "8000000000"}))["ok"]


@pytest.mark.asyncio
async def test_bot_profile_update_uses_bot_compatible_method(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    pending = await ops.request(source(), "bot_profile_update", {"about": "The bot's own about"})
    assert (await ops.execute_confirmed(source(), "bot_profile_update", pending["params"]))["ok"]
    request = adapter.requests[-1]
    assert isinstance(request, functions.bots.SetBotInfoRequest)
    assert request.bot is None and request.lang_code == "" and request.about == "The bot's own about"


@pytest.mark.asyncio
async def test_history_search_cursors_do_not_overlap_or_cross_topics(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    for message_id in range(1, 8):
        ops.observe(source(thread_id="500"), SimpleNamespace(id=message_id, message="red widget", sender_id=11))
    first = await ops.request(source(thread_id="500"), "messages_history", {"limit": 3})
    second = await ops.request(source(thread_id="500"), "messages_history",
                               {"limit": 3, "offset_id": first["data"]["next_offset_id"]})
    assert [row["id"] for row in first["data"]["messages"]] == [7, 6, 5]
    assert [row["id"] for row in second["data"]["messages"]] == [4, 3, 2]
    search = await ops.request(source(thread_id="500"), "messages_search", {"query": "red", "limit": 3})
    assert [row["id"] for row in search["data"]["messages"]] == [7, 6, 5]
    assert (await ops.request(source(thread_id="501"), "messages_history", {}))["data"]["messages"] == []
    assert not adapter.requests
    adapter._client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_scope", ["chat", "topic"])
async def test_explicit_message_id_lookup_rejects_telegram_global_id_scope_mismatch(tmp_path, wrong_scope):
    adapter = FakeAdapter(tmp_path)
    msg = types.Message(id=9, peer_id=types.PeerChannel(999 if wrong_scope == "chat" else 321), message="private", date=None)
    adapter._client.get_messages.return_value = [msg]
    ops = TelegramClientOperations(adapter)
    result = await ops.request(source(thread_id="7"), "message_get", {"message_id": 9})
    assert not result["ok"] and not ops._observed_messages(source(thread_id="7"))


@pytest.mark.asyncio
async def test_explicit_message_id_lookup_uses_mtproto_and_caches_only_verified_scope(tmp_path):
    adapter = FakeAdapter(tmp_path)
    adapter._client.get_messages.return_value = [types.Message(id=9, peer_id=types.PeerChannel(321), message="visible", date=None)]
    ops = TelegramClientOperations(adapter)
    result = await ops.request(source(), "message_get", {"message_id": 9})
    assert result["ok"] and result["data"]["text"] == "visible"
    assert len(ops._observed_messages(source())) == 1


@pytest.mark.asyncio
async def test_bot_method_invalid_error_is_precise_and_has_no_fallback(tmp_path):
    adapter = FakeAdapter(tmp_path)
    class BotMethodInvalidError(Exception):
        pass
    adapter._mtproto_rich_request = AsyncMock(side_effect=BotMethodInvalidError("server rejected bot method"))
    result = await TelegramClientOperations(adapter).request(source(), "chat_info")
    assert result["error"]["code"] == "BOT_METHOD_INVALID"
    adapter._mtproto_rich_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["not_admitted", "not_owner", "bot", "guest"])
async def test_mutations_fail_closed_before_any_rpc(tmp_path, rejection):
    adapter, src = FakeAdapter(tmp_path), source()
    if rejection == "not_admitted":
        adapter.allowed = False
    elif rejection == "not_owner":
        adapter.owner = False
    elif rejection == "bot":
        src.is_bot = True
    else:
        src.is_guest = True
    result = await TelegramClientOperations(adapter).request(src, "chat_title", {"title": "No"})
    assert result["error"]["code"] == "FORBIDDEN" and not adapter.requests


@pytest.mark.asyncio
async def test_gateway_log_read_is_owner_only_bounded_redacted_and_pathless(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    secret = "bearer-value-that-must-not-leak-123456789"
    rows = [f"gateway-event-{index}" for index in range(130)]
    rows[-1] = f"Authorization: Bearer {secret}"
    (log_dir / "gateway.log").write_text("\n".join(rows), encoding="utf-8")
    ops = TelegramClientOperations(adapter)

    result = await ops.request(src, "gateway_logs", {"limit": 2})
    assert result["ok"], result
    assert result["data"]["file"] == "gateway.log"
    assert result["data"]["line_count"] == 2 and result["data"]["truncated"]
    assert "gateway-event-128" in result["data"]["text"]
    assert secret not in result["data"]["text"]
    assert len(result["data"]["text"]) <= 16_000

    no_path = await ops.request(src, "gateway_logs", {"path": str(tmp_path / "outside")})
    assert not no_path["ok"] and no_path["error"]["code"] == "VALUEERROR"
    over_limit = await ops.request(src, "gateway_logs", {"limit": 101})
    assert not over_limit["ok"] and over_limit["error"]["code"] == "VALUEERROR"

    adapter.owner = False
    non_owner = source(user_id="8")
    capabilities = await ops.request(non_owner, "capabilities")
    assert "gateway_logs" not in capabilities["data"]["reads"]
    assert "gateway_logs" not in capabilities["data"]["parameters"]
    denied = await ops.request(non_owner, "gateway_logs", {"limit": 2})
    assert denied["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["actor", "chat", "topic", "profile", "message", "expired", "revoked"])
async def test_confirmation_rechecks_all_bindings_without_mutation(tmp_path, mismatch):
    adapter, src = FakeAdapter(tmp_path), source(thread_id="44")
    ui = TelegramClientOperationsUI(adapter)
    result = await ui.request(src, "chat_title", {"title": "Renamed group"})
    token = next(iter(ui._states))
    event = callback(result["confirmation"]["message_id"])
    if mismatch == "actor":
        src.user_id = "8"
    elif mismatch == "chat":
        src.chat_id = "-1000000000999"
    elif mismatch == "topic":
        src.thread_id = "45"
    elif mismatch == "profile":
        adapter.home = tmp_path / "other-profile"
    elif mismatch == "message":
        event.message_id = "99999"
    elif mismatch == "expired":
        ui._states[token]["created_at"] -= CONFIRM_TTL + 1
    else:
        adapter.allowed = False
    assert await ui.handle_callback(event, src, f"tgo:c:{token}:yes")
    assert not any(isinstance(r, functions.channels.EditTitleRequest) for r in adapter.requests)


@pytest.mark.asyncio
async def test_owner_confirmation_consumes_once_and_edits_result_in_place(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(thread_id="44")
    ui = TelegramClientOperationsUI(adapter)
    pending = await ui.request(src, "chat_title", {"title": "Renamed group"})
    token = next(iter(ui._states))
    event = callback(pending["confirmation"]["message_id"])
    # Telegram can omit topic on callbacks: only the exact bound card may restore it.
    assert await ui.handle_callback(event, source(), f"tgo:c:{token}:yes")
    await ui.handle_callback(event, src, f"tgo:c:{token}:yes")
    writes = [r for r in adapter.requests if isinstance(r, functions.channels.EditTitleRequest)]
    assert len(writes) == 1 and writes[0].title == "Renamed group"
    assert isinstance(adapter.requests[-1], functions.messages.EditMessageRequest)
    assert adapter.requests[-1].rich_message is not None


@pytest.mark.asyncio
async def test_profile_changed_during_acknowledgement_cannot_cross_confirmed_boundary(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramClientOperationsUI(adapter)
    pending = await ui.request(source(), "chat_title", {"title": "Renamed group"})
    token = next(iter(ui._states))
    async def switch(*_args, **_kwargs):
        adapter.home = tmp_path / "other-profile"
    await ui.handle_callback(callback(pending["confirmation"]["message_id"], AsyncMock(side_effect=switch)),
                             source(), f"tgo:c:{token}:yes")
    assert not any(isinstance(r, functions.channels.EditTitleRequest) for r in adapter.requests)


@pytest.mark.asyncio
async def test_delete_requires_current_chat_topic_observation_even_at_execution(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    assert not (await ops.request(source(), "messages_delete", {"message_ids": [8]}))["ok"]
    ops.observe(source(thread_id="9"), SimpleNamespace(id=8))
    assert not (await ops.request(source(), "messages_delete", {"message_ids": [8]}))["ok"]
    assert not (await ops.execute_confirmed(source(), "messages_delete", {"message_ids": [8]}))["ok"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_media_download_is_profile_scoped_private_and_atomic(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    doc = types.Document(id=22, access_hash=5, file_reference=b"x", date=None, mime_type="text/plain",
        size=10, dc_id=2, attributes=[types.DocumentAttributeFilename(file_name="../../notes.txt")])
    ops.observe(source(), SimpleNamespace(id=8, message="", sender_id=3, media=types.MessageMediaDocument(document=doc)))
    result = await ops.request(source(), "files_download", {"message_id": 8})
    assert result["ok"], result
    path = Path(result["data"]["cached_path"])
    assert path.parent == tmp_path / "cache" / "telegram-client-operations"
    assert path.read_bytes() == b"file-bytes" and path.stat().st_mode & 0o777 == 0o600
    assert "/../" not in str(path) and not list(path.parent.glob(".telegram-download-*"))


@pytest.mark.asyncio
async def test_unknown_size_download_succeeds_without_application_size_cap(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    doc = types.Document(id=22, access_hash=5, file_reference=b"x", date=None, mime_type="text/plain", size=0, dc_id=2, attributes=[])
    ops.observe(source(), SimpleNamespace(id=8, media=types.MessageMediaDocument(document=doc)))
    async def download_unknown_size(message, file, progress_callback):
        Path(file).write_bytes(b"unbounded-file")
        await progress_callback(len(b"unbounded-file"), None)
    adapter._client.download_media = download_unknown_size
    result = await ops.request(source(), "files_download", {"message_id": 8})
    assert result["ok"], result
    assert Path(result["data"]["cached_path"]).read_bytes() == b"unbounded-file"


def test_pagination_footer_and_destination_numbers_are_always_present(tmp_path):
    ui = TelegramClientOperationsUI(FakeAdapter(tmp_path))
    data = {"ok": True, "action": "messages_history", "data": {"messages": [{"id": x} for x in range(20)], "total": 20}}
    text, markup = ui._render_result(data, page=1, token="bound")
    assert text.endswith("<i>Page 2 of 3 • total 20</i>")
    assert [button.text for row in markup.rows for button in row.buttons] == ["Previous (Page 1)", "Next (Page 3)"]


@pytest.mark.asyncio
async def test_quote_reply_sends_rich_message_with_quote_header_in_topic(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(user_id="8", thread_id="44")
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="The quick brown fox jumps over the lazy dog", sender_id=8))
    result = await ops.request(src, "quote_reply", {
        "message_id": 9, "quote": "brown fox", "content": "Agreed, that part matters.",
        "format": "markdown",
    })
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.reply_to.reply_to_msg_id == 9
    assert request.reply_to.top_msg_id == 44
    assert request.reply_to.quote_text == "brown fox"
    assert request.reply_to.quote_offset == 10
    assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
    assert result["data"]["reply_to_message_id"] == 9
    assert result["data"]["quote"] == "brown fox"
    assert result["data"]["message_id"] is not None
    assert len(request._bytes()) > 0


@pytest.mark.asyncio
async def test_quote_reply_reports_utf16_offset_after_emoji(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="😀 hello world", sender_id=8))
    result = await ops.request(src, "quote_reply", {
        "message_id": 9, "quote": "hello", "content": "Noted.",
    })
    assert result["ok"], result
    # "😀 " is 3 UTF-16 code units (surrogate pair + space), not 2 Python indices.
    assert adapter.requests[-1].reply_to.quote_offset == 3


@pytest.mark.asyncio
async def test_quote_reply_supports_buttons_like_rich_send(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(user_id="8", thread_id="44")
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="Read the guide first", sender_id=8))
    result = await ops.request(src, "quote_reply", {
        "message_id": 9, "quote": "the guide",
        "content": "<p>Done, see the guide.</p>", "format": "html",
        "buttons": [{"type": "copy", "label": "Copy", "value": "guide excerpt"}],
    })
    assert result["ok"], result
    request = adapter.requests[-1]
    assert request.reply_to.quote_text == "the guide"
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert result["data"]["button_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("params,match", [
    ({"message_id": 10, "quote": "brown fox", "content": "Agreed."}, "observed"),
    ({"message_id": 9, "quote": "brown cat", "content": "Agreed."}, "exactly"),
    ({"message_id": 9, "quote": "  ", "content": "Agreed."}, "non-empty"),
    ({"message_id": 9, "quote": "x" * 4097, "content": "Agreed."}, "at most 4,096"),
    ({"quote": "brown fox", "content": "Agreed."}, "message_id"),
    ({"message_id": 9, "quote": "brown fox", "content": "Agreed.", "chat_id": "1"}, "only message_id"),
    ({"message_id": 9, "quote": "brown fox", "content": ""}, "non-empty"),
])
async def test_quote_reply_rejects_bad_target_quote_or_shape_before_rpc(tmp_path, params, match):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="The quick brown fox jumps", sender_id=8))
    result = await ops.request(src, "quote_reply", params)
    assert result["status"] == "error", result
    assert result["error"]["code"] == "INVALID_ARGUMENT", result
    assert match in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_quote_reply_rejects_message_from_another_thread(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(thread_id="44"), SimpleNamespace(id=9, message="Topic message", sender_id=8))
    result = await ops.request(source(thread_id="45"), "quote_reply", {
        "message_id": 9, "quote": "Topic", "content": "Reply.",
    })
    assert result["status"] == "error"
    assert "observed" in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_quote_reply_is_listed_as_a_direct_action(tmp_path):
    adapter = FakeAdapter(tmp_path)
    capabilities = await TelegramClientOperations(adapter).request(source(), "capabilities")
    assert capabilities["ok"], capabilities
    assert "quote_reply" in capabilities["data"]["direct_actions"]
    assert "quote_reply" in capabilities["data"]["parameters"]
    assert "message_id" in capabilities["data"]["parameters"]["quote_reply"]


@pytest.mark.asyncio
async def test_quote_reply_header_is_input_reply_to_message_with_verbatim_excerpt(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="The quick brown fox jumps", sender_id=8))
    result = await ops.request(src, "quote_reply", {"message_id": 9, "quote": "brown fox", "content": "Agreed."})
    assert result["ok"], result
    header = adapter.requests[-1].reply_to
    assert isinstance(header, types.InputReplyToMessage)
    assert header.reply_to_msg_id == 9 and header.quote_text == "brown fox" and header.quote_offset == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("action,unpin", [("pin_message", False), ("unpin_message", True)])
async def test_pin_unpin_confirm_then_send_update_pinned_request(tmp_path, action, unpin):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="pin me", sender_id=8))
    pending = await ops.request(src, action, {"message_id": 9})
    assert pending["status"] == "confirmation_required" and not adapter.requests
    result = await ops.execute_confirmed(src, action, pending["params"])
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.UpdatePinnedMessageRequest)
    assert request.peer == adapter.peer and request.id == 9
    assert request.silent is True and bool(request.unpin) is unpin
    assert result["data"] == {"message_id": 9, "pinned": not unpin}


@pytest.mark.asyncio
async def test_pin_unpin_require_observed_message_and_reject_pm_oneside_outside_dm(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    denied = await ops.request(src, "pin_message", {"message_id": 9})
    assert denied["error"]["code"] == "MESSAGE_SCOPE_INVALID" and not adapter.requests
    assert not (await ops.execute_confirmed(src, "pin_message", {"message_id": 9}))["ok"]
    ops.observe(src, SimpleNamespace(id=9, message="pin me", sender_id=8))
    bad_shape = await ops.request(src, "pin_message", {"message_id": 9, "pm_oneside": True})
    assert bad_shape["error"]["code"] == "INVALID_ARGUMENT" and not adapter.requests
    pending = await ops.request(src, "unpin_message", {"message_id": 9, "pm_oneside": True})
    assert pending["status"] == "confirmation_required"
    refused = await ops.execute_confirmed(src, "unpin_message", pending["params"])
    assert not refused["ok"] and "DM" in refused["error"]["message"]
    assert not adapter.requests
    adapter.owner = False
    assert (await ops.request(src, "pin_message", {"message_id": 9}))["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_big_reaction_confirms_then_sends_big_reaction(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    pending = await ops.request(src, "big_reaction", {"message_id": 9, "emoji": "👍"})
    assert pending["status"] == "confirmation_required" and not adapter.requests
    result = await ops.execute_confirmed(src, "big_reaction", pending["params"])
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.peer == adapter.peer and request.msg_id == 9
    assert request.big is True and request.add_to_recent is True
    assert len(request.reaction) == 1 and isinstance(request.reaction[0], types.ReactionEmoji)
    assert request.reaction[0].emoticon == "👍"
    assert result["data"]["emoji"] == "👍"


@pytest.mark.asyncio
@pytest.mark.parametrize("params,code", [
    ({"message_id": 9, "emoji": "thumbsup"}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": ""}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": "like"}, "INVALID_ARGUMENT"),
    ({"message_id": 10, "emoji": "👍"}, "MESSAGE_SCOPE_INVALID"),
])
async def test_big_reaction_rejects_bad_emoji_or_unobserved_message(tmp_path, params, code):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    result = await ops.request(src, "big_reaction", params)
    assert result["error"]["code"] == code and not adapter.requests


@pytest.mark.asyncio
async def test_ephemeral_send_and_delete_use_dm_receiver(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter.peer = types.InputPeerUser(user_id=8, access_hash=111)
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="vanish", sender_id=8))
    pending = await ops.request(src, "ephemeral_send", {"content": "This disappears."})
    assert pending["status"] == "confirmation_required" and not adapter.requests
    result = await ops.execute_confirmed(src, "ephemeral_send", pending["params"])
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.ephemeral.SendMessageRequest)
    assert isinstance(request.receiver_id, types.InputUser) and request.receiver_id.user_id == 8
    assert request.peer == adapter.peer and request.message == "This disappears."
    assert result["data"]["ephemeral"] is True
    deleted = await ops.execute_confirmed(src, "ephemeral_delete", {"message_id": 9})
    assert deleted["ok"], deleted
    drequest = adapter.requests[-1]
    assert isinstance(drequest, functions.ephemeral.DeleteMessageRequest)
    assert drequest.receiver_id.user_id == 8 and drequest.id == 9 and drequest.peer == adapter.peer


@pytest.mark.asyncio
async def test_ephemeral_send_refuses_non_dm_peer(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    pending = await ops.request(src, "ephemeral_send", {"content": "Nope."})
    assert pending["status"] == "confirmation_required"
    result = await ops.execute_confirmed(src, "ephemeral_send", pending["params"])
    assert not result["ok"] and "direct message" in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_cross_chat_reply_uses_observed_peer_never_params(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter._input_peer_for = AsyncMock(return_value=adapter.peer)
    ops = TelegramClientOperations(adapter)
    other = source(chat_id="-1000000000999", thread_id="44")
    ops.observe(other, SimpleNamespace(id=9, message="Hello from the other chat", sender_id=8))
    pending = await ops.request(src, "cross_chat_reply", {"message_id": 9, "content": "Replying across chats."})
    assert pending["status"] == "confirmation_required" and not adapter.requests
    result = await ops.execute_confirmed(src, "cross_chat_reply", pending["params"])
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.peer == adapter.peer
    assert request.reply_to.reply_to_msg_id == 9
    assert request.reply_to.reply_to_peer_id == adapter.peer
    assert request.reply_to.top_msg_id == 44
    assert result["data"]["reply_to_chat_id"] == "-1000000000999"
    assert result["data"]["chat_id"] == "-1000000000999"


@pytest.mark.asyncio
async def test_cross_chat_reply_refuses_unobserved_ambiguous_or_raw_targets(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    refused = await ops.request(src, "cross_chat_reply", {"message_id": 9, "content": "No target."})
    assert refused["error"]["code"] == "MESSAGE_SCOPE_INVALID" and not adapter.requests
    bad_shape = await ops.request(src, "cross_chat_reply",
                                  {"message_id": 9, "content": "No.", "chat_id": "-1000000000999"})
    assert bad_shape["error"]["code"] == "INVALID_ARGUMENT" and not adapter.requests
    ops.observe(source(chat_id="-1000000000999"), SimpleNamespace(id=9, message="one", sender_id=8))
    ops.observe(source(chat_id="-1000000000888"), SimpleNamespace(id=9, message="two", sender_id=8))
    ambiguous = await ops.request(src, "cross_chat_reply", {"message_id": 9, "content": "Which?"})
    assert ambiguous["error"]["code"] == "MESSAGE_SCOPE_INVALID"
    assert "more than one" in ambiguous["error"]["message"] and not adapter.requests


@pytest.mark.asyncio
async def test_chat_topics_by_id_lookup_and_list_share_shape(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    by_id = await ops.request(source(), "chat_topics", {"topic_ids": [3, 5]})
    assert by_id["ok"], by_id
    assert [row["id"] for row in by_id["data"]["topics"]] == [3, 5]
    assert by_id["data"]["total"] == 2
    assert isinstance(adapter.requests[-1], functions.messages.GetForumTopicsByIDRequest)
    listed = await ops.request(source(), "chat_topics", {"limit": 2})
    assert [row["id"] for row in listed["data"]["topics"]] == [1, 2]
    assert isinstance(adapter.requests[-1], functions.messages.GetForumTopicsRequest)
    mixed = await ops.request(source(), "chat_topics", {"topic_ids": [3], "limit": 5})
    # Read-path validation errors surface as VALUEERROR (same as gateway_logs).
    assert mixed["error"]["code"] == "VALUEERROR"


@pytest.mark.asyncio
async def test_new_client_actions_are_listed_in_capabilities(tmp_path):
    adapter = FakeAdapter(tmp_path)
    capabilities = await TelegramClientOperations(adapter).request(source(), "capabilities")
    assert capabilities["ok"], capabilities
    for action in ("pin_message", "unpin_message", "big_reaction",
                   "ephemeral_send", "ephemeral_delete", "cross_chat_reply"):
        assert action in capabilities["data"]["mutations"]
        assert action in capabilities["data"]["parameters"]
    assert "topic_ids" in capabilities["data"]["parameters"]["chat_topics"]
