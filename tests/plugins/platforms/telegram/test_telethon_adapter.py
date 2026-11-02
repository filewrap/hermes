import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from datetime import UTC


def _walk_tl(node):
    from telethon import types

    yield node
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk_tl(child)
    elif isinstance(node, types.TLObject):
        for child in vars(node).values():
            if isinstance(child, (types.TLObject, list, tuple)):
                yield from _walk_tl(child)


def test_registered_plugin_factory_uses_telethon_only():
    from plugins.platforms.telegram.adapter import _build_adapter

    adapter = _build_adapter(PlatformConfig(extra={}))
    assert isinstance(adapter, TelethonAdapter)
    assert adapter.name == "Telegram (MTProto)"


def test_credentials_fail_closed_when_missing():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    with pytest.raises(ValueError):
        adapter._credentials()


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "expected_topics"), [("42", [77]), ("43", [])])
async def test_inbound_topic_id_is_remembered_before_group_trigger_filter(
        monkeypatch, tmp_path, user_id, expected_topics):
    from plugins.platforms.telegram.topic_catalog import known_topic_ids

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    source = SimpleNamespace(chat_id=user_id, user_id=user_id, chat_type="dm",
                             thread_id="77", is_bot=False)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_is_owner_user", lambda user_id, source=None: str(user_id) == "42")
    monkeypatch.setattr(adapter, "_settings_home", lambda _source: tmp_path)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: None)
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda _source, _message: None)
    trigger = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_group_triggered", trigger)
    adapter._bot_id = 900
    message = SimpleNamespace(message="ordinary topic message", id=78)
    event = SimpleNamespace(message=message)

    await adapter._process_inbound_event(event, [message])

    assert known_topic_ids(tmp_path, user_id, user_id, 900) == expected_topics
    trigger.assert_awaited_once_with(event, source)


@pytest.mark.asyncio
async def test_start_answers_with_degraded_welcome_when_tenant_store_fails(monkeypatch):
    """A failing start_user must not swallow /start (observed live: the
    tenant store raising TenantStoreError made every /start vanish into
    the branch-wide deny). The sender still gets an answer, fail-closed
    to the unlisted wording, with no store-dependent owner notice."""
    from plugins.platforms.telegram import button_demo
    from gateway import mongo_multiuser

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    source = SimpleNamespace(chat_id="42", user_id="42", user_name="Test User",
                             chat_type="dm", thread_id=None, is_bot=False)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: "home")
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda *_args: None)
    monkeypatch.setattr(button_demo, "handle_reply_keyboard_text", AsyncMock(return_value=None))
    monkeypatch.setattr(button_demo, "clear_reply_keyboard_for_command", AsyncMock(return_value=None))

    class FailingStore:
        def get_access(self, _user_id):
            return {}

        def start_user(self, _user_id):
            raise RuntimeError("Tenant storage operation failed")

    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: FailingStore())
    monkeypatch.setattr(mongo_multiuser, "owner_user_id", lambda _home: "1")
    sent = []
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=lambda *args, **kwargs: sent.append((args, kwargs))))
    reactions = []
    monkeypatch.setattr(adapter, "send_reaction", AsyncMock(side_effect=lambda *args, **kwargs: reactions.append(args)))

    message = SimpleNamespace(message="/start", id=99)
    await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    assert len(sent) == 1
    text = sent[0][0][1]
    assert "don\u2019t recognize you" in text
    assert "Ask to be added" in text
    assert reactions == [("42", "99", "🥴")]


@pytest.mark.asyncio
async def test_settings_command_opens_a_native_card_in_each_owner_dm_topic(monkeypatch, tmp_path):
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
    from plugins.platforms.telegram.skills_ui import TelegramSkillsUI
    from plugins.platforms.telegram.thread_ui import TelegramThreadUI

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    topics = ["77", "88", "99", "1"]
    sources = [SimpleNamespace(
        chat_id="123", user_id="123", user_name="Owner", chat_type="dm",
        thread_id=topic, profile="default", is_bot=False,
    ) for topic in topics]
    monkeypatch.setattr(adapter, "_source", AsyncMock(side_effect=sources))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: None)
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda *_args: None)
    monkeypatch.setattr(adapter, "_refresh_command_menu", AsyncMock())
    monkeypatch.setattr(adapter, "_prepare_attachment_intake", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    monkeypatch.setattr(TelegramThreadUI, "remember_source_topic", lambda *_args: None)

    class NoopUI:
        async def handle_message(self, *_args):
            return False

    noop_ui = NoopUI()
    settings_ui = TelegramPersonalSettingsUI(adapter)
    monkeypatch.setattr(adapter, "_setup_ui", lambda: noop_ui)
    monkeypatch.setattr(adapter, "_model_settings_ui", lambda: noop_ui)
    monkeypatch.setattr(adapter, "_provider_ui", lambda: noop_ui)
    monkeypatch.setattr(adapter, "_settings_ui", lambda: settings_ui)
    monkeypatch.setattr(adapter, "_skills_ui", lambda: TelegramSkillsUI(adapter))

    async def invoke(_event, source, _ui, handler, text):
        return await handler(_event, source, text)

    monkeypatch.setattr(adapter, "_invoke_control_ui_message", invoke)
    requests = []
    message_ids = iter((501, 502, 503, 504))

    async def send_native(request):
        requests.append(request)
        return SimpleNamespace(id=next(message_ids))

    monkeypatch.setattr(adapter, "_mtproto_rich_request", send_native)
    monkeypatch.setattr(adapter, "_sent_message_id", lambda response: str(response.id))

    for index in range(len(topics)):
        message = SimpleNamespace(message="/settings", id=400 + index)
        await adapter._process_inbound_event(SimpleNamespace(message=message), [message])

    assert [request.reply_to.top_msg_id for request in requests] == [77, 88, 99, None]
    assert [request.reply_to.reply_to_msg_id for request in requests] == [400, 401, 402, 403]
    assert {state["thread_id"] for state in settings_ui._states.values()} == set(topics)
    assert len({state["message_id"] for state in settings_ui._states.values()}) == 4


@pytest.mark.asyncio
async def test_source_maps_general_only_for_forums_and_does_not_guess_named_topics_from_replies():
    adapter = TelethonAdapter(PlatformConfig())

    async def source_for(chat, reply):
        message = SimpleNamespace(id=78, reply_to=reply)
        event = SimpleNamespace(
            message=message, chat=chat, sender=SimpleNamespace(id=42),
            chat_id="-1001", sender_id=42,
        )
        return await adapter._source(event)

    assert (await source_for(SimpleNamespace(title="Forum", forum=True), None)).thread_id == "1"
    ordinary_reply = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=None, forum_topic=False)
    assert (await source_for(SimpleNamespace(title="Forum", forum=True), ordinary_reply)).thread_id == "1"
    named_reply = SimpleNamespace(reply_to_msg_id=66, reply_to_top_id=77, forum_topic=True)
    assert (await source_for(SimpleNamespace(title="Forum", forum=True), named_reply)).thread_id == "77"
    assert (await source_for(SimpleNamespace(title="Group", forum=False), None)).thread_id is None


