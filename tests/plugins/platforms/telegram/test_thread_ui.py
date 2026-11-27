from contextlib import nullcontext
import asyncio
from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions

from plugins.platforms.telegram.thread_ui import TelegramThreadUI


NOW = datetime(2026, 10, 22, tzinfo=UTC)


def _topic(topic_id, title=None, *, closed=False, top_message=None, is_general=False):
    return SimpleNamespace(
        id=topic_id, title=title or f"Topic {topic_id}", top_message=top_message or topic_id + 1000,
        date=NOW, closed=closed, hidden=False, unread_count=2, is_general=is_general,
    )


class FakeAdapter:
    def __init__(self, topics=None, count=None):
        self.topics = list(topics or [_topic(2)])
        self.count = len(self.topics) if count is None else count
        self.calls = []
        self.messages = []
        self.client = self
        self._client = self
        self.home = "/tmp/telegram-thread-ui-profile"
        self.next_message_id = 900
        self.created = []

    def _is_owner_user(self, user_id, source):
        return str(user_id) == "111"

    def _authorized(self, source):
        return True

    def _settings_home(self, source):
        return self.home

    def _peer_for(self, chat_id):
        return f"peer:{chat_id}"

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            raise errors.BotMethodInvalidError(request)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            rows = [row for row in self.topics if row.id in request.topics]
            return SimpleNamespace(topics=rows)
        if isinstance(request, functions.messages.DeleteTopicHistoryRequest):
            self.topics = [row for row in self.topics if row.id != request.top_msg_id]
            self.count = len(self.topics)
        return SimpleNamespace()

    async def _create_topic_from_message(self, source, title, **kwargs):
        self.created.append((source, title, kwargs))

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self.next_message_id += 1
        self.messages.append((source, text, markup, reply_to))
        return SimpleNamespace(id=self.next_message_id)

    async def _edit_rich_ui_message(self, event, source, text, markup):
        self.messages.append((source, text, markup, "edit"))

    async def _mtproto_rich_request(self, request):
        self.calls.append(request)
        return SimpleNamespace()


def _source(*, chat_id="-10012345", user_id="111", chat_type="group", thread_id=None):
    return SimpleNamespace(chat_id=chat_id, user_id=user_id, chat_type=chat_type,
                           thread_id=thread_id, is_bot=False)


def _event(message_id="901"):
    return SimpleNamespace(message_id=message_id, answer=AsyncMock())


@pytest.fixture

def ui(monkeypatch, tmp_path):
    adapter = FakeAdapter([_topic(i) for i in range(2, 12)], count=10)
    adapter.home = str(tmp_path)
    instance = TelegramThreadUI(adapter)
    monkeypatch.setattr(instance, "_profile_scope", lambda _home: nullcontext())
    monkeypatch.setattr(instance, "_known_topic_ids", lambda _state: [row.id for row in adapter.topics])
    return instance, adapter


@pytest.mark.asyncio
async def test_list_is_paginated_with_live_topic_lookup_and_footer(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "list", reply_to="7")

    state = next(iter(instance._states.values()))
    first_request = next(call for call in adapter.calls if isinstance(call, functions.messages.GetForumTopicsByIDRequest))
    assert first_request.peer == "peer:-10012345"
    assert first_request.topics == list(range(2, 12))
    assert len(state["page_topics"]) == 8
    assert "<i>Page 1 of 2 • total 10</i>" in adapter.messages[-1][1]
    assert "*Page 1" not in adapter.messages[-1][1]

    event = _event(str(state["message_id"]))
    await instance.handle_callback(event, _source(), f"hm:thread:{state['id']}:page:1")
    page_requests = [call for call in adapter.calls if isinstance(call, functions.messages.GetForumTopicsByIDRequest)]
    assert len(page_requests) == 2
    assert [row.id for row in state["page_topics"]] == [10, 11]
    assert "<i>Page 2 of 2 • total 10</i>" in adapter.messages[-1][1]
    event.answer.assert_awaited_once_with("Loading topic page…")


