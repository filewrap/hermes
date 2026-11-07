"""Bounded, non-blocking progress-card edits for Telegram downloads."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.telegram.client_operations_ui as client_ui
from plugins.platforms.telegram.client_operations_ui import TelegramClientOperationsUI


@pytest.fixture
def download_ui():
    adapter = SimpleNamespace(
        _authorized=lambda _source: True,
        _settings_home=lambda source: f"/profiles/{source.profile}",
    )
    ui = TelegramClientOperationsUI(adapter)
    ui._send_card = AsyncMock(return_value="message-900")
    source = SimpleNamespace(user_id="100", chat_id="-10042", thread_id="77",
                             profile="alpha", message_id="12", is_bot=False, is_guest=False)
    return ui, source


@pytest.mark.asyncio
async def test_blocked_progress_edit_does_not_block_transfer_or_overwrite_final(download_ui):
    ui, source = download_ui
    edit_started = asyncio.Event()
    edit_cancelled = asyncio.Event()
    final_edits = []

    async def blocked_edit(_state, text, _markup):
        if "Downloaded " in text:
            edit_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                edit_cancelled.set()
                raise
        final_edits.append(text)

    ui._edit_card = AsyncMock(side_effect=blocked_edit)
    callback_returned = asyncio.Event()

    async def fake_request(_source, _action, _params, *, progress_callback=None, cancel_event=None):
        await progress_callback(10, 100)
        callback_returned.set()
        await edit_started.wait()
        return {"ok": True, "status": "complete", "action": "files_download", "data": {"downloaded_bytes": 10}}

    ui.operations.request = AsyncMock(side_effect=fake_request)
    task = asyncio.create_task(ui.request(source, "files_download", {"message_id": "44"}))
    await asyncio.wait_for(callback_returned.wait(), timeout=1)
    result = await asyncio.wait_for(task, timeout=1)
    assert result["status"] == "complete"
    assert edit_cancelled.is_set()
    assert len(final_edits) == 1
    assert "download complete" in final_edits[0].lower()
    assert ui._edit_card.await_count == 2
    assert not ui._states


@pytest.mark.asyncio
async def test_progress_edits_are_single_worker_throttled_and_use_latest_snapshot(download_ui, monkeypatch):
    ui, source = download_ui
    clock = [100.0]
    monkeypatch.setattr(client_ui, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    first_edit_started = asyncio.Event()
    unblock_first_edit = asyncio.Event()
    second_edit_done = asyncio.Event()
    progress_edits = []

    async def edit(_state, text, _markup):
        if "Downloaded " not in text:
            return
        progress_edits.append((clock[0], text))
        if len(progress_edits) == 1:
            first_edit_started.set()
            await unblock_first_edit.wait()
        elif len(progress_edits) == 2:
            second_edit_done.set()

    ui._edit_card = AsyncMock(side_effect=edit)
    continue_transfer = asyncio.Event()

    async def fake_request(_source, _action, _params, *, progress_callback=None, cancel_event=None):
        await progress_callback(1, 1000)
        await first_edit_started.wait()
        for value in (200, 300, 400):
            await progress_callback(value, 1000)
        state = next(iter(ui._states.values()))
        assert state["progress_task"] is not None and not state["progress_task"].done()
        assert len(progress_edits) == 1
        clock[0] = 103.0
        unblock_first_edit.set()
        await second_edit_done.wait()
        await continue_transfer.wait()
        return {"ok": True, "status": "complete", "action": "files_download", "data": {}}

    ui.operations.request = AsyncMock(side_effect=fake_request)
    task = asyncio.create_task(ui.request(source, "files_download", {"message_id": "44"}))
    await asyncio.wait_for(second_edit_done.wait(), timeout=1)
    assert len(progress_edits) == 2
    assert [at for at, _ in progress_edits] == [100.0, 103.0]
    assert "Downloaded 0.000400 MB\nof 0.001000 MB (40.0%)." in progress_edits[-1][1]
    continue_transfer.set()
    await asyncio.wait_for(task, timeout=1)
    assert "download complete" in ui._edit_card.await_args.args[1].lower()
    assert ui._edit_card.await_count == 3


@pytest.mark.asyncio
async def test_external_cancellation_cancels_blocked_progress_before_terminal_edit(download_ui):
    ui, source = download_ui
    edit_started = asyncio.Event()
    progress_cancelled = asyncio.Event()
    final_edits = []

    async def blocked_edit(_state, text, _markup):
        if "Downloaded " in text:
            edit_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                progress_cancelled.set()
                raise
        final_edits.append(text)

    ui._edit_card = AsyncMock(side_effect=blocked_edit)
    callback_ready = asyncio.Event()

    async def fake_request(_source, _action, _params, *, progress_callback=None, cancel_event=None):
        await progress_callback(25, 100)
        callback_ready.set()
        await asyncio.Event().wait()

    ui.operations.request = AsyncMock(side_effect=fake_request)
    task = asyncio.create_task(ui.request(source, "files_download", {"message_id": "44"}))
    await asyncio.wait_for(callback_ready.wait(), timeout=1)
    await asyncio.wait_for(edit_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert progress_cancelled.is_set()
    assert len(final_edits) == 1
    assert "Download task cancelled" in final_edits[0]
    assert ui._edit_card.await_count == 2
    assert not ui._states


@pytest.mark.asyncio
async def test_scope_change_cancels_blocked_progress_worker_without_more_edits(download_ui):
    ui, source = download_ui
    edit_started = asyncio.Event()
    progress_cancelled = asyncio.Event()

    async def blocked_edit(_state, text, _markup):
        if "Downloaded " in text:
            edit_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                progress_cancelled.set()
                raise

    ui._edit_card = AsyncMock(side_effect=blocked_edit)
    scope_changed = asyncio.Event()

    async def fake_request(_source, _action, _params, *, progress_callback=None, cancel_event=None):
        await progress_callback(10, 100)
        await edit_started.wait()
        source.profile = "beta"
        await progress_callback(20, 100)
        scope_changed.set()
        return {"ok": True, "status": "complete", "action": "files_download", "data": {}}

    ui.operations.request = AsyncMock(side_effect=fake_request)
    result = await asyncio.wait_for(ui.request(source, "files_download", {"message_id": "44"}), timeout=1)
    await asyncio.wait_for(scope_changed.wait(), timeout=1)
    assert result["status"] == "complete"
    assert progress_cancelled.is_set()
    assert ui._edit_card.await_count == 1
    assert not ui._states


@pytest.mark.asyncio
async def test_coalesced_byte_snapshots_do_not_repeat_full_ui_scope_resolution(download_ui):
    ui, source = download_ui
    scope_calls = 0
    validate = ui._download_scope_valid

    def counted_scope(*args):
        nonlocal scope_calls
        scope_calls += 1
        return validate(*args)

    ui._download_scope_valid = counted_scope
    ui._edit_card = AsyncMock()

    async def fake_request(_source, _action, _params, *, progress_callback=None, cancel_event=None):
        # Real operations checks scope per part. Discarded UI snapshots must
        # not repeat those lookups before any external edit can occur.
        before = scope_calls
        for downloaded in range(4096):
            await progress_callback(downloaded, 4096)
        assert scope_calls == before
        return {"ok": True, "status": "complete", "action": "files_download",
                "data": {"downloaded_bytes": 4096}}

    ui.operations.request = AsyncMock(side_effect=fake_request)
    result = await ui.request(source, "files_download", {"message_id": "44"})
    assert result["ok"]
    assert scope_calls >= 2  # card creation and immediately before final edit
    assert not ui._states