@pytest.mark.asyncio
async def test_connect_uses_bot_token_and_registers_native_telethon_events(monkeypatch, tmp_path):
    import telethon
    import plugins.platforms.telegram.telethon_adapter as adapter_module

    calls = []
    monkeypatch.setattr(adapter_module, "_secret", lambda key, default="": {
        "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash"}.get(key, default))

    class FakeClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            calls.append((session, api_id, api_hash, kwargs))
            self.handlers = []
            self.commands = []

        async def start(self, *, bot_token):
            calls.append(("start", bot_token))

        async def get_me(self):
            return SimpleNamespace(bot=True, id=42, username="testbot")

        def is_connected(self):
            return False

        def add_event_handler(self, handler, event):
            self.handlers.append((handler, event))

        async def __call__(self, request):
            calls.append(("request", request))
            name = type(request).__name__
            if name == "SetBotCommandsRequest":
                self.commands = request.commands
                return True
            if name == "GetBotCommandsRequest":
                return self.commands
            raise AssertionError(f"Unexpected Telegram request: {name}")

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(telethon, "TelegramClient", FakeClient)
    cfg = PlatformConfig(token="bot-token", extra={})
    adapter = TelethonAdapter(cfg)
    monkeypatch.setattr(adapter, "_session_file", lambda: tmp_path / "profile" / "telethon.session")

    assert await adapter.connect()
    assert calls[0][1:3] == (123, "hash")
    assert calls[1] == ("start", "bot-token")
    handlers = {handler.__name__: event for handler, event in adapter._client.handlers}
    assert {"NewMessage", "Album", "CallbackQuery", "InlineQuery", "Raw"} <= {
        type(event).__name__ for event in handlers.values()
    }
    from telethon.tl import types
    assert handlers["_on_managed_bot_update"].types is types.UpdateManagedBot
    menu_requests = [call[1] for call in calls if len(call) > 1 and call[0] == "request"]
    assert [type(request).__name__ for request in menu_requests] == [
        "SetBotCommandsRequest", "GetBotCommandsRequest",
    ]
    assert menu_requests[0].commands
    assert type(menu_requests[1].scope) is type(menu_requests[0].scope)
    assert [(row.command, row.description) for row in adapter._client.commands] == [
        (row.command, row.description) for row in menu_requests[0].commands
    ]
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_native_send_edit_delete_use_mtproto_client(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(("request", request))
            return SimpleNamespace(id=8)

        async def send_file(self, *args, **kwargs):
            calls.append(("file", args, kwargs))
            return SimpleNamespace(id=9)

        async def delete_messages(self, *args, **kwargs):
            calls.append(("delete", args, kwargs))

    media_file = tmp_path / "x.txt"
    media_file.write_text("attachment", encoding="utf-8")
    adapter._client = Client()
    assert (await adapter.send("-1001", "hello")).message_id == "8"
    assert (await adapter.send_file("-1001", str(media_file), "caption")).message_id == "9"
    assert (await adapter.edit_message("-1001", "8", "updated")).success
    assert await adapter.delete_message("-1001", "8")
    # The file send shows the upload chat action first (a SetTypingRequest),
    # then the file itself; strict-topic sends suppress it, plain sends do not.
    assert [c[0] for c in calls] == ["request", "request", "file", "request", "delete"]
    assert type(calls[1][1]).__name__ == "SetTypingRequest"
    assert type(calls[0][1]).__name__ == "SendMessageRequest"
    assert type(calls[0][1].rich_message).__name__ == "InputRichMessageMarkdown"
    assert calls[2][2]["parse_mode"] == "markdown"
    assert type(calls[3][1]).__name__ == "EditMessageRequest"
    assert type(calls[3][1].rich_message).__name__ == "InputRichMessageMarkdown"


def _make_test_video(path: Path, *, width: int = 320, height: int = 240, duration: int = 2) -> Path:
    import subprocess

    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={width}x{height}:rate=10",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return path


def test_probe_video_geometry_reads_real_video(tmp_path):
    from plugins.platforms.telegram.telethon_adapter import _probe_video_geometry

    video = _make_test_video(tmp_path / "clip.mp4")
    assert _probe_video_geometry(str(video)) == {"width": 320, "height": 240, "duration": 2}


def test_probe_video_geometry_fails_open_on_non_video(tmp_path):
    from plugins.platforms.telegram.telethon_adapter import _probe_video_geometry

    text = tmp_path / "note.txt"
    text.write_text("not a video", encoding="utf-8")
    assert _probe_video_geometry(str(text)) == {}


def test_video_thumbnail_jpeg_writes_frame(tmp_path):
    from plugins.platforms.telegram.telethon_adapter import _video_thumbnail_jpeg

    video = _make_test_video(tmp_path / "clip.mp4")
    thumb = _video_thumbnail_jpeg(str(video), 2)
    assert thumb is not None
    assert Path(thumb).read_bytes()[:2] == b"\xff\xd8"
    Path(thumb).unlink()


@pytest.mark.asyncio
async def test_send_video_carries_geometry_and_thumbnail(tmp_path):
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=8)

        async def send_file(self, *args, **kwargs):
            thumb = kwargs.get("thumb")
            calls.append(("file", args, kwargs, bool(thumb and Path(thumb).is_file())))
            return SimpleNamespace(id=7)

    video = _make_test_video(tmp_path / "clip.mp4")
    adapter._client = Client()
    result = await adapter.send_video("-1001", str(video), "caption")
    assert result.success and result.message_id == "7"
    assert len(calls) == 1
    _, _, kwargs, thumb_existed = calls[0]
    assert thumb_existed  # the thumbnail existed while Telethon read it
    assert kwargs["thumb"].endswith(".jpg")
    video_attr = next(a for a in kwargs["attributes"] if isinstance(a, types.DocumentAttributeVideo))
    assert (video_attr.w, video_attr.h, video_attr.duration) == (320, 240, 2)
    assert any(isinstance(a, types.DocumentAttributeFilename) for a in kwargs["attributes"])
    assert not Path(kwargs["thumb"]).exists()  # temp thumbnail removed after the send


@pytest.mark.asyncio
async def test_send_video_without_readable_video_still_sends(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=8)

        async def send_file(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(id=7)

    text = tmp_path / "note.txt"
    text.write_text("not a video", encoding="utf-8")
    adapter._client = Client()
    result = await adapter.send_video("-1001", str(text))
    assert result.success
    assert "attributes" not in calls[0][1] and "thumb" not in calls[0][1]


@pytest.mark.asyncio
async def test_restart_notice_without_saved_id_sends_native_rich_message():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=81)

    adapter._client = Client()
    adapter.send = AsyncMock(side_effect=AssertionError("restart notice must not call plain send"))
    result = await adapter.send_or_edit_restart_notice(
        "123", "# Hermes is restarting", metadata={
            "thread_id": "77", "telegram_reply_to_message_id": "21",
        },
    )

    assert result.success is True
    assert result.message_id == "81"
    assert len(calls) == 1
    request = calls[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert type(request.rich_message).__name__ == "InputRichMessageMarkdown"
    assert request.reply_to.reply_to_msg_id == 21
    assert request.reply_to.top_msg_id == 77
    assert request.no_webpage is True
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_swarm_callback_routes_to_swarm_ui(monkeypatch):
    """A sw: callback reaches the swarm UI, not the unavailable-answer fallthrough."""
    from gateway import mongo_multiuser

    monkeypatch.setattr(mongo_multiuser, "enabled", lambda home: False)
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    calls = []

    class FakeSwarmUI:
        async def handle_callback(self, event, data):
            calls.append(data)

    adapter._swarm_ui = lambda: FakeSwarmUI()

    class FakeEvent:
        data = b"sw:abcdef123456:cancel"
        answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    event = FakeEvent()
    await adapter._on_callback_query(event)
    assert calls == ["sw:abcdef123456:cancel"]
    assert event.answers == []


@pytest.mark.asyncio
async def test_restart_notice_edits_saved_rich_message_without_sending_new():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=82)

    adapter._client = Client()
    result = await adapter.send_or_edit_restart_notice(
        "123", "# Hermes is restarting", message_id="81",
    )

    assert result.success is True
    assert result.message_id == "81"
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "EditMessageRequest"
    assert calls[0].id == 81
    assert type(calls[0].rich_message).__name__ == "InputRichMessageMarkdown"


