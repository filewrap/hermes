from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _adapter(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = object()
    adapter._peer_for = lambda chat_id: ("peer", str(chat_id))
    adapter._mtproto_rich_request = AsyncMock()
    return adapter


def _source():
    return SimpleNamespace(
        chat_id="100", user_id="100", thread_id=None, chat_type="dm",
        profile="alpha", platform="telegram",
    )


def _html(request):
    rich = request.rich_message
    return getattr(rich, "html", None) or getattr(rich, "markdown", None) or request.message


@pytest.mark.asyncio
async def test_model_picker_selection_edits_working_card_then_readable_result(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    selected = AsyncMock(return_value="Model switched to model-x.")
    adapter._model_picker_state[7] = {
        "chat_id": "100", "user_id": "100", "thread_id": None,
        "session_key": "session-a", "providers": [], "current_model": "old",
        "current_provider": "openrouter", "selected_provider": "openrouter",
        "model_list": ["model-x"], "on_model_selected": selected,
        "stage": "model", "page": 0, "message_id": "55",
        "created_at": time.monotonic(), "consuming": False,
    }
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.combined_selection_warning",
        lambda *_args, **_kwargs: None,
    )
    event = SimpleNamespace(message_id=55, answer=AsyncMock())

    await adapter._handle_model_picker_callback(event, "mp:7:select:0")

    assert selected.await_count == 1
    assert adapter._mtproto_rich_request.await_count == 2
    requests = [call.args[0] for call in adapter._mtproto_rich_request.await_args_list]
    assert "Working" in _html(requests[0])
    assert "Applying model" in _html(requests[0])
    # The pick re-renders the model page with the new model green; the card
    # keeps its buttons instead of dropping to button-less result text.
    assert "Model selection complete" not in _html(requests[1])
    assert "- **Model:** **`model-x`**" in _html(requests[1])
    assert requests[1].reply_markup is not None and requests[1].reply_markup.rows
    buttons = [button for row in requests[1].reply_markup.rows for button in row.buttons]
    picked = next(button for button in buttons if button.text == "model-x")
    assert picked.style.bg_success is True
    assert 7 in adapter._model_picker_state
    assert adapter._model_picker_state[7]["current_model"] == "model-x"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved,expected", [(1, "Approved once"), (0, "no longer pending")])
async def test_exec_approval_edits_working_card_then_readable_outcome(tmp_path, monkeypatch, resolved, expected):
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._settings_home = lambda _source: str(tmp_path)
    adapter._approval_state[5] = {
        "owner_user_id": "100", "intended_user_id": "100", "owner_session": False,
        "profile_home": str(Path(tmp_path).resolve()), "allowed_choices": {"once"},
        "chat_id": "100", "thread_id": None, "message_id": "55",
        "session_key": "session-a", "request_id": "request-a",
    }
    event = SimpleNamespace(message_id=55, answer=AsyncMock())
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *_args, **_kwargs: resolved)

    await adapter._handle_exec_approval_callback(event, "ea:once:5")

    requests = [call.args[0] for call in adapter._mtproto_rich_request.await_args_list]
    assert len(requests) == 2
    assert "Working" in _html(requests[0])
    assert "approval" in _html(requests[0]).lower()
    assert expected.lower() in _html(requests[1]).lower()


@pytest.mark.asyncio
async def test_exec_approval_owner_approves_when_stored_owner_is_empty(tmp_path, monkeypatch):
    """The owner must be able to approve even when the card was built with an
    empty stored owner (ambient home had no OWNER_TELEGRAM_ID at send time):
    the handler re-derives the owner from the tapper's profile home."""
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._settings_home = lambda _source: str(tmp_path)
    adapter._approval_state[5] = {
        "owner_user_id": "", "intended_user_id": "", "owner_session": True,
        "profile_home": str(Path(tmp_path).resolve()), "allowed_choices": {"once"},
        "chat_id": "100", "thread_id": None, "message_id": "55",
        "session_key": "session-a", "request_id": "request-a",
    }
    event = SimpleNamespace(message_id=55, answer=AsyncMock())
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *_args, **_kwargs: 1)

    await adapter._handle_exec_approval_callback(event, "ea:once:5")

    assert 5 not in adapter._approval_state
    requests = [call.args[0] for call in adapter._mtproto_rich_request.await_args_list]
    assert "Approved once" in _html(requests[-1])


