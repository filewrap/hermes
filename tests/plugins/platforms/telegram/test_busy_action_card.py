"""Tests for Telegram MTProto busy action card and callback actions."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import types

from gateway.platforms.base import Platform
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

# The fork's zero-emoji convention: no emoji codepoints in any user-facing
# busy-card label or card text. Covers emoticons/pictographs, dingbats and
# misc symbols, arrows, and the variation selector / zero-width joiner that
# turn a plain symbol into an emoji presentation.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


def _assert_emoji_free(*texts: str) -> None:
    for text in texts:
        assert not _EMOJI_RE.search(text or ""), f"emoji found in: {text!r}"


class FakeClient:
    def __init__(self):
        self.sent_messages = []

    async def __call__(self, request):
        self.sent_messages.append(request)
        return SimpleNamespace(id=101)


@pytest.fixture
def fake_adapter():
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._client = FakeClient()
    adapter.config = SimpleNamespace(extra={})
    adapter.gateway_runner = Mock()
    adapter._busy_action_tokens = {}
    adapter._session_busy_action_cards = {}
    adapter._peer_for = lambda _chat_id: types.PeerUser(12345)
    adapter._authorized = lambda _src: True
    adapter._is_owner_user = lambda _uid, _src=None: True
    adapter._route_control_request = None
    adapter._sent_message_id = lambda res, **kw: str(res.id)
    return adapter


@pytest.mark.asyncio
async def test_send_busy_action_card_creates_rich_message_and_tokens(fake_adapter):
    source = SimpleNamespace(chat_id="12345", user_id="12345", thread_id=None, profile="main", platform=Platform.TELEGRAM, chat_type="dm")
    event = SimpleNamespace(
        source=source,
        text="What is the weather?",
        message_id=999,
        created=1000.0,
    )

    result = await fake_adapter.send_busy_action_card(
        event=event,
        message="Hermes is already working on a task.",
        running_agent=SimpleNamespace(),
    )
    assert result is True
    assert len(fake_adapter._client.sent_messages) == 1
    sent_request = fake_adapter._client.sent_messages[0]

    markup = getattr(sent_request, "reply_markup", None)
    assert markup is not None
    callbacks = []
    for row in markup.rows:
        for btn in row.buttons:
            if isinstance(btn.type, types.InlineButtonTypeCallback):
                callbacks.append(btn.type.data.decode("ascii"))

    assert any(c.startswith("hm:busy:inject:") for c in callbacks)
    assert any(c.startswith("hm:busy:now:") for c in callbacks)
    assert any(c.startswith("hm:busy:queue:") for c in callbacks)
    assert any(c.startswith("hm:busy:stop:") for c in callbacks)
    # hm:run:/ nav buttons removed: the busy card carries no re-dispatch buttons.
    assert not any(c.startswith("hm:run:") for c in callbacks)
    assert "__Run /status" in sent_request.message  # italic hint replaces the buttons


@pytest.mark.asyncio
async def test_handle_busy_action_inject(fake_adapter):
    source = SimpleNamespace(chat_id="12345", user_id="12345", thread_id=None, profile="main", platform=Platform.TELEGRAM, chat_type="dm")
    agent = Mock()
    agent.is_busy = Mock(return_value=True)
    agent.steer = Mock(return_value=True)

    event_obj = SimpleNamespace(source=source, text="Please also check humidity")
    token = "abcdef0123"
    session_key = "agent:main:telegram:dm:12345"
    fake_adapter._busy_action_tokens[token] = {
        "session_key": session_key,
        "event": event_obj,
        "text": event_obj.text,
        "created_at": 1000.0,
    }

    fake_runner = Mock()
    fake_runner._peek_session_state = Mock(return_value=SimpleNamespace(turn=SimpleNamespace(agent=agent)))
    fake_runner.pending_events = {session_key: [event_obj]}
    fake_adapter.gateway_runner = fake_runner

    cb_event = Mock()
    cb_event.message_id = 555
    cb_event.answer = AsyncMock()
    fake_adapter._mtproto_rich_request = AsyncMock()

    await fake_adapter._handle_busy_action_callback(cb_event, source, f"hm:busy:inject:{token}")
    agent.steer.assert_called_once_with("Please also check humidity")
    cb_event.answer.assert_called_once()
    assert token not in fake_adapter._busy_action_tokens


@pytest.mark.asyncio
async def test_handle_busy_action_stop(fake_adapter):
    source = SimpleNamespace(chat_id="12345", user_id="12345", thread_id=None, profile="main", platform=Platform.TELEGRAM, chat_type="dm")
    agent = Mock()
    agent.is_busy = Mock(return_value=True)

    event_obj = SimpleNamespace(source=source, text="stop task")
    token = "stoptoken1"
    session_key = "agent:main:telegram:dm:12345"
    fake_adapter._busy_action_tokens[token] = {
        "session_key": session_key,
        "event": event_obj,
        "text": event_obj.text,
        "created_at": 1000.0,
    }

    fake_runner = Mock()
    fake_runner._peek_session_state = Mock(return_value=SimpleNamespace(turn=SimpleNamespace(agent=agent)))
    fake_adapter.gateway_runner = fake_runner

    cb_event = Mock()
    cb_event.message_id = 556
    cb_event.answer = AsyncMock()
    fake_adapter._mtproto_rich_request = AsyncMock()

    await fake_adapter._handle_busy_action_callback(cb_event, source, f"hm:busy:stop:{token}")
    agent.interrupt.assert_called_once_with("Stopped by user")
    fake_runner._release_running_agent_state.assert_called_once_with(session_key)
    cb_event.answer.assert_called_once()


@pytest.mark.asyncio
async def test_busy_action_card_labels_body_and_edits_are_emoji_free(fake_adapter):
    source = SimpleNamespace(chat_id="12345", user_id="12345", thread_id=None, profile="main", platform=Platform.TELEGRAM, chat_type="dm")
    event = SimpleNamespace(source=source, text="What is the weather?", message_id=999, created=1000.0)

    assert await fake_adapter.send_busy_action_card(
        event=event, message="Hermes is already working on a task.", running_agent=SimpleNamespace(),
    )
    sent_request = fake_adapter._client.sent_messages[0]
    labels = [btn.text for row in sent_request.reply_markup.rows for btn in row.buttons]
    assert labels, "busy card must carry action buttons"
    _assert_emoji_free(*labels, sent_request.message or "")

    # The post-action card edits (inject / answer-now / stop) are user-facing
    # text too: they must stay emoji-free as well.
    agent = Mock()
    agent.is_busy = Mock(return_value=True)
    agent.steer = Mock(return_value=True)
    fake_runner = Mock()
    fake_runner._peek_session_state = Mock(return_value=SimpleNamespace(turn=SimpleNamespace(agent=agent)))
    fake_runner._interrupt_running_agent_for_busy_event = AsyncMock()
    fake_adapter.gateway_runner = fake_runner
    fake_adapter._mtproto_rich_request = AsyncMock()

    for action, token in (("inject", "emojitok1"), ("now", "emojitok2"), ("stop", "emojitok3")):
        event_obj = SimpleNamespace(source=source, text="Please also check humidity")
        fake_adapter._busy_action_tokens[token] = {
            "session_key": "agent:main:telegram:dm:12345",
            "event": event_obj,
            "text": event_obj.text,
            "created_at": 1000.0,
        }
        cb_event = Mock()
        cb_event.message_id = 557
        cb_event.answer = AsyncMock()
        await fake_adapter._handle_busy_action_callback(cb_event, source, f"hm:busy:{action}:{token}")
        edit_request = fake_adapter._mtproto_rich_request.await_args.args[0]
        _assert_emoji_free(edit_request.message or "")