@pytest.mark.asyncio
async def test_restart_notice_edit_failure_sends_one_rich_replacement():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class MessageIdInvalidError(Exception):
        pass

    class Client:
        async def __call__(self, request):
            calls.append(request)
            if len(calls) == 1:
                raise MessageIdInvalidError("not editable")
            return SimpleNamespace(id=83)

    adapter._client = Client()
    adapter.send = AsyncMock(side_effect=AssertionError("restart notice must not call plain send"))
    result = await adapter.send_or_edit_restart_notice(
        "123", "# Hermes is restarting", message_id="81", metadata={"thread_id": "77"},
    )

    assert result.success is True
    assert result.message_id == "83"
    assert [type(request).__name__ for request in calls] == ["EditMessageRequest", "SendMessageRequest"]
    assert all(type(request.rich_message).__name__ == "InputRichMessageMarkdown" for request in calls)
    assert calls[1].reply_to.reply_to_msg_id == 77
    assert calls[1].reply_to.top_msg_id == 77
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_notice_unchanged_edit_is_not_duplicated():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class MessageNotModifiedError(Exception):
        pass

    class Client:
        async def __call__(self, request):
            calls.append(request)
            raise MessageNotModifiedError("identical rich content")

    adapter._client = Client()
    result = await adapter.send_or_edit_restart_notice(
        "123", "# Hermes is restarting", message_id="81",
    )

    assert result.success is True
    assert result.message_id == "81"
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "EditMessageRequest"


@pytest.mark.asyncio
async def test_markdown_survives_split_messages_with_angle_bracket_placeholders(tmp_path):
    from telethon.extensions import markdown
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(("send", request))
            return SimpleNamespace(id=len(calls))

        async def send_file(self, chat_id, file_path, **kwargs):
            calls.append(("file", file_path, kwargs))
            return SimpleNamespace(id=len(calls))

    adapter._client = Client()
    content = "📖 **Hermes Commands**\n" + ("plain command description\n" * 1400)
    content += "`/format <json|md|html>`\n**Still bold**"
    result = await adapter.send("123", content)

    assert result.success
    assert len(calls) == 2
    assert isinstance(calls[0][1].rich_message, types.InputRichMessageMarkdown)
    assert isinstance(calls[1][1].rich_message, types.InputRichMessage)
    assert any(isinstance(node, types.TextButton)
               and isinstance(node.type, types.InlineButtonTypeCopy)
               and node.type.copy_text == "/format <json|md|html>"
               for node in _walk_tl(calls[1][1].rich_message))
    parsed_tail, entities = markdown.parse(calls[-1][1].message)
    assert "/format <json|md|html>" in parsed_tail
    assert "Still bold" in parsed_tail
    assert any(type(entity).__name__ == "MessageEntityBold" for entity in entities)

    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    caption = "**Caption** with `<name>`"
    await adapter.send_file("123", str(image), caption)
    assert calls[-1][0] == "file"
    assert calls[-1][2]["parse_mode"] == "markdown"


def test_telegram_parse_mode_only_selects_html_for_real_markup():
    from plugins.platforms.telegram.telethon_adapter import _parse_mode_for_text

    assert _parse_mode_for_text("**Header** and `<b>literal</b>`") == "markdown"
    assert _parse_mode_for_text("<b>Actual HTML</b>") == "html"


@pytest.mark.asyncio
async def test_inbound_peer_is_reused_for_outbound_send_and_persisted():
    adapter = TelethonAdapter(PlatformConfig())
    input_peer = SimpleNamespace(user_id=7, access_hash=123)
    chat = SimpleNamespace(id=7, access_hash=123, first_name="alice")
    persisted = []
    sent = []

    class Session:
        def process_entities(self, entities):
            persisted.append(entities)

    class Client:
        session = Session()

        async def __call__(self, request):
            sent.append(request)
            return SimpleNamespace(id=11)

    adapter._client = Client()
    await adapter._cache_inbound_peer(SimpleNamespace(input_chat=input_peer, chat=chat), "7")

    result = await adapter.send("7", "hello")
    assert result.success
    assert sent[0].peer is input_peer
    assert type(sent[0].rich_message).__name__ == "InputRichMessageMarkdown"
    assert persisted == [[chat]]


@pytest.mark.asyncio
async def test_outbound_send_resolves_uncached_numeric_chat_id_to_input_peer():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    input_peer = types.InputPeerChannel(channel_id=42, access_hash=123)
    sent = []

    class Client:
        async def get_input_entity(self, peer):
            assert peer == -10042
            return input_peer

        async def __call__(self, request):
            sent.append(request)
            return SimpleNamespace(id=11)

    adapter._client = Client()
    result = await adapter.send("-10042", "Gateway online")

    assert result.success
    assert sent[0].peer is input_peer
    assert adapter._peer_for("-10042") is input_peer