@pytest.mark.asyncio
async def test_exec_approval_second_tap_is_expired(tmp_path, monkeypatch):
    """An answered approval is dead: a second tap is rejected, it cannot
    resolve or execute twice."""
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._settings_home = lambda _source: str(tmp_path)
    adapter._approval_state[5] = {
        "owner_user_id": "100", "intended_user_id": "100", "owner_session": False,
        "profile_home": str(Path(tmp_path).resolve()), "allowed_choices": {"once"},
        "chat_id": "100", "thread_id": None, "message_id": "55",
        "session_key": "session-a", "request_id": "request-a",
    }
    event = SimpleNamespace(message_id=55, answer=AsyncMock())
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *_args, **_kwargs: 1)

    await adapter._handle_exec_approval_callback(event, "ea:once:5")
    await adapter._handle_exec_approval_callback(event, "ea:once:5")

    answers = [str(call.args[0]) for call in event.answer.await_args_list]
    assert any("has expired" in text for text in answers)


@pytest.mark.asyncio
async def test_exec_approval_denied_tap_leaves_card_armed_for_owner(tmp_path, monkeypatch):
    """A denied tap must not consume the approval: the guard stays live for
    the owner instead of going stale."""
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    adapter = _adapter(tmp_path)
    stranger = SimpleNamespace(
        chat_id="100", user_id="200", thread_id=None, chat_type="dm",
        profile="alpha", platform="telegram",
    )
    adapter._source = AsyncMock(return_value=stranger)
    adapter._authorized = lambda _source: True
    adapter._settings_home = lambda _source: str(tmp_path)
    adapter._approval_state[5] = {
        "owner_user_id": "", "intended_user_id": "", "owner_session": True,
        "profile_home": str(Path(tmp_path).resolve()), "allowed_choices": {"once"},
        "chat_id": "100", "thread_id": None, "message_id": "55",
        "session_key": "session-a", "request_id": "request-a",
    }
    event = SimpleNamespace(message_id=55, answer=AsyncMock())
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda *_args, **_kwargs: resolved.append(1) or 1,
    )

    await adapter._handle_exec_approval_callback(event, "ea:once:5")

    answers = [str(call.args[0]) for call in event.answer.await_args_list]
    assert any("Only the owner" in text for text in answers)
    assert 5 in adapter._approval_state, "a denied tap must not pop the pending approval"
    assert resolved == []

    adapter._source = AsyncMock(return_value=_source())
    await adapter._handle_exec_approval_callback(event, "ea:once:5")
    assert resolved == [1]
    assert 5 not in adapter._approval_state


@pytest.mark.asyncio
async def test_send_exec_approval_prompt_prefers_metadata_owner_and_home(tmp_path, monkeypatch):
    """Cards built from turn-stamped metadata keep the session's owner and
    profile home even when the ambient home has no owner configured."""
    from gateway.platforms.base import ExecApprovalPrompt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(tmp_path)
    adapter._client = object()
    prompt = ExecApprovalPrompt(
        chat_id="100", session_key="session-a", text="Approve?",
        actions=[("Allow Once", "once", "primary"), ("Deny", "deny", "danger")],
        command="rm x", description="dangerous", smart_denied=False,
        metadata={"user_id": "100", "owner_user_id": "100", "profile_home": str(tmp_path)},
        request_id="request-a",
    )

    result = await adapter._send_exec_approval_prompt(prompt)

    assert result.success is True
    state = next(iter(adapter._approval_state.values()))
    assert state["owner_user_id"] == "100"
    assert state["intended_user_id"] == "100"
    assert state["profile_home"] == str(Path(tmp_path).resolve())