@pytest.mark.asyncio
async def test_callback_rejects_other_account_chat_and_profile_without_mutation(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "list")
    state = next(iter(instance._states.values()))
    before = len(adapter.calls)
    event = _event(str(state["message_id"]))

    await instance.handle_callback(event, _source(user_id="222"), f"hm:thread:{state['id']}:close:2")
    await instance.handle_callback(event, _source(chat_id="-10099999"), f"hm:thread:{state['id']}:close:2")
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: None)
    await instance.handle_callback(event, _source(), f"hm:thread:{state['id']}:close:2")
    assert len(adapter.calls) == before
    assert event.answer.await_count == 3
    assert all(call.kwargs.get("alert") for call in event.answer.call_args_list)


@pytest.mark.asyncio
async def test_rename_and_close_use_native_forum_topic_requests(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "rename 2 Planning session")
    rename = next(call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest))
    assert rename.topic_id == 2 and rename.title == "Planning session"

    await instance.handle_command(_source(), "close 2")
    close = [call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest)][-1]
    assert close.topic_id == 2 and close.closed is True

    await instance.handle_command(_source(), "reopen 2")
    reopen = [call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest)][-1]
    assert reopen.topic_id == 2 and reopen.closed is False


@pytest.mark.asyncio
async def test_delete_requires_a_separate_one_shot_confirmation(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "delete 2")
    state = next(iter(instance._states.values()))
    delete_requests = [call for call in adapter.calls if isinstance(call, functions.messages.DeleteTopicHistoryRequest)]
    assert delete_requests == []
    assert "permanently deleted" in adapter.messages[-1][1]
    assert "<hr>" in adapter.messages[-1][1]
    assert "<i>Page 1 of 1 • total 1</i>" in adapter.messages[-1][1]
    confirmation = state["confirmation"]
    event = _event(str(state["message_id"]))
    payload = f"hm:thread:{state['id']}:delete-confirm:{confirmation}"

    await instance.handle_callback(event, _source(), payload)
    delete_requests = [call for call in adapter.calls if isinstance(call, functions.messages.DeleteTopicHistoryRequest)]
    assert len(delete_requests) == 1
    assert delete_requests[0].top_msg_id == 2

    # Replaying the exact callback cannot consume a second deletion.
    await instance.handle_callback(event, _source(), payload)
    assert len([call for call in adapter.calls if isinstance(call, functions.messages.DeleteTopicHistoryRequest)]) == 1


@pytest.mark.asyncio
async def test_open_selects_topic_by_posting_into_its_native_thread(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "open 2")
    selected_source, text, _markup, reply_to = adapter.messages[-1]
    assert selected_source.thread_id == "2"
    assert reply_to == 2
    assert "Topic selected" in text


@pytest.mark.asyncio
async def test_create_command_delegates_to_existing_adapter_native_path_without_new_emoji(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "create A topic", reply_to="8")
    assert len(adapter.created) == 1
    _, title, kwargs = adapter.created[0]
    assert title == "A topic"
    assert kwargs["reply_to"] == "8"
    assert "✅" not in kwargs["welcome_text"]


def test_topic_source_copy_keeps_wire_invisible_profile_provenance():
    source = _source(thread_id="4")
    source._identity = object()
    source._transport_adapter_ref = object()
    source._authorization_profile_home = "/profile/owner"
    copied = TelegramThreadUI._topic_context(source, 8)
    assert copied.thread_id == "8"
    assert copied._identity is source._identity
    assert copied._transport_adapter_ref is source._transport_adapter_ref
    assert copied._authorization_profile_home == source._authorization_profile_home


@pytest.mark.asyncio
async def test_non_owner_cannot_open_thread_ui(ui):
    instance, adapter = ui
    before = len(adapter.calls)
    await instance.handle_command(_source(user_id="222"), "list")
    assert len(adapter.calls) == before
    assert "restricted" in adapter.messages[-1][1]


@pytest.mark.asyncio
async def test_explicit_list_page_uses_stable_known_ids_and_live_lookup(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "list 2")
    state = next(iter(instance._states.values()))
    request = [call for call in adapter.calls if isinstance(call, functions.messages.GetForumTopicsByIDRequest)][-1]
    assert state["page"] == 1
    assert request.topics == list(range(2, 12))
    assert [row.id for row in state["page_topics"]] == [10, 11]