@pytest.mark.asyncio
async def test_unauthorized_inbound_update_does_not_cache_peer(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="8")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _: False)
    cache_peer = AsyncMock()
    monkeypatch.setattr(adapter, "_cache_inbound_peer", cache_peer)

    await adapter._on_new_message(SimpleNamespace(message=SimpleNamespace(message="/status")))

    cache_peer.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_messages_require_mention_unless_free_response(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._bot_username = "hermes_bot"
    source = SimpleNamespace(chat_type="group", chat_id="-1001", thread_id=None)
    event = SimpleNamespace(message=SimpleNamespace(message="hello", reply_to_msg_id=None))
    assert not await adapter._group_triggered(event, source)
    event.message.message = "hello @hermes_bot"
    assert await adapter._group_triggered(event, source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, allow_all, reply_sender_id, explicitly_allowed, expected",
    [
        ("trigger", False, 42, True, True),
        ("trigger", False, 99, True, False),
        ("trigger", False, 42, False, False),
        ("trigger", True, 42, False, True),
        # Tag-mode invocation is independent of authorization; the inbound
        # authorization guard still denies unallowed users after this gate.
        ("tag", False, 42, False, True),
        ("tag", False, 99, False, False),
    ],
)
async def test_strict_group_modes_admit_only_replies_to_this_bot_without_bypassing_access(
    monkeypatch, mode, allow_all, reply_sender_id, explicitly_allowed, expected,
):
    import plugins.platforms.telegram.telethon_adapter as adapter_module
    monkeypatch.setattr(adapter_module, "_secret", lambda _key, default="": default)
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._bot_username = "hermes_bot"
    adapter._bot_id = 42
    adapter._client = object()
    adapter._chat_settings = lambda _source: {"trigger_mode": mode, "allow_all": allow_all}
    adapter._is_explicitly_allowed = lambda _source: explicitly_allowed
    source = SimpleNamespace(chat_type="group", chat_id="-1001", thread_id=None)

    message = SimpleNamespace(
        message="follow-up without a username or command",
        reply_to_msg_id=10,
        get_reply_message=AsyncMock(return_value=SimpleNamespace(sender_id=reply_sender_id)),
    )
    assert await adapter._group_triggered(SimpleNamespace(message=message), source) is expected


def test_telegram_identity_prompt_is_bound_to_each_adapter_username():
    naina = TelethonAdapter(PlatformConfig(extra={}))
    naina._bot_username = "NainaAssistantBot"
    hermes = TelethonAdapter(PlatformConfig(extra={}))
    hermes._bot_username = "HermesAssistantBot"

    assert "@NainaAssistantBot" in naina._telegram_identity_prompt()
    assert "@HermesAssistantBot" not in naina._telegram_identity_prompt()
    assert "@HermesAssistantBot" in hermes._telegram_identity_prompt()
    assert "@NainaAssistantBot" not in hermes._telegram_identity_prompt()


@pytest.mark.asyncio
async def test_group_commands_and_explicit_mention_setting(monkeypatch):
    import plugins.platforms.telegram.telethon_adapter as adapter_module
    monkeypatch.setattr(adapter_module, "_secret", lambda key, default="": default)

    source = SimpleNamespace(chat_type="group", chat_id="-1001", thread_id=None)
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "hermes_bot"
    event = SimpleNamespace(message=SimpleNamespace(message="/help@hermes_bot", reply_to_msg_id=None))
    assert await adapter._group_triggered(event, source)
    event.message.message = "/help@another_bot"
    assert not await adapter._group_triggered(event, source)
    event.message.message = "hello @hermes_bot_extra"
    assert not await adapter._group_triggered(event, source)

    no_mention = TelethonAdapter(PlatformConfig(extra={"require_mention": False}))
    no_mention._bot_username = "hermes_bot"
    event.message.message = "hello everyone"
    assert await no_mention._group_triggered(event, source)


def test_native_proxy_url_is_translated_to_telethon_dict(monkeypatch):
    import plugins.platforms.telegram.telethon_adapter as adapter_module
    monkeypatch.setattr(adapter_module, "_secret", lambda key: "socks5://user:pass@proxy.example:1080")
    assert adapter_module._proxy_settings() == {
        "proxy_type": "socks5", "addr": "proxy.example", "port": 1080,
        "rdns": True, "username": "user", "password": "pass",
    }


def test_yaml_bridge_preserves_native_menu_and_drops_legacy_rich_toggle(caplog):
    from plugins.platforms.telegram.adapter import _apply_yaml_config

    with caplog.at_level("INFO"):
        extra = _apply_yaml_config({}, {
            "extra": {
                "rich_messages": True,
                "command_menu": {"max_commands": 60},
                "disable_link_previews": True,
            },
        })
    assert extra == {"command_menu": {"max_commands": 60}, "disable_link_previews": True}
    assert "rich_messages is ignored" in caplog.text


@pytest.mark.asyncio
async def test_text_send_splits_at_rich_message_limit_and_keeps_every_chunk_in_topic():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []
    from telethon import types

    class Client:
        async def get_messages(self, peer, *, ids):
            return SimpleNamespace(
                id=ids, peer_id=types.PeerUser(42),
                reply_to=SimpleNamespace(reply_to_top_id=77),
            )

        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=len(calls))

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    result = await adapter.send(
        "-1001", "x" * 33_000, reply_to="88",
        metadata={"thread_id": "77", "disable_link_previews": True})
    assert result.success
    assert len(calls) == 2
    assert all(len(req.message) <= adapter.MAX_MESSAGE_LENGTH for req in calls)
    assert calls[0].no_webpage is True
    assert calls[0].reply_to.reply_to_msg_id == 88
    assert calls[0].reply_to.top_msg_id == 77
    assert calls[1].reply_to.reply_to_msg_id == 88
    assert calls[1].reply_to.top_msg_id == 77
    assert all(type(req.rich_message).__name__ == "InputRichMessageMarkdown" for req in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,chat_id", [("dm", "7"), ("group", "-1001")])
async def test_inbound_telethon_message_preserves_reply_context_and_bot_identity(
    monkeypatch, chat_type, chat_id,
):
    adapter = TelethonAdapter(PlatformConfig())
    adapter._bot_id = 42
    adapter._bot_username = "NainaAssistantBot"
    source = SimpleNamespace(
        user_id="7", user_name="alice", chat_type=chat_type, chat_id=chat_id, thread_id=None,
    )

    class Reply:
        message = "original question"
        sender_id = 42

        async def get_sender(self):
            return SimpleNamespace(username="hermes_bot", first_name="Hermes")

    class Message:
        message = "follow up"
        id = 12
        reply_to_msg_id = 10
        photo = video = voice = audio = sticker = document = None

        async def get_reply_message(self):
            return Reply()

    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_media", AsyncMock(return_value=([], [], "")))
    adapter.handle_message = AsyncMock()
    await adapter._on_new_message(SimpleNamespace(message=Message()))
    event = adapter.handle_message.await_args.args[0]
    assert event.reply_to_message_id == "10"
    assert event.reply_to_text == "original question"
    assert event.reply_to_author_id == "42"
    assert event.reply_to_author_name == "hermes_bot"
    assert event.reply_to_is_own_message is True
    assert "You are the Telegram assistant @NainaAssistantBot" in event.channel_prompt
    assert "do not deny being this bot" in event.channel_prompt
    assert "model or provider name does not change your Telegram identity" in event.channel_prompt


@pytest.mark.asyncio
async def test_standalone_send_connects_sends_and_disconnects_via_telethon(monkeypatch, tmp_path):
    import telethon
    import plugins.platforms.telegram.telethon_adapter as adapter_module

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.handlers = []
            self.commands = []

        async def start(self, *, bot_token):
            calls.append(("start", bot_token))

        async def get_me(self):
            return SimpleNamespace(bot=True, id=42, username="hermes_bot")

        def add_event_handler(self, handler, event):
            self.handlers.append((handler, event))

        def is_connected(self):
            return False

        async def __call__(self, request):
            calls.append(("request", request))
            name = type(request).__name__
            if name == "SetBotCommandsRequest":
                self.commands = request.commands
                return True
            if name == "GetBotCommandsRequest":
                calls.append(("menu_response", [(row.command, row.description) for row in self.commands]))
                return self.commands
            return SimpleNamespace(id=9)

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(telethon, "TelegramClient", FakeClient)
    monkeypatch.setattr(adapter_module, "_secret", lambda key, default="": {
        "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash"}.get(key, default))
    monkeypatch.setattr(TelethonAdapter, "_session_file", lambda self: tmp_path / "profile" / "session")
    result = await adapter_module.send_standalone("bot-token", "123", "hello", thread_id="77")
    assert result["success"] is True
    assert calls[0] == ("start", "bot-token")
    menu_requests = [call[1] for call in calls if len(call) > 1 and call[0] == "request" and
                     type(call[1]).__name__ in {"SetBotCommandsRequest", "GetBotCommandsRequest"}]
    assert [type(request).__name__ for request in menu_requests] == [
        "SetBotCommandsRequest", "GetBotCommandsRequest",
    ]
    assert next(call[1] for call in calls if call[0] == "menu_response") == [
        (row.command, row.description) for row in menu_requests[0].commands
    ]
    send_request = next(call[1] for call in calls if len(call) > 1 and call[0] == "request" and
                        type(call[1]).__name__ == "SendMessageRequest")
    assert send_request.peer == "123"
    assert send_request.reply_to.reply_to_msg_id == 77
    assert type(send_request.rich_message).__name__ == "InputRichMessageMarkdown"
    assert calls[-1] == ("disconnect",)


def test_native_rich_helpers_build_colored_mtproto_buttons():
    from plugins.platforms.telegram.mtproto_rich import inline_markup, input_rich_markdown

    rich = input_rich_markdown("# Rich **text**")
    markup = inline_markup([[("Enable", "hm:guestbot:on", "success"),
                             ("Disable", "hm:guestbot:off", "danger")]])
    assert type(rich).__name__ == "InputRichMessageMarkdown"
    assert rich.markdown == "# Rich **text**"
    buttons = markup.rows[0].buttons
    assert [button.text for button in buttons] == ["Enable", "Disable"]
    assert buttons[0].style.bg_success is True
    assert buttons[1].style.bg_danger is True


@pytest.mark.asyncio
async def test_model_picker_builds_and_sends_native_inline_markup():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=101)

    adapter._client = Client()
    result = await adapter.send_model_picker(
        chat_id="42",
        providers=[{"slug": "openrouter", "name": "OpenRouter", "models": ["vendor/model-a"]}],
        current_model="vendor/model-b", current_provider="openrouter", session_key="session-1",
        on_model_selected=AsyncMock(), metadata={"user_id": "7", "thread_id": "77"},
    )
    assert result.success
    assert type(calls[0]).__name__ == "SendMessageRequest"
    assert calls[0].reply_to.reply_to_msg_id == 77
    assert calls[0].reply_markup.rows
    provider_text, _ = adapter._model_picker_provider_page(
        2,
        {"providers": [{"slug": "openrouter", "name": "OpenRouter"}],
         "current_model": "vendor/model-b", "current_provider": "openrouter"},
        0,
    )
    assert provider_text.rstrip().endswith("__Page 1 of 1 • total 1__")
    provider_buttons = [getattr(item, "button", item)
                        for row in calls[0].reply_markup.rows for item in row.buttons]
    assert next(button for button in provider_buttons if button.text == "Cancel").style.bg_danger is True

    state = {"model_list": ["vendor/model-a"], "selected_provider_name": "OpenRouter",
             "current_model": "vendor/model-a"}
    model_text, markup = adapter._model_picker_model_page(2, state, 0)
    assert model_text.rstrip().endswith("__Page 1 of 1 • total 1__")
    assert markup.rows
    model_buttons = [getattr(item, "button", item)
                     for row in markup.rows for item in row.buttons]
    assert next(button for button in model_buttons if button.text == "Cancel").style.bg_danger is True


