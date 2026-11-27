"""Expired vs mismatched client-operations callbacks answer differently.

A callback whose token is gone (consumed, or reaped past CONFIRM_TTL) is
expired; a callback whose token is still live but bound to another
message/actor/chat/thread/profile mismatches — and the live token survives
for a correct retry.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.client_operations_ui import (
    CONFIRM_TTL,
    TelegramClientOperationsUI,
)

# Downloads under this size never show a chat card; at or over it the
# preparing / progress / result card flow stays exactly as before.
_QUIET_MAX_BYTES = 20 * 1024 * 1024


@pytest.fixture
def ui(tmp_path):
    adapter = SimpleNamespace(
        _client=object(),
        _authorized=lambda _source: True,
        _is_owner_user=lambda _user, source=None, **_kw: True,
        _settings_home=lambda _source: tmp_path / "alpha",
        _input_peer_for=AsyncMock(return_value="resolved-peer"),
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(updates=[])),
        _sent_message_id=lambda *args, **kwargs: 555,
        _message_thread_id=lambda _message: None,
    )
    return TelegramClientOperationsUI(adapter)


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                         user_id="100", thread_id="77")


class _Event:
    def __init__(self, message_id="500"):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", alert))


def _seed(ui, token, *, message_id="500", age=0.0):
    state = ui._new_state(_source(), token, "confirm", action="send_message", params={})
    state["message_id"] = message_id
    if age:
        state["created_at"] = time.monotonic() - age
    ui._states[token] = state
    return state


@pytest.mark.asyncio
async def test_aged_confirmation_callback_is_expired_and_reaped(ui):
    _seed(ui, "tok-exp", age=CONFIRM_TTL + 20)
    event = _Event(message_id="500")

    assert await ui.handle_callback(event, _source(), "tgo:c:tok-exp:yes") is True
    assert event.answers == [("This card expired.", True)]
    assert "tok-exp" not in ui._states


@pytest.mark.asyncio
async def test_live_confirmation_callback_from_another_message_is_mismatched_and_survives(ui):
    _seed(ui, "tok-live")
    event = _Event(message_id="999")

    assert await ui.handle_callback(event, _source(), "tgo:c:tok-live:yes") is True
    assert event.answers == [("This card does not match this message.", False)]
    assert "tok-live" in ui._states

    # The token survives a mismatched tap: the bound message still works.
    event = _Event(message_id="500")
    assert await ui.handle_callback(event, _source(), "tgo:c:tok-live:yes") is True
    assert event.answers[0][0] == "Rechecking authorization and applying the confirmed payload."


def _metadata_result(file_size):
    return {"ok": True, "status": "complete", "action": "files_metadata",
            "data": {"id": 280618, "file_size": file_size}}


def _download_result(file_size, *, ok=True, code=""):
    if ok:
        return {"ok": True, "status": "complete", "action": "files_download",
                "data": {"id": 280618, "file_size": file_size, "downloaded_bytes": file_size,
                         "elapsed_seconds": 0.3, "average_bytes_per_second": 58000.0}}
    return {"ok": False, "status": "error",
            "error": {"code": code, "message": "Download did not complete."}}


def _stub_download_flow(ui, metadata_result, download_result):
    """Route operations.request to scripted metadata/download results; spy on cards."""
    calls = {"metadata": [], "download": []}

    async def fake_request(source, action, params=None, **kwargs):
        if str(action).lower() == "files_metadata":
            calls["metadata"].append(params)
            return metadata_result
        calls["download"].append((params, kwargs))
        return download_result

    ui.operations.request = fake_request
    ui._send_card = AsyncMock(return_value="777")
    ui._edit_card = AsyncMock()
    return calls


@pytest.mark.asyncio
async def test_small_download_sends_no_card_but_still_returns_result(ui):
    calls = _stub_download_flow(ui, _metadata_result(17430), _download_result(17430))

    result = await ui._download_request(_source(), {"message_id": "280618"})

    assert result["ok"] is True
    assert result["data"]["downloaded_bytes"] == 17430
    assert calls["metadata"] == [{"message_id": "280618"}]
    assert len(calls["download"]) == 1
    assert calls["download"][0][1].get("progress_callback") is None
    assert calls["download"][0][1].get("cancel_event") is None
    ui._send_card.assert_not_called()
    ui._edit_card.assert_not_called()
    assert ui._states == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("file_size", [_QUIET_MAX_BYTES, 25 * 1024 * 1024])
async def test_large_download_keeps_preparing_and_result_cards(ui, file_size):
    calls = _stub_download_flow(ui, _metadata_result(file_size), _download_result(file_size))

    result = await ui._download_request(_source(), {"message_id": "280618"})

    assert result["ok"] is True
    ui._send_card.assert_awaited_once()
    assert "Preparing explicit Telegram media download." in ui._send_card.call_args.args[1]
    ui._edit_card.assert_awaited_once()
    assert "Telegram media download complete" in ui._edit_card.call_args.args[1]
    assert calls["download"][0][1].get("progress_callback") is not None
    assert calls["download"][0][1].get("cancel_event") is not None
    assert ui._states == {}


@pytest.mark.asyncio
async def test_cancelled_large_download_still_edits_card_to_cancelled(ui):
    _stub_download_flow(ui, _metadata_result(25 * 1024 * 1024),
                        _download_result(25 * 1024 * 1024, ok=False, code="DOWNLOAD_CANCELLED"))

    result = await ui._download_request(_source(), {"message_id": "280618"})

    assert result["ok"] is False
    ui._send_card.assert_awaited_once()
    ui._edit_card.assert_awaited_once()
    assert "Telegram media download cancelled" in ui._edit_card.call_args.args[1]


@pytest.mark.asyncio
async def test_failed_large_download_still_edits_card_to_failed(ui):
    _stub_download_flow(ui, _metadata_result(25 * 1024 * 1024),
                        _download_result(25 * 1024 * 1024, ok=False, code="DOWNLOAD_INCOMPLETE"))

    result = await ui._download_request(_source(), {"message_id": "280618"})

    assert result["ok"] is False
    ui._send_card.assert_awaited_once()
    ui._edit_card.assert_awaited_once()
    assert "Telegram media download failed" in ui._edit_card.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_result", [
    {"ok": False, "status": "error", "error": {"code": "MESSAGE_ID_INVALID", "message": "No such message."}},
    {"ok": True, "status": "complete", "action": "files_metadata", "data": {"id": 280618, "file_size": None}},
])
async def test_unresolvable_size_falls_back_to_card_flow(ui, metadata_result):
    calls = _stub_download_flow(ui, metadata_result, _download_result(25 * 1024 * 1024))

    result = await ui._download_request(_source(), {"message_id": "280618"})

    assert result["ok"] is True
    assert len(calls["download"]) == 1
    ui._send_card.assert_awaited_once()
    ui._edit_card.assert_awaited_once()
    assert "Telegram media download complete" in ui._edit_card.call_args.args[1]
