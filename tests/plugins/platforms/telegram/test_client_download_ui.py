"""Progress and cancellation UI for explicit Telegram client-media downloads."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.client_operations_ui import TelegramClientOperationsUI


@pytest.fixture
def download_ui():
    adapter = SimpleNamespace(
        _authorized=lambda _source: True,
        _settings_home=lambda source: f"/profiles/{source.profile}",
    )
    ui = TelegramClientOperationsUI(adapter)
    ui._send_card = AsyncMock(return_value="message-900")
    ui._edit_events = asyncio.Queue()

    async def record_edit(*args):
        await ui._edit_events.put(args)

    ui._edit_card = AsyncMock(side_effect=record_edit)
    source = SimpleNamespace(user_id="100", chat_id="-10042", thread_id="77",
                             profile="alpha", message_id="12", is_bot=False, is_guest=False)
    return ui, source


@pytest.mark.asyncio
async def test_download_agent_request_shows_progress_and_scope_bound_cancel(download_ui):
    ui, source = download_ui
    called = {}

    async def fake_request(actual_source, action, params, *, progress_callback=None, cancel_event=None):
        called.update(source=actual_source, action=action, params=params,
                      progress_callback=progress_callback, cancel_event=cancel_event)
        await progress_callback(5, 10)
        await cancel_event.wait()
        return {"ok": False, "status": "error", "error": {
            "code": "DOWNLOAD_CANCELLED", "message": "Stopped <script>alert(1)</script>"}}

    ui.operations.request = AsyncMock(side_effect=fake_request)
    task = asyncio.create_task(ui.request(source, "files_download", {"message_id": "44"}))
    while not called:
        await asyncio.sleep(0)

    ui._send_card.assert_awaited_once()
    initial = ui._send_card.await_args.args[1]
    assert "Telegram media download" in initial
    assert "2 GB" not in initial and "4 GB" not in initial and "not a guarantee" not in initial
    assert "<pre><code>" in initial
    await asyncio.wait_for(ui._edit_events.get(), timeout=1)
    ui._edit_card.assert_awaited_once()
    progress = ui._edit_card.await_args.args[1]
    assert "Downloaded 0.000005 MB\nof 0.000010 MB (50.0%)." in progress
    assert " MB/s" in progress and "bytes/s" not in progress
    token, state = next(iter(ui._states.items()))
    state["created_at"] -= 181  # active downloads do not expire at the ordinary callback TTL

    async def invoke(data, event_source=source, message_id="message-900"):
        event = SimpleNamespace(message_id=message_id, answer=AsyncMock())
        await ui.handle_callback(event, event_source, data)
        return event

    callback = f"tgo:d:{token}:cancel"
    await invoke(f"tgo:c:{token}:yes")
    assert token in ui._states and not called["cancel_event"].is_set()
    for changed in (
        SimpleNamespace(**{**vars(source), "user_id": "someone-else"}),
        SimpleNamespace(**{**vars(source), "chat_id": "-10099"}),
        SimpleNamespace(**{**vars(source), "thread_id": "78"}),
        SimpleNamespace(**{**vars(source), "profile": "beta"}),
    ):
        await invoke(callback, changed)
        assert not called["cancel_event"].is_set()
    await invoke(callback, message_id="wrong-message")
    assert not called["cancel_event"].is_set()

    event = await invoke(callback)
    assert called["cancel_event"].is_set()
    event.answer.assert_awaited_once()
    result = await task
    assert result["error"]["code"] == "DOWNLOAD_CANCELLED"
    assert token not in ui._states
    assert ui._send_card.await_count == 1
    await asyncio.wait_for(ui._edit_events.get(), timeout=1)
    final = ui._edit_card.await_args.args[1]
    assert "download cancelled" in final.lower()
    assert "<pre><code>" in final
    assert "&lt;script&gt;" in final
    assert "<script>" not in final
    assert ui._edit_card.await_args.args[2] is None


@pytest.mark.asyncio
async def test_tgfiles_download_updates_single_card_without_duplicate_result(download_ui):
    ui, source = download_ui
    ui.operations.request = AsyncMock(return_value={"ok": True, "status": "complete",
                                                   "action": "files_download", "data": {
                                                       "file_name": "example.bin", "file_size": 1_048_576,
                                                       "downloaded_bytes": 1_000_000, "path": "/tmp/file",
                                                       "cached_path": "/profiles/alpha/cache/file",
                                                       "download_diagnostics": {"secret": "internal"},
                                                       "download_limits": {"max_connections": 8}}})
    ui._show_result = AsyncMock()
    handled = await ui.handle_command(SimpleNamespace(), source, "tgfiles", "download 44")
    assert handled
    ui.operations.request.assert_awaited_once()
    assert ui.operations.request.await_args.args[1:] == ("files_download", {"message_id": "44"})
    ui._send_card.assert_awaited_once()
    ui._edit_card.assert_awaited_once()
    ui._show_result.assert_not_awaited()
    card = ui._edit_card.await_args.args[1]
    assert "Downloaded: 1.00 MB" in card
    assert "Media size: 1.05 MB" in card
    assert "&quot;downloaded_bytes&quot;: 1000000" in card
    assert "/tmp/file" not in card
    assert "cached_path" not in card
    assert "download_diagnostics" not in card
    assert "download_limits" not in card


def test_download_units_are_decimal_at_one_megabyte_boundary():
    assert TelegramClientOperationsUI._format_download_amount(999_999) == "0.999999 MB"
    assert TelegramClientOperationsUI._format_download_amount(1_000_000) == "1.00 MB"
    assert TelegramClientOperationsUI._format_download_amount(1_048_576) == "1.05 MB"
    assert TelegramClientOperationsUI._format_throughput(1_000_000) == "1.00 MB/s"


@pytest.mark.asyncio
async def test_download_card_delivery_failure_never_starts_core_download(download_ui):
    ui, source = download_ui
    ui._send_card = AsyncMock(return_value=None)
    ui.operations.request = AsyncMock()
    result = await ui.request(source, "files_download", {"message_id": "44"})
    assert result["error"]["code"] == "DOWNLOAD_CARD_DELIVERY_FAILED"
    assert "not started" in result["error"]["message"]
    ui.operations.request.assert_not_awaited()
    assert not ui._states


@pytest.mark.asyncio
async def test_unadmitted_actor_gets_no_download_card(download_ui):
    ui, source = download_ui
    ui.adapter._authorized = lambda _source: False
    ui.operations.request = AsyncMock(return_value={"ok": False, "status": "error", "error": {
        "code": "FORBIDDEN", "message": "not admitted"}})
    result = await ui.request(source, "files_download", {"message_id": "44"})
    assert result["error"]["code"] == "FORBIDDEN"
    ui._send_card.assert_not_awaited()
    ui.operations.request.assert_awaited_once_with(source, "files_download", {"message_id": "44"})


@pytest.mark.asyncio
async def test_external_task_cancellation_propagates_and_cleans_active_state(download_ui):
    ui, source = download_ui
    started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    ui.operations.request = AsyncMock(side_effect=never_finishes)
    task = asyncio.create_task(ui.request(source, "files_download", {"message_id": "44"}))
    await started.wait()
    assert len(ui._states) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ui._states
    await asyncio.wait_for(ui._edit_events.get(), timeout=1)
    assert ui._edit_card.await_count == 1
    final = ui._edit_card.await_args.args[1]
    assert "Download task cancelled" in final
    assert ui._edit_card.await_args.args[2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("profile", "beta"), ("chat_id", "-10099"), ("user_id", "101")])
async def test_scope_change_during_initial_card_prevents_download(download_ui, field, value):
    ui, source = download_ui
    ui.operations.request = AsyncMock()

    async def send_and_switch(*_args, **_kwargs):
        setattr(source, field, value)
        return "message-900"

    ui._send_card = AsyncMock(side_effect=send_and_switch)
    result = await ui.request(source, "files_download", {"message_id": "44"})
    assert result["error"]["code"] == "DOWNLOAD_SCOPE_CHANGED"
    ui.operations.request.assert_not_awaited()
    ui._edit_card.assert_not_awaited()
    assert not ui._states