@pytest.mark.asyncio
async def test_slash_confirm_uses_native_mtproto_buttons_and_one_shot_callback(monkeypatch):
    from tools import slash_confirm

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=55)

    adapter._client = Client()
    sent = await adapter.send_slash_confirm(
        chat_id="42", title="/new",
        message=("# Confirm /new\n\nDiscard this session?\n\n"
                 "**Text fallback:** reply `/approve`, `/always`, or `/cancel`."),
        session_key="session-1", confirm_id="cf-1",
        metadata={"user_id": "7", "thread_id": "77", "reply_to": "88"},
    )
    assert sent.success
    request = calls[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert request.reply_to.reply_to_msg_id == 88
    assert request.reply_to.top_msg_id == 77
    assert type(request.rich_message).__name__ == "InputRichMessage"
    block_text = str(request.rich_message)
    assert "profile-wide preference" in block_text
    assert "Text fallback:" in block_text
    assert "_Text fallback:" not in block_text
    copy_buttons = [
        item for block in request.rich_message.blocks
        for item in getattr(getattr(block, "text", None), "texts", ())
        if type(item).__name__ == "TextButton"
    ]
    assert [item.type.copy_text for item in copy_buttons] == ["/approve", "/always", "/cancel"]
    assert not any(icon in block_text for icon in ("⚠️", "✅", "❌", "ℹ️", "🟡"))
    rows = [[getattr(button, "button", button) for button in row.buttons]
            for row in request.reply_markup.rows]
    assert [len(row) for row in rows] == [1, 2]
    assert [button.text for row in rows for button in row] == [
        "Approve once", "Always approve", "Cancel",
    ]
    assert rows[0][0].style.bg_success is True
    assert rows[1][0].style.bg_danger is True
    assert rows[1][1].style.bg_danger is True

    source = SimpleNamespace(chat_id="42", user_id="7", thread_id="77")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    actions = []

    async def handle(choice):
        actions.append(choice)
        return "Approved once."

    slash_confirm.register("session-1", "cf-1", "new", handle)
    answers = []

    async def answer(text=None, alert=False):
        answers.append((text or "", alert))

    event = SimpleNamespace(message_id=55, answer=answer)
    await adapter._handle_slash_confirm_callback(event, "sc:once:cf-1")
    assert actions == ["once"]
    assert slash_confirm.get_pending("session-1") is None
    assert type(calls[-1]).__name__ == "EditMessageRequest"
    assert calls[-1].rich_message.markdown == "Approved once."
    assert calls[-1].reply_markup is None

    await adapter._handle_slash_confirm_callback(event, "sc:once:cf-1")
    assert actions == ["once"]
    assert answers[-1][1] is True
    assert "cf-1" not in adapter._slash_confirm_state


def test_help_markdown_entities_use_valid_utf16_offsets():
    from plugins.platforms.telegram.mtproto_rich import markdown_entities

    text, entities = markdown_entities("😀 **bold** and `/help`")
    assert text == "😀 bold and /help"
    assert [(type(entity).__name__, entity.offset, entity.length) for entity in entities] == [
        ("MessageEntityBold", 3, 4),
        ("MessageEntityCode", 12, 5),
    ]
    with pytest.raises(ValueError, match="surrogate pair"):
        from telethon import types
        from plugins.platforms.telegram.mtproto_rich import _validate_entity_spans

        _validate_entity_spans("😀x", [types.MessageEntityBold(offset=1, length=1)])


def test_help_menu_exposes_all_gateway_commands_and_skill_pages():
    from html import escape
    from plugins.platforms.telegram.rich_ui import help_sections, help_view, PAGE_SIZE

    sections = help_sections()
    search_sections = [entries for category, entries in sections if category == "Search"]
    assert len(search_sections) == 1
    assert {command.split(" ", 1)[0] for command, _description in search_sections[0]} >= {
        "/search", "/deepresearch",
    }
    seen = set()
    category_sections = [section for section in sections if section[0] != "Skills"]
    for category, entries in sections:
        if category == "Skills":
            view = "skills"
        else:
            view = str(next(i for i, (name, _) in enumerate(category_sections) if name == category))
        pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        for page in range(pages):
            text, markup = help_view(view, page)
            assert markup is not None
            assert text.startswith(
                "<h1>Hermes Help</h1><p>Send a command below, or choose another section.</p>"
                f"<hr /><h2>{escape(category)}</h2>"
            )
            page_entries = entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
            expected_items = "".join(
                f"<li><b><code>{escape(command)}</code></b>"
                + (f" — {escape(description)}" if description else "")
                + "</li>"
                for command, description in page_entries
            )
            assert f"<ul>{expected_items}</ul>" in text
            footer = f"<hr /><p><i>Page {page + 1} of {pages} • total {len(entries)}</i></p>"
            assert text.endswith(footer)
            assert "<table" not in text and "Use the Next button below" not in text
            for command, _description in page_entries:
                seen.add(command)
    assert any(command.startswith("/help") for command in seen)
    assert any(command.startswith("/guestbot") for command in seen)
    assert any(command.startswith("/search") for command in seen)
    assert seen == {command for _category, entries in sections for command, _description in entries}


def test_help_layout_preserves_usage_and_groups_threads_skills_and_overview():
    from plugins.platforms.telegram.rich_ui import help_sections, help_view, PAGE_SIZE

    sections = help_sections()
    session_index = next(i for i, (name, _entries) in enumerate(sections) if name == "Session")
    session_entries = sections[session_index][1]
    search_index = next(i for i, (name, _entries) in enumerate(sections) if name == "Search")
    search_count = len(sections[search_index][1])
    assert PAGE_SIZE == 5
    assert not any(command.split(" ", 1)[0] == "/search" for command, _ in session_entries)

    pages = (len(session_entries) + PAGE_SIZE - 1) // PAGE_SIZE
    # /subgoal carries a long usage string; render the page it actually sits
    # on (the Session catalog grows over time, so a fixed page goes stale).
    page = next(
        index // PAGE_SIZE
        for index, (command, _description) in enumerate(session_entries)
        if command.split(" ", 1)[0] == "/subgoal"
    )
    text, markup = help_view(str(session_index), page)

    assert markup is not None
    assert text.startswith(
        "<h1>Hermes Help</h1><p>Send a command below, or choose another section.</p>"
        "<hr /><h2>Session</h2>"
    )
    assert "<b><code>/subgoal [text | remove N | clear]</code></b>" in text
    footer = f"<hr /><p><i>Page {page + 1} of {pages} • total {len(session_entries)}</i></p>"
    assert text.endswith(footer)
    assert "<table" not in text and "Use the Next button below" not in text
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request
    native_request = send_request(types.PeerUser(42), text)
    assert type(native_request.rich_message).__name__ == "InputRichMessageHTML"
    assert native_request.rich_message.html.endswith(
        f"<p><i>Page {page + 1} of {pages} • total {len(session_entries)}</i></p>"
    )
    last_page, _ = help_view(str(session_index), pages - 1)
    assert "Use the Next button below to see more." not in last_page
    assert last_page.endswith(f"<hr /><p><i>Page {pages} of {pages} • total {len(session_entries)}</i></p>")
    rows = [[getattr(button, "button", button).text for button in row.buttons]
            for row in markup.rows]
    labels = [label for row in rows for label in row]
    assert f"Session · {len(session_entries)}" in labels
    assert f"Search · {search_count}" in labels
    markup_buttons = [getattr(button, "button", button)
                      for row in markup.rows for button in row.buttons]
    assert any(button.text == "Personal settings"
               and button.type.data == f"hm:personal:open:{session_index}:{page}".encode()
               for button in markup_buttons)

    home, home_markup = help_view()
    gateway_count = sum(len(entries) for name, entries in sections if name != "Skills")
    total_count = sum(len(entries) for _name, entries in sections)
    skill_count = sum(len(entries) for name, entries in sections if name == "Skills")
    assert home.startswith("<h1>Hermes Help</h1><p>")
    assert "<hr /><h2>Command catalog</h2>" in home
    assert f"<li><b>Gateway commands:</b> {gateway_count}</li>" in home
    assert f"<li><b>Skill commands:</b> {skill_count}</li>" in home
    assert home.endswith(f"<hr /><p><i>Page 1 of 1 • total {total_count}</i></p>")
    assert "<table" not in home and "All help" not in home
    home_rows = [[getattr(button, "button", button).text for button in row.buttons]
                 for row in home_markup.rows]
    special_labels = [f"{name} · {len(entries)}"
                      for name, entries in sections if name in {"Threads", "Skills"}]
    assert any(all(label in row for label in special_labels) and len(row) == 2 for row in home_rows)
    if not any(name == "Skills" for name, _entries in sections):
        assert any("Skills · 0" in row for row in home_rows)
    assert ["Overview"] in home_rows
    home_buttons = [getattr(button, "button", button)
                    for row in home_markup.rows for button in row.buttons]
    assert any(button.text == "Personal settings" and button.type.data == b"hm:personal:open:home:0"
               for button in home_buttons)
    stale, _ = help_view("all", 0)
    assert stale.startswith("<h1>Hermes Help</h1>") and "All help" not in stale

def test_guestbot_setting_is_saved_to_active_profile(monkeypatch, tmp_path):
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    source = SimpleNamespace(chat_id="-1001")

    adapter._persist_guestbot_setting(source, False)
    assert adapter._guestbot_enabled() is False
    saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "guest_mode: false" in saved

    adapter._persist_guestbot_setting(source, True)
    assert adapter._guestbot_enabled() is True
    assert "guest_mode: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_guest_mode_update_uses_authorized_ai_handler_and_native_result(monkeypatch):
    import json
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._authorized = lambda _source: True
    handled = []

    async def handle(event):
        handled.append(event)
        return "Hello **guest**\nMEDIA:/tmp/guest-secret.png"

    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace()

    adapter._client = Client()
    adapter._message_handler = handle
    message = SimpleNamespace(
        peer_id=types.PeerChat(chat_id=88), from_id=types.PeerUser(user_id=77),
        id=500, message="Can you help?", reply_to=None, reply_to_msg_id=None,
    )
    reference = SimpleNamespace(message="Earlier context", from_id=types.PeerUser(user_id=77))
    update = SimpleNamespace(query_id=1234, message=message, reference_messages=[reference])

    await adapter._on_guest_chat_query(update)

    assert len(handled) == 1
    assert handled[0].source.thread_id == "guest-77"
    payload = json.loads(handled[0].text.split("\n", 1)[1])
    assert payload["request_text"] == "Can you help?"
    assert payload["recent_context"] == [{"speaker": "guest", "text": "Earlier context"}]
    assert handled[0].metadata["telegram_guest_query"] is True
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "SetBotGuestChatResultRequest"
    assert calls[0].query_id == 1234
    assert type(calls[0].result.send_message).__name__ == "InputBotInlineMessageRichMessage"
    assert calls[0].result.send_message.rich_message.markdown == "Hello **guest**"


@pytest.mark.asyncio
async def test_guest_mode_disabled_answers_without_calling_ai():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={"guest_mode": False}))
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace()

    adapter._client = Client()
    adapter._message_handler = AsyncMock()
    message = SimpleNamespace(
        peer_id=types.PeerUser(user_id=77), from_id=types.PeerUser(user_id=77),
        id=1, message="hello", reply_to=None,
    )
    await adapter._on_guest_chat_query(SimpleNamespace(query_id=9, message=message))
    adapter._message_handler.assert_not_awaited()
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "SetBotGuestChatResultRequest"


