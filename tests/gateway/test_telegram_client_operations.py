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
from plugins.platforms.telegram.client_operations_ui import TelegramClientOperationsUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from datetime import UTC


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
    result = await ops.request(src, "poll_create", params)
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendMediaRequest)
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
    result = await ops.request(src, "poll_create", params)
    assert result["ok"] and result["status"] == "complete", result
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
async def test_rich_send_profile_button_accepts_any_user_id(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    params = {"content": "Member", "buttons": [{"type": "profile", "label": "Profile", "value": "8"}]}
    # No observation is needed: a profile button may target any user ID.
    result = await ops.request(src, "rich_message_send", params)
    assert result["ok"] and result["status"] == "complete", result
    assert adapter.requests
    # Same in another topic: profile buttons carry no sender-scope check.
    other_topic_result = await ops.request(source(thread_id="45"), "rich_message_send", params)
    assert other_topic_result["ok"], other_topic_result
    assert len(adapter.requests) == 2
    adapter.owner = False
    result = await ops.request(src, "rich_message_send", {"content": "Admitted non-owner reply"})
    assert result["ok"] and result["status"] == "complete"
    assert len(adapter.requests) == 3
    allowed_mutation = await ops.request(src, "chat_title", {"title": "Admitted change"})
    assert allowed_mutation["ok"] and allowed_mutation["status"] == "complete"
    assert len(adapter.requests) == 4


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
async def test_rich_send_profile_button_is_not_profile_scoped(tmp_path):
    adapter = FakeAdapter(tmp_path)
    adapter._settings_home = lambda src: tmp_path / str(src.profile)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(profile="alpha"), SimpleNamespace(id=9, message="member", sender_id=8))
    result = await ops.request(source(profile="beta"), "rich_message_send", {
        "content": "Member", "buttons": [{"type": "profile", "label": "Profile", "value": "8"}],
    })
    assert result["ok"] and result["status"] == "complete", result
    assert adapter.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["chat_info", "bot_profile", "user_info"])