def test_task_delivery_failure_marker_is_monotonic_after_successful_sends():
    metadata = {
        "deep_research_content": True,
        "telegram_task_event_metadata": {
            "telegram_task_delivery_failure": "Telegram topic delivery failed",
        },
    }

    TelethonAdapter._clear_task_delivery_failure(metadata)

    assert metadata["telegram_task_event_metadata"]["telegram_task_delivery_failure"] == "Telegram topic delivery failed"


def _has_emoji(text):
    """Characters in the Unicode emoji ranges (incl. Dingbats)."""
    for ch in str(text):
        code = ord(ch)
        if (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
                or code in (0xFE0F, 0x200D)
                or 0x2190 <= code <= 0x21FF or 0x2300 <= code <= 0x23FF
                or 0x2B00 <= code <= 0x2BFF or 0x1F100 <= code <= 0x1F1FF):
            return True
    return False


@pytest.mark.asyncio
async def test_model_picker_selection_rerenders_with_new_model_green(tmp_path, monkeypatch):
    """After a successful pick the card must keep its buttons and mark the NEW
    model green (success) — not drop to button-less result text."""
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    selected = AsyncMock(return_value="Model switched to model-x.")
    adapter._model_picker_state[7] = {
        "chat_id": "100", "user_id": "100", "thread_id": None,
        "session_key": "session-a", "providers": [], "current_model": "old",
        "current_provider": "openrouter", "selected_provider": "openrouter",
        "selected_provider_name": "OpenRouter",
        "model_list": ["model-x", "model-y"], "on_model_selected": selected,
        "stage": "model", "page": 0, "message_id": "55",
        "created_at": time.monotonic(), "consuming": False,
    }
    monkeypatch.setattr(
        "hermes_cli.model_selection_guards.combined_selection_warning",
        lambda *_args, **kwargs: None,
    )
    event = SimpleNamespace(message_id=55, answer=AsyncMock())

    await adapter._handle_model_picker_callback(event, "mp:7:select:0")

    assert selected.await_count == 1
    assert adapter._mtproto_rich_request.await_count == 2
    requests = [call.args[0] for call in adapter._mtproto_rich_request.await_args_list]
    assert "Applying model" in _html(requests[0])
    final = requests[1]
    assert "Model selection complete" not in _html(final)
    assert "- **Model:** **`model-x`**" in _html(final)
    assert final.reply_markup is not None and final.reply_markup.rows
    buttons = [button for row in final.reply_markup.rows for button in row.buttons]
    picked = next(button for button in buttons if button.text == "model-x")
    assert picked.style.bg_success is True
    assert any(button.text == "‹ Providers" for button in buttons)
    assert 7 in adapter._model_picker_state
    assert adapter._model_picker_state[7]["current_model"] == "model-x"
    assert adapter._model_picker_state[7]["current_provider"] == "openrouter"
    assert adapter._model_picker_state[7]["consuming"] is False


@pytest.mark.asyncio
async def test_approval_outcome_text_has_no_emoji(tmp_path, monkeypatch):
    """Slash-confirm outcome cards (rendered by the /model guard path) must
    contain no emoji."""
    adapter = _adapter(tmp_path)
    source = _source()
    adapter._source = AsyncMock(return_value=source)
    adapter._authorized = lambda _source: True
    adapter._settings_home = lambda _source: str(tmp_path)
    adapter._approval_state[5] = {
        "owner_user_id": "100", "intended_user_id": "100", "owner_session": False,
        "profile_home": str(Path(tmp_path).resolve()), "allowed_choices": {"once"},
        "chat_id": "100", "thread_id": None, "message_id": "55",
        "session_key": "session-a", "request_id": "request-a",
    }
    event = SimpleNamespace(message_id=55, answer=AsyncMock())
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *_args, **kwargs: 1)

    await adapter._handle_exec_approval_callback(event, "ea:once:5")

    requests = [call.args[0] for call in adapter._mtproto_rich_request.await_args_list]
    assert requests
    assert not _has_emoji(_html(requests[-1]))