@pytest.mark.asyncio
async def test_guest_mode_rejects_non_user_sender():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace()

    adapter._client = Client()
    adapter._message_handler = AsyncMock()
    message = SimpleNamespace(
        peer_id=types.PeerChat(chat_id=88), from_id=types.PeerChannel(channel_id=99),
        id=1, message="hello", reply_to=None,
    )
    await adapter._on_guest_chat_query(SimpleNamespace(query_id=10, message=message))
    adapter._message_handler.assert_not_awaited()
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "SetBotGuestChatResultRequest"


@pytest.mark.asyncio
async def test_guest_mode_serializes_requests_from_same_guest():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._authorized = lambda _source: True
    started = asyncio.Event()
    release = asyncio.Event()
    handled = []

    async def handle(event):
        handled.append(event)
        started.set()
        await release.wait()
        return "done"

    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace()

    adapter._client = Client()
    adapter._message_handler = handle

    def update(query_id, message_id):
        message = SimpleNamespace(
            peer_id=types.PeerChat(chat_id=88), from_id=types.PeerUser(user_id=77),
            id=message_id, message="hello", reply_to=None,
        )
        return SimpleNamespace(query_id=query_id, message=message)

    first = asyncio.create_task(adapter._on_guest_chat_query(update(1, 101)))
    await started.wait()
    await adapter._on_guest_chat_query(update(2, 102))
    assert len(handled) == 1
    assert len(calls) == 1
    assert "already processing" in calls[0].result.send_message.rich_message.markdown
    release.set()
    await first
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_help_callback_edits_as_native_rich_message_with_native_markup(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="-1001", user_id="6802929470", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace()

    adapter._client = Client()

    class Event:
        data = b"hm:help:0:0"
        message_id = 14

        async def answer(self, *args, **kwargs):
            return None

    await adapter._on_callback_query(Event())
    assert len(calls) == 1
    assert type(calls[0]).__name__ == "EditMessageRequest"
    assert "<h2>Session</h2>" in calls[0].rich_message.html
    assert "<table" not in calls[0].rich_message.html
    assert type(calls[0].rich_message).__name__ == "InputRichMessageHTML"
    assert calls[0].reply_markup.rows
    buttons = [getattr(button, "button", button)
               for row in calls[0].reply_markup.rows for button in row.buttons]
    assert any(button.text == "Personal settings" and button.type.data == b"hm:personal:open:0:0"
               for button in buttons)


@pytest.mark.asyncio
async def test_personal_settings_callback_routes_to_settings_ui(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="123", user_id="123", chat_type="dm", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_settings_ui", lambda: SimpleNamespace(handle_callback=handler))

    class Event:
        data = b"ps:17:memory-off"
        message_id = 55

        async def answer(self, *args, **kwargs):
            return None

    event = Event()
    await adapter._on_callback_query(event)
    handler.assert_awaited_once_with(event, source, "ps:17:memory-off")


@pytest.mark.asyncio
async def test_pv_models_callback_routes_to_provider_ui_unchanged(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="123", user_id="123", chat_type="dm", thread_id="17")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_control_routes", dict)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_provider_ui", lambda: SimpleNamespace(handle_callback=handler))

    class Event:
        data = b"pv:17:models:0"
        message_id = 55

        async def answer(self, *args, **kwargs):
            return None

    event = Event()
    await adapter._on_callback_query(event)
    handler.assert_awaited_once_with(event, source, "pv:17:models:0")


@pytest.mark.asyncio
async def test_unauthorized_personal_settings_callback_is_rejected(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="123", user_id="123", chat_type="dm", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "_settings_ui", lambda: SimpleNamespace(handle_callback=handler))
    answers = []

    class Event:
        data = b"ps:17:memory-off"
        message_id = 55

        async def answer(self, *args, **kwargs):
            answers.append((args, kwargs))

    await adapter._on_callback_query(Event())
    assert answers == [(("This action is not available for this account.",), {"alert": True})]
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_command_sends_native_rich_message_and_counted_keyboard(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="-1001", user_id="6802929470", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=15)

    adapter._client = Client()
    message = SimpleNamespace(message="/help", id=44)
    await adapter._on_new_message(SimpleNamespace(message=message))

    assert len(calls) == 1
    assert type(calls[0]).__name__ == "SendMessageRequest"
    assert calls[0].rich_message.html.startswith("<h1>Hermes Help</h1>")
    assert "<table" not in calls[0].rich_message.html
    assert type(calls[0].rich_message).__name__ == "InputRichMessageHTML"
    assert calls[0].entities is None
    assert calls[0].reply_to.reply_to_msg_id == 44
    assert calls[0].reply_markup.rows
    labels = [getattr(button, "button", button).text
              for row in calls[0].reply_markup.rows for button in row.buttons]
    from plugins.platforms.telegram.rich_ui import help_sections
    entries = next(entries for name, entries in help_sections() if name == "Session")
    assert f"Session · {len(entries)}" in labels


@pytest.mark.asyncio
async def test_bare_search_command_returns_rich_usage_without_starting_a_turn(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    from telethon import types

    source = SimpleNamespace(chat_id="-1001", user_id="6802929470", chat_type="group", thread_id="77")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    calls = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return SimpleNamespace(
                id=ids, peer_id=types.PeerUser(42),
                reply_to=SimpleNamespace(reply_to_top_id=77),
            )

        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=16)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    await adapter._on_new_message(SimpleNamespace(message=SimpleNamespace(message="/search", id=45)))

    assert len(calls) == 1
    request = calls[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    assert "<code>/search &lt;query&gt;</code>" in request.message
    assert "Example" in request.message
    assert "<code>/deepresearch &lt;question&gt;</code>" in request.message
    assert 'type="copy_text"' in request.message
    assert 'text="/search &lt;query&gt;"' in request.message
    assert "Copy search template" in request.message
    assert request.reply_to.reply_to_msg_id == 45
    assert request.reply_to.top_msg_id == 77


@pytest.mark.asyncio
async def test_guestbot_callback_is_authorized_before_persisting(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="-1001", user_id="999", chat_type="group")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)
    persist_calls = []
    monkeypatch.setattr(adapter, "_persist_guestbot_setting", lambda *args: persist_calls.append(args))
    answered = []

    class Event:
        data = b"hm:guestbot:off"
        message_id = 17

        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    await adapter._on_callback_query(Event())
    assert answered == [(("This action is not available for this account.",), {"alert": True})]
    assert not persist_calls


@pytest.mark.asyncio
async def test_exec_approval_buttons_resolve_exact_request_once_and_remove_keyboard(
    monkeypatch, tmp_path,
):
    import tools.approval as approval_module

    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "OWNER_TELEGRAM_ID=7\nTELEGRAM_ALLOWED_USERS=7\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=101)

    adapter._client = Client()
    result = await adapter.send_exec_approval(
        chat_id="7", command="python run-task.py", session_key="session-42",
        request_id="request-42", metadata={"user_id": "7"},
    )
    assert result.success and result.message_id == "101"
    prompt = calls[0]
    assert type(prompt).__name__ == "SendMessageRequest"
    buttons = [button for row in prompt.reply_markup.rows for button in row.buttons]
    callback = next(button.type.data for button in buttons if b":once:" in button.type.data)

    adapter.set_authorization_check(lambda user_id, *_args, **_kwargs: user_id == "7")
    resolved = []
    monkeypatch.setattr(
        approval_module, "resolve_gateway_approval",
        lambda *args, **kwargs: resolved.append((args, kwargs)) or 1,
    )

    class Event:
        data = callback
        message_id = 101

        def __init__(self):
            self.chat_id = 7
            self.sender_id = 7
            self.sender = SimpleNamespace(id=7, first_name="Owner", bot=False)
            self.chat = SimpleNamespace(id=7, first_name="Owner")
            self.message = SimpleNamespace(
                id=101, chat_id=7, reply_to=None,
            )
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    event = Event()
    await adapter._on_callback_query(event)
    assert resolved == [(("session-42", "once"), {"request_id": "request-42"})]
    assert type(calls[-1]).__name__ == "EditMessageRequest"
    assert calls[-1].reply_markup is None
    assert "Approved once" in calls[-1].rich_message.markdown
    assert adapter._approval_state == {}

    await adapter._on_callback_query(event)
    assert len(resolved) == 1
    assert event.answers[-1] == (("This approval has expired.",), {"alert": True})


@pytest.mark.asyncio
async def test_mtproto_create_topic_posts_welcome_in_new_private_topic():
    from datetime import datetime, timezone
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    created = types.Updates(
        updates=[types.UpdateNewMessage(types.MessageService(
            id=777, peer_id=types.PeerUser(42), date=datetime.now(UTC),
            action=types.MessageActionTopicCreate(title="personal items", icon_color=0),
        ), pts=1, pts_count=1)], users=[], chats=[],
        date=datetime.now(UTC), seq=1,
    )
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            if type(request).__name__ == "CreateForumTopicRequest":
                return created
            return SimpleNamespace(id=778)

    adapter._client = Client()
    source = SimpleNamespace(chat_id="42", chat_type="dm", thread_id=None)
    await adapter._create_topic_from_message(source, "personal items", reply_to=123)

    assert type(calls[0]).__name__ == "CreateForumTopicRequest"
    assert calls[0].title == "personal items"
    assert calls[0].random_id > 0
    assert type(calls[1]).__name__ == "SendMessageRequest"
    assert calls[1].reply_to.reply_to_msg_id == 777
    assert calls[1].reply_to.top_msg_id == 777


@pytest.mark.asyncio
async def test_mtproto_send_keeps_explicit_message_reply_and_topic_root():
    from datetime import datetime
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return types.Message(
                id=ids, peer_id=types.PeerUser(42), message="prior", date=datetime.now(),
                reply_to=types.MessageReplyHeader(reply_to_msg_id=5, reply_to_top_id=77),
            )

        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=88)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    result = await adapter.send("42", "reply", reply_to="18", metadata={"thread_id": "77"})
    assert result.success
    assert calls[0].reply_to.reply_to_msg_id == 18
    assert calls[0].reply_to.top_msg_id == 77