async def test_metadata_read_schemas_are_usable_and_phone_not_exposed(tmp_path, action):
    adapter = FakeAdapter(tmp_path)
    result = await TelegramClientOperations(adapter).request(source(), action)
    assert result["ok"], result
    assert "phone" not in result["data"]
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_user_info_accepts_any_user_id(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    result = await ops.request(source(), "user_info", {"user_id": "8000000000"})
    assert result["ok"], result
    assert result["data"]["id"] == "8000000000"
    # Topic scope is irrelevant: any user ID resolves from any topic.
    assert (await ops.request(source(thread_id="9"), "user_info", {"user_id": "8000000000"}))["ok"]


@pytest.mark.asyncio
async def test_bot_profile_update_uses_bot_compatible_method(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    result = await ops.request(source(), "bot_profile_update", {"about": "The bot's own about"})
    assert result["ok"] and result["status"] == "complete", result
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
@pytest.mark.parametrize("rejection", ["not_admitted", "bot", "guest"])
async def test_mutations_fail_closed_before_any_rpc(tmp_path, rejection):
    adapter, src = FakeAdapter(tmp_path), source()
    if rejection == "not_admitted":
        adapter.allowed = False
    elif rejection == "bot":
        src.is_bot = True
    else:
        src.is_guest = True
    result = await TelegramClientOperations(adapter).request(src, "chat_title", {"title": "No"})
    assert result["error"]["code"] == "FORBIDDEN" and not adapter.requests


@pytest.mark.asyncio
async def test_gateway_log_read_is_bounded_redacted_and_pathless_for_any_admitted_user(tmp_path):
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

    # Any admitted user may read the logs, owner or not.
    adapter.owner = False
    non_owner = source(user_id="8")
    capabilities = await ops.request(non_owner, "capabilities")
    assert "gateway_logs" in capabilities["data"]["reads"]
    assert "gateway_logs" in capabilities["data"]["parameters"]
    allowed = await ops.request(non_owner, "gateway_logs", {"limit": 2})
    assert allowed["ok"], allowed
    assert allowed["data"]["file"] == "gateway.log"
    assert secret not in allowed["data"]["text"]


@pytest.mark.asyncio
async def test_ui_request_executes_mutation_immediately_without_confirmation(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source(thread_id="44")
    ui = TelegramClientOperationsUI(adapter)
    result = await ui.request(src, "chat_title", {"title": "Renamed group"})
    assert result["ok"] and result["status"] == "complete", result
    assert any(isinstance(r, functions.channels.EditTitleRequest) for r in adapter.requests)
    assert not ui._states  # no confirmation card is ever staged
    # Admission revoked is still refused before any RPC.
    adapter.allowed = False
    denied = await ui.request(src, "chat_title", {"title": "No change"})
    assert denied["error"]["code"] == "FORBIDDEN"
    assert len([r for r in adapter.requests
                if isinstance(r, functions.channels.EditTitleRequest)]) == 1


@pytest.mark.asyncio
async def test_delete_requires_current_chat_topic_observation_even_at_execution(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    assert not (await ops.request(source(), "purge", {"message_ids": [8]}))["ok"]
    ops.observe(source(thread_id="9"), SimpleNamespace(id=8))
    assert not (await ops.request(source(), "purge", {"message_ids": [8]}))["ok"]
    assert not (await ops.execute_confirmed(source(), "purge", {"message_ids": [8]}))["ok"]
    assert not adapter.requests


def _observed(message_id, *, own=True, date=None):
    kwargs = {"id": message_id, "out": own}
    if date is not None:
        kwargs["date"] = date
    return SimpleNamespace(**kwargs)


def _delete_requests(adapter):
    return [r for r in adapter.requests
            if isinstance(r, (functions.channels.DeleteMessagesRequest,
                              functions.messages.DeleteMessagesRequest))]


@pytest.mark.asyncio
async def test_purge_explicit_ids_deletes_observed_messages(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(), _observed(10))
    ops.observe(source(), _observed(11))
    result = await ops.request(source(), "purge", {"message_ids": [10, 11]})
    assert result["ok"] and result["status"] == "complete", result
    assert result["data"]["deleted_message_ids"] == [10, 11]
    assert result["data"]["purged"] == 2
    deletes = _delete_requests(adapter)
    assert len(deletes) == 1 and list(deletes[0].id) == [10, 11]


@pytest.mark.asyncio
async def test_purge_last_n_resolves_newest_own_observed(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    for message_id, own in ((1, False), (2, True), (3, True), (4, True), (5, False)):
        ops.observe(source(), _observed(message_id, own=own))
    result = await ops.request(source(), "purge", {"last_n": 2})
    assert result["ok"], result
    assert result["data"]["deleted_message_ids"] == [3, 4]
    assert result["data"]["form"] == "last_n"
    assert result["data"]["observed_own_total"] == 3


@pytest.mark.asyncio
async def test_purge_window_resolves_by_date_and_reports_dateless(tmp_path):
    from datetime import datetime, timedelta, timezone

    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    now = datetime.now(UTC)
    ops.observe(source(), _observed(20, date=now - timedelta(minutes=30)))
    ops.observe(source(), _observed(21, date=now - timedelta(hours=2)))
    ops.observe(source(), _observed(22))  # own, no recorded time
    result = await ops.request(source(), "purge", {"minutes": 60})
    assert result["ok"], result
    assert result["data"]["deleted_message_ids"] == [20]
    assert result["data"]["own_without_time"] == 1
    assert "no recorded time" in result["data"]["note"]


@pytest.mark.asyncio
async def test_purge_all_resolves_all_own_observed(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    for message_id, own in ((30, True), (31, False), (32, True)):
        ops.observe(source(), _observed(message_id, own=own))
    result = await ops.request(source(), "purge", {"all": True})
    assert result["ok"], result
    assert result["data"]["deleted_message_ids"] == [30, 32]
    assert "cannot be purged this way" in result["data"]["note"]


@pytest.mark.asyncio
async def test_purge_empty_window_is_a_plain_noop(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(), _observed(40, own=False))
    result = await ops.request(source(), "purge", {"last_n": 5})
    assert result["ok"] and result["data"]["purged"] == 0, result
    assert not _delete_requests(adapter)


@pytest.mark.asyncio
async def test_purge_is_owner_only(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    ops.observe(source(), _observed(50))
    stranger = source(user_id="8")
    for params in ({"message_ids": [50]}, {"last_n": 1}, {"all": True}):
        denied = await ops.request(stranger, "purge", params)
        assert denied["error"]["code"] == "FORBIDDEN", denied
        denied = await ops.execute_confirmed(stranger, "purge", params)
        assert denied["error"]["code"] == "FORBIDDEN", denied
    assert not adapter.requests


@pytest.mark.asyncio
async def test_purge_hidden_from_non_owner_capabilities(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    owner_caps = await ops.request(source(), "capabilities")
    assert "purge" in owner_caps["data"]["mutations"]
    assert "purge" in owner_caps["data"]["parameters"]
    stranger_caps = await ops.request(source(user_id="8"), "capabilities")
    assert "purge" not in stranger_caps["data"]["mutations"]
    assert "purge" not in stranger_caps["data"]["parameters"]


@pytest.mark.asyncio
async def test_purge_chunks_large_resolved_sets(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    for message_id in range(1000, 1150):
        ops.observe(source(), _observed(message_id))
    result = await ops.request(source(), "purge", {"all": True})
    assert result["ok"], result
    assert result["data"]["purged"] == 150
    deletes = _delete_requests(adapter)
    assert [len(r.id) for r in deletes] == [100, 50]


@pytest.mark.asyncio
async def test_purge_validation_needs_exactly_one_form(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    for params in ({}, {"last_n": 2, "all": True}, {"last_n": 0},
                   {"last_n": 501}, {"minutes": 10, "hours": 1},
                   {"message_ids": []}, {"message_ids": [1, 1]}):
        result = await ops.request(source(), "purge", params)
        assert not result["ok"], (params, result)
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
async def test_pin_unpin_execute_immediately_via_update_pinned_request(tmp_path, action, unpin):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="pin me", sender_id=8))
    result = await ops.request(src, action, {"message_id": 9})
    assert result["ok"] and result["status"] == "complete", result
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
    refused = await ops.request(src, "unpin_message", {"message_id": 9, "pm_oneside": True})
    assert not refused["ok"] and "DM" in refused["error"]["message"]
    assert not adapter.requests
    adapter.owner = False
    assert (await ops.request(src, "pin_message", {"message_id": 9}))["ok"]


@pytest.mark.asyncio
async def test_reaction_executes_immediately(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    result = await ops.request(src, "reaction", {"message_id": 9, "emoji": "👍", "big": True})
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.peer == adapter.peer and request.msg_id == 9
    assert request.big is True and request.add_to_recent is True
    assert len(request.reaction) == 1 and isinstance(request.reaction[0], types.ReactionEmoji)
    assert request.reaction[0].emoticon == "👍"
    assert result["data"]["emoji"] == "👍"
    assert result["data"]["big"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("params,code", [
    ({"message_id": 9, "emoji": "thumbsup"}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": ""}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": "like"}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": "�"}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": "👍👍"}, "INVALID_ARGUMENT"),
    ({"message_id": 10, "emoji": "👍"}, "MESSAGE_SCOPE_INVALID"),
    ({"message_id": 9, "emoji": "👍", "big": "yes"}, "INVALID_ARGUMENT"),
    ({"message_id": 9, "emoji": "👍", "chat_id": "-1000000000321"}, "INVALID_ARGUMENT"),
])
async def test_reaction_rejects_bad_emoji_or_unobserved_message(tmp_path, params, code):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    result = await ops.request(src, "reaction", params)
    assert result["error"]["code"] == code and not adapter.requests


@pytest.mark.asyncio
async def test_reaction_defaults_to_normal_and_omitted_message_id_uses_triggering_message(tmp_path):
    from dataclasses import replace

    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    normal = await ops.request(src, "reaction", {"message_id": 9, "emoji": "👍"})
    assert normal["ok"], normal
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.big is False and normal["data"]["big"] is False
    ops.observe(src, SimpleNamespace(id=55, message="the triggering message", sender_id=7))
    triggered = await ops.request(src, "reaction", {"emoji": "🎉"})
    assert triggered["ok"], triggered
    request = adapter.requests[-1]
    assert request.msg_id == 55 and request.big is False
    assert triggered["data"]["message_id"] == 55
    unknown = replace(src, message_id=None)
    refused = await ops.request(unknown, "reaction", {"emoji": "👍"})
    assert not refused["ok"] and "message_id" in refused["error"]["message"]
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji", ["👍🏽", "❤️"])
async def test_reaction_accepts_multi_codepoint_single_emoji(tmp_path, emoji):
    """One emoji glyph may be several code points (skin tone, VS16); the
    validator counts base glyphs, not code points."""
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    result = await ops.request(src, "reaction", {"message_id": 9, "emoji": emoji})
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.reaction[0].emoticon == emoji


class _ReactionRejectingAdapter(FakeAdapter):
    """_mtproto_rich_request that answers a SendReactionRequest with the
    server-side REACTION_INVALID the host hit live (2026-11-09 05:39:25 UTC)."""

    async def _mtproto_rich_request(self, request):
        if isinstance(request, functions.messages.SendReactionRequest):
            from telethon.errors.rpcerrorlist import ReactionInvalidError

            raise ReactionInvalidError(request=request)
        return await super()._mtproto_rich_request(request)


@pytest.mark.asyncio
async def test_reaction_server_rejection_is_a_clear_actionable_error(tmp_path):
    """Live failure: Telegram answered a SendReactionRequest with
    REACTION_INVALID (400) — the server rejecting the emoji payload, not a
    bot-account ban (that would be BOT_METHOD_INVALID). The tool must say so
    plainly, naming the emoji, so the agent retries with a standard reaction
    emoji instead of looping on tool-call formatting."""
    adapter, src = _ReactionRejectingAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="react to me", sender_id=8))
    result = await ops.request(src, "reaction", {"message_id": 9, "emoji": "🥰"})
    assert not result["ok"]
    assert result["error"]["code"] == "INVALID_ARGUMENT", result
    assert "not accepted as a reaction" in result["error"]["message"]
    assert "🥰" in result["error"]["message"]
    assert "Invalid reaction provided" not in result["error"]["message"]


@pytest.mark.asyncio
async def test_leave_supergroup_uses_leave_channel_request(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "leave", {})
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.channels.LeaveChannelRequest)
    assert request.channel.channel_id == 321 and request.channel.access_hash == 987654321
    assert result["data"] == {"left": True, "chat_type": "supergroup_or_channel"}


@pytest.mark.asyncio
async def test_leave_basic_group_uses_delete_chat_user_with_self(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter.peer = types.InputPeerChat(chat_id=321)
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "leave", {})
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.DeleteChatUserRequest)
    assert request.chat_id == 321
    assert isinstance(request.user_id, types.InputUserSelf)
    assert result["data"] == {"left": True, "chat_type": "group"}


@pytest.mark.asyncio
async def test_leave_refuses_dm_and_rejects_parameters(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter.peer = types.InputPeerUser(user_id=7, access_hash=123)
    ops = TelegramClientOperations(adapter)
    refused = await ops.request(src, "leave", {})
    assert not refused["ok"] and "direct message" in refused["error"]["message"]
    assert not adapter.requests
    bad_params = await ops.request(src, "leave", {"chat_id": "-1000000000321"})
    assert bad_params["error"]["code"] == "INVALID_ARGUMENT" and not adapter.requests


@pytest.mark.asyncio
async def test_leave_is_listed_in_capabilities(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    capabilities = await ops.request(src, "capabilities")
    assert "leave" in capabilities["data"]["mutations"]
    assert capabilities["data"]["parameters"]["leave"] == {}


@pytest.mark.asyncio
async def test_ephemeral_send_and_delete_use_dm_receiver(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter.peer = types.InputPeerUser(user_id=8, access_hash=111)
    ops = TelegramClientOperations(adapter)
    ops.observe(src, SimpleNamespace(id=9, message="vanish", sender_id=8))
    result = await ops.request(src, "ephemeral_send", {"content": "This disappears."})
    assert result["ok"] and result["status"] == "complete", result
    request = adapter.requests[-1]
    assert isinstance(request, functions.ephemeral.SendMessageRequest)
    assert isinstance(request.receiver_id, types.InputUser) and request.receiver_id.user_id == 8
    assert request.peer == adapter.peer and request.message == "This disappears."
    assert result["data"]["ephemeral"] is True
    deleted = await ops.request(src, "ephemeral_delete", {"message_id": 9})
    assert deleted["ok"] and deleted["status"] == "complete", deleted
    drequest = adapter.requests[-1]
    assert isinstance(drequest, functions.ephemeral.DeleteMessageRequest)
    assert drequest.receiver_id.user_id == 8 and drequest.id == 9 and drequest.peer == adapter.peer


@pytest.mark.asyncio
async def test_ephemeral_send_refuses_non_dm_peer(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "ephemeral_send", {"content": "Nope."})
    assert not result["ok"] and "direct message" in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_cross_chat_reply_uses_observed_peer_never_params(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    adapter._input_peer_for = AsyncMock(return_value=adapter.peer)
    ops = TelegramClientOperations(adapter)
    other = source(chat_id="-1000000000999", thread_id="44")
    ops.observe(other, SimpleNamespace(id=9, message="Hello from the other chat", sender_id=8))
    result = await ops.request(src, "cross_chat_reply", {"message_id": 9, "content": "Replying across chats."})
    assert result["ok"] and result["status"] == "complete", result
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
async def test_request_kwargs_builds_nested_tl_objects(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "request", {
        "method": "messages.SetTyping",
        "kwargs": {"action": {"_type": "SendMessageTextDraftAction", "random_id": 123456789012345678,
                              "text": {"_type": "TextWithEntities", "text": "hello", "entities": []}}},
    })
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.messages.SetTypingRequest)
    assert request.peer == adapter.peer
    action = request.action
    assert isinstance(action, types.SendMessageTextDraftAction)
    assert action.random_id == 123456789012345678
    assert isinstance(action.text, types.TextWithEntities) and action.text.text == "hello"


@pytest.mark.asyncio
async def test_request_reaches_write_methods_with_current_chat_peer(tmp_path):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "request", {"method": "channels.LeaveChannel"})
    assert result["ok"], result
    request = adapter.requests[-1]
    assert isinstance(request, functions.channels.LeaveChannelRequest)
    assert request.channel.channel_id == 321 and request.channel.access_hash == 987654321


@pytest.mark.asyncio
@pytest.mark.parametrize("params,message", [
    ({"method": "messages.SetTyping",
      "kwargs": {"peer": {"_type": "InputPeerChannel", "channel_id": 321, "access_hash": 1}}},
     "routing or credentials"),
    ({"method": "messages.SetTyping", "kwargs": {"action": {"_type": "NoSuchTlType"}}},
     "is not a Telethon TL type"),
    ({"method": "messages.SetTyping", "kwargs": {"action": {"random_id": 1}}},
     'needs a "_type"'),
    ({"method": "messages.SetTyping", "args": {"action": {"_type": "SendMessageTextDraftAction"}}},
     "use kwargs for nested TL objects"),
    ({"method": "messages.SetTyping", "kwargs": {"action": {"_type": "SendMessageTextDraftAction"}}},
     "needs text"),
])
async def test_request_kwargs_rejects_bad_descriptors(tmp_path, params, message):
    adapter, src = FakeAdapter(tmp_path), source()
    ops = TelegramClientOperations(adapter)
    result = await ops.request(src, "request", params)
    assert result["error"]["code"] == "INVALID_ARGUMENT" and message in result["error"]["message"]
    assert not adapter.requests


@pytest.mark.asyncio
async def test_request_parameters_advertise_kwargs(tmp_path):
    adapter = FakeAdapter(tmp_path)
    capabilities = await TelegramClientOperations(adapter).request(source(), "capabilities")
    assert capabilities["ok"], capabilities
    assert "kwargs" in capabilities["data"]["parameters"]["request"]


@pytest.mark.asyncio
async def test_request_is_not_listed_as_a_read(tmp_path):
    adapter = FakeAdapter(tmp_path)
    capabilities = await TelegramClientOperations(adapter).request(source(), "capabilities")
    assert capabilities["ok"], capabilities
    assert "request" not in capabilities["data"]["reads"]
    assert "request" in capabilities["data"]["structured_requests"]
    assert "read or write" in capabilities["data"]["structured_request_help"]


@pytest.mark.asyncio
async def test_new_client_actions_are_listed_in_capabilities(tmp_path):
    adapter = FakeAdapter(tmp_path)
    capabilities = await TelegramClientOperations(adapter).request(source(), "capabilities")
    assert capabilities["ok"], capabilities
    for action in ("pin_message", "unpin_message", "reaction",
                   "ephemeral_send", "ephemeral_delete", "cross_chat_reply"):
        assert action in capabilities["data"]["mutations"]
        assert action in capabilities["data"]["parameters"]
    assert "topic_ids" in capabilities["data"]["parameters"]["chat_topics"]