@pytest.mark.asyncio
async def test_archive_callback_is_one_shot_and_sets_general_hidden_true(monkeypatch):
    adapter = FakeAdapter([_topic(1, is_general=True)])
    instance = TelegramThreadUI(adapter)
    monkeypatch.setattr(instance, "_profile_scope", lambda _home: nullcontext())
    monkeypatch.setattr(instance, "_known_topic_ids", lambda _state: [1])
    await instance.handle_command(_source(), "list")
    state = next(iter(instance._states.values()))
    old_token = state["id"]
    payload = f"hm:thread:{old_token}:archive:1"
    event = _event(str(state["message_id"]))

    await asyncio.gather(
        instance.handle_callback(event, _source(), payload),
        instance.handle_callback(event, _source(), payload),
    )

    changes = [call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest)]
    assert len(changes) == 1
    assert changes[0].topic_id == 1 and changes[0].hidden is True
    assert old_token not in instance._states
    assert state["id"] != old_token


@pytest.mark.asyncio
async def test_selected_topic_finalizes_originating_card_and_restores_missing_thread(ui):
    instance, adapter = ui
    await instance.handle_command(_source(thread_id="4"), "list")
    state = next(iter(instance._states.values()))
    payload = f"hm:thread:{state['id']}:select:2"

    await instance.handle_callback(_event(str(state["message_id"])), _source(), payload)

    card_source, card_text, _markup, operation = adapter.messages[-1]
    assert operation == "edit"
    assert card_source.thread_id == "4"
    assert "Opened topic 2." in card_text
    welcome_source, welcome_text, _welcome_markup, reply_to = adapter.messages[-2]
    assert welcome_source.thread_id == "2"
    assert "Topic selected" in welcome_text
    assert reply_to == 2


@pytest.mark.asyncio
async def test_explicit_unarchive_sets_general_hidden_false(ui):
    instance, adapter = ui
    general = _topic(1, is_general=True)
    general.hidden = True
    adapter.topics = [general]
    adapter.count = 1

    await instance.handle_command(_source(), "unarchive 1")

    change = next(call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest))
    assert change.topic_id == 1 and change.hidden is False
    assert "Topic 1 unarchived successfully." in adapter.messages[-1][1]


@pytest.mark.asyncio
async def test_help_commands_render_as_html_code_not_literal_markdown(ui):
    instance, adapter = ui
    await instance.handle_command(_source(), "help")
    text = adapter.messages[-1][1]
    assert "<code>/thread create &lt;title&gt;</code>" in text
    assert "<code>/thread list [page]</code>" in text
    assert "<code>/thread unarchive 1</code>" in text
    assert "`/thread" not in text


@pytest.mark.asyncio
async def test_callback_rejects_different_source_profile_even_with_same_home(ui):
    instance, adapter = ui
    original = _source()
    original.profile = "profile-a"
    await instance.handle_command(original, "list")
    state = next(iter(instance._states.values()))
    before = len(adapter.calls)
    callback_source = _source()
    callback_source.profile = "profile-b"

    event = _event(str(state["message_id"]))
    await instance.handle_callback(
        event, callback_source,
        f"hm:thread:{state['id']}:close:2",
    )

    assert len(adapter.calls) == before
    event.answer.assert_awaited_once()
    assert event.answer.call_args.kwargs["alert"] is True


@pytest.mark.asyncio
async def test_missing_callback_topic_is_restored_before_profile_resolution(ui, monkeypatch):
    instance, adapter = ui
    monkeypatch.setattr(adapter, "_settings_home", lambda source:
                        adapter.home if str(source.thread_id or "") == "44" else "/tmp/other-thread-profile")
    source = _source(thread_id="44")
    await instance.handle_command(source, "list")
    state = next(iter(instance._states.values()))
    data = f"hm:thread:{state['id']}:close:2"
    await instance.handle_callback(_event("not-the-card"), _source(), data)
    assert not any(isinstance(call, functions.messages.EditForumTopicRequest) for call in adapter.calls)
    await instance.handle_callback(_event(str(state["message_id"])), _source(), data)
    mutations = [call for call in adapter.calls if isinstance(call, functions.messages.EditForumTopicRequest)]
    assert len(mutations) == 1 and mutations[0].topic_id == 2
    assert str(adapter.messages[-1][0].thread_id) == "44"