@pytest.mark.asyncio
async def test_explicit_thread_create_request_is_intercepted_before_agent_dispatch(monkeypatch):
    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="42", user_id="7", chat_type="dm", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    create_topic = AsyncMock()
    handle_message = AsyncMock()
    adapter._create_topic_from_message = create_topic
    adapter.handle_message = handle_message

    event = SimpleNamespace(message=SimpleNamespace(message='create me a thread named "personal items"', id=52))
    await adapter._on_new_message(event)

    create_topic.assert_awaited_once_with(source, "personal items", reply_to=52)
    handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_deepresearch_text_chunks_keep_forum_top_id_and_advance_task_chain():
    adapter = TelethonAdapter(PlatformConfig())
    from telethon import types
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return SimpleNamespace(id=ids, peer_id=types.PeerUser(42),
                                   reply_to=SimpleNamespace(reply_to_top_id=777))

        async def __call__(self, request):
            requests.append(request)
            return SimpleNamespace(id=900 + len(requests))

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    event_metadata = {
        "deep_research": {"session_id": "abc123abc123", "latest_reply_message_id": "700"},
        "telegram_task_reply_to_message_id": "700",
    }
    metadata = {
        "thread_id": "777",
        "deep_research": event_metadata["deep_research"],
        "deep_research_content": True,
        "telegram_task_reply_chain": True,
        "telegram_task_event_metadata": event_metadata,
    }

    first = await adapter.send("42", "First report chunk", metadata=metadata)
    second = await adapter.send("42", "Second report chunk", metadata=metadata)

    assert first.success and first.message_id == "901"
    assert second.success and second.message_id == "902"
    assert len(requests) == 2
    assert requests[0].reply_to.reply_to_msg_id == 700
    assert requests[0].reply_to.top_msg_id == 777
    assert requests[1].reply_to.reply_to_msg_id == 901
    assert requests[1].reply_to.top_msg_id == 777
    assert event_metadata["deep_research"]["latest_reply_message_id"] == "902"
    assert event_metadata["telegram_task_reply_to_message_id"] == "902"


@pytest.mark.asyncio
async def test_deepresearch_report_document_uses_topic_header_and_advances_anchor(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    from telethon import types
    requests = []

    class Client:
        async def get_messages(self, peer, *, ids):
            return SimpleNamespace(id=ids, peer_id=types.PeerUser(42),
                                   reply_to=SimpleNamespace(reply_to_top_id=777))

        async def upload_file(self, _path):
            return "uploaded-file"

        def _parse_message_text(self, text, _mode):
            return text, []

        async def __call__(self, request):
            requests.append(request)
            return SimpleNamespace(id=801)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))
    report = tmp_path / "report.md"
    report.write_text("# Research report", encoding="utf-8")
    event_metadata = {"deep_research": {"session_id": "abc123abc123", "latest_reply_message_id": "700"}}
    metadata = {
        "thread_id": "777",
        "deep_research": event_metadata["deep_research"],
        "deep_research_content": True,
        "telegram_task_reply_chain": True,
        "telegram_task_event_metadata": event_metadata,
    }

    result = await adapter.send_document("42", str(report), caption="Deep research report", metadata=metadata)

    assert result.success and result.message_id == "801"
    assert len(requests) == 1
    request = requests[0]
    assert type(request).__name__ == "SendMediaRequest"
    assert request.reply_to.reply_to_msg_id == 700
    assert request.reply_to.top_msg_id == 777
    assert event_metadata["deep_research"]["latest_reply_message_id"] == "801"


@pytest.mark.asyncio
async def test_progress_card_floodwait_is_returned_without_inline_sleep():
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class FloodWait(Exception):
        seconds = 17

    class Client:
        async def __call__(self, _request):
            calls.append("attempt")
            raise FloodWait()

    adapter._client = Client()
    started = asyncio.get_running_loop().time()
    result = await adapter.send("42", "Status", metadata={"telegram_progress_card": True})
    elapsed = asyncio.get_running_loop().time() - started

    assert not result.success
    assert result.retry_after == 17
    assert len(calls) == 1
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_topic_creation_retries_same_idempotent_request_after_floodwait():
    from datetime import datetime
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class FloodWait(Exception):
        seconds = 0.001

    class Client:
        async def __call__(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise FloodWait()
            if len(requests) == 2:
                return types.Updates(
                    updates=[types.UpdateNewMessage(types.MessageService(
                        id=777, peer_id=types.PeerUser(42), date=datetime.now(),
                        action=types.MessageActionTopicCreate(title="Research topic", icon_color=0),
                    ), pts=1, pts_count=1)], users=[], chats=[], date=datetime.now(), seq=1,
                )
            return SimpleNamespace(id=888)

    adapter._client = Client()
    source = SimpleNamespace(chat_id="42", chat_type="group", thread_id=None)
    topic_id = await adapter._create_topic_from_message(
        source, "Research topic", reply_to="55", random_id=123456,
    )

    assert topic_id == 777
    assert len(requests) == 3  # create retry plus one topic welcome message
    assert requests[0] is requests[1]
    assert requests[0].random_id == requests[1].random_id == 123456
    assert adapter._topic_welcome_message_id("42", "777") == "888"


@pytest.mark.asyncio
async def test_private_topic_creation_error_names_botfather_threaded_mode():
    adapter = TelethonAdapter(PlatformConfig())
    captured = []

    class ThreadedModeDisabledError(Exception):
        pass

    class Client:
        async def __call__(self, _request):
            raise ThreadedModeDisabledError()

    adapter._client = Client()

    async def capture_send(*args, **kwargs):
        captured.append((args, kwargs))
        return SimpleNamespace(success=False, error="not delivered")

    adapter.send = capture_send
    source = SimpleNamespace(chat_id="42", chat_type="dm", thread_id=None)
    topic_id = await adapter._create_topic_from_message(source, "Private research", reply_to="55")

    assert topic_id is None
    assert "BotFather" in adapter._topic_creation_error
    assert "Threaded Mode" in adapter._topic_creation_error
    assert "BotFather" in captured[0][0][1]
    assert "Threaded Mode" in captured[0][0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["settings", "skills"])
async def test_personal_menu_entry_tracks_exact_incoming_slash_command(monkeypatch, command):
    from unittest.mock import Mock

    adapter = TelethonAdapter(PlatformConfig())
    source = SimpleNamespace(chat_id="42", user_id="42", chat_type="dm", thread_id=None)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_command_menu", AsyncMock())
    settings = SimpleNamespace(open=AsyncMock(), handle_message=AsyncMock(return_value=False),
                               cancel_for_source=Mock())
    skills = SimpleNamespace(open=AsyncMock(), handle_message=AsyncMock(return_value=False),
                             cancel_for_source=Mock())
    monkeypatch.setattr(adapter, "_settings_ui", lambda: settings)
    monkeypatch.setattr(adapter, "_skills_ui", lambda: skills)
    adapter._client = SimpleNamespace()
    message = SimpleNamespace(message=f"/{command}", id=44)
    await adapter._on_new_message(SimpleNamespace(message=message))
    menu = settings if command == "settings" else skills
    menu.open.assert_awaited_once_with(source, reply_to=44, invocation_message_id=44)


def test_quoted_excerpt_reads_the_reply_header_only():
    from telethon import types

    message = types.Message(
        id=9, peer_id=types.PeerUser(42), message="full message text", date=None,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=7, quote_text="  quoted excerpt  ", quote_offset=5),
    )
    assert TelethonAdapter._quoted_excerpt(message) == "quoted excerpt"


@pytest.mark.parametrize("message", [
    SimpleNamespace(reply_to=None),
    SimpleNamespace(reply_to=SimpleNamespace()),
    SimpleNamespace(reply_to=SimpleNamespace(quote_text="   ")),
    SimpleNamespace(reply_to=SimpleNamespace(quote_text=42)),
    SimpleNamespace(),
])
def test_quoted_excerpt_is_none_without_a_real_quote(message):
    assert TelethonAdapter._quoted_excerpt(message) is None
