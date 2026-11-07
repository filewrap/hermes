"""Safe reply-target selection and user-facing download details for /tgfiles."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.client_operations_ui import TelegramClientOperationsUI


@pytest.fixture
def file_ui():
    adapter = SimpleNamespace(
        _authorized=lambda _source: True,
        _settings_home=lambda source: f"/profiles/{source.profile}",
    )
    ui = TelegramClientOperationsUI(adapter)
    ui._show_result = AsyncMock()
    source = SimpleNamespace(user_id="100", chat_id="-10042", thread_id="77",
                             profile="alpha", message_id="900", is_bot=False, is_guest=False)
    return ui, source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "reply_header", "expected_action"),
    [
        ("", SimpleNamespace(reply_to_msg_id=41), "files_metadata"),
        ("metadata", SimpleNamespace(reply_to_msg_id=42), "files_metadata"),
        ("download", SimpleNamespace(reply_to_msg_id=43), "files_download"),
    ],
)
async def test_tgfiles_reply_targets_use_native_reply_id_not_command_delivery_id(
    file_ui, arguments, reply_header, expected_action
):
    ui, source = file_ui
    command = SimpleNamespace(id=900, reply_to=reply_header)
    ui._request = AsyncMock(return_value={"ok": True, "status": "complete", "data": {}})

    assert await ui.handle_command(command, source, "tgfiles", arguments, reply_to=command.id)

    ui._request.assert_awaited_once_with(
        source, expected_action, {"message_id": str(reply_header.reply_to_msg_id)},
        render=False, reply_to=900,
    )
    if expected_action == "files_metadata":
        ui._show_result.assert_awaited_once()
    else:
        ui._show_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_tgfiles_resolves_wrapped_raw_message_and_never_uses_delivery_reply_id(file_ui):
    ui, source = file_ui
    command = SimpleNamespace(id=900, raw_message=SimpleNamespace(
        id=900, reply_to_msg_id=61, reply_to=SimpleNamespace(reply_to_msg_id=62)))
    ui._request = AsyncMock(return_value={"ok": True, "status": "complete", "data": {}})

    await ui.handle_command(command, source, "tgfiles", "", reply_to=777)

    assert ui._request.await_args.args[2] == {"message_id": "61"}
    assert ui._request.await_args.kwargs["reply_to"] == 777


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,action", [("metadata 71", "files_metadata"), ("download 72", "files_download")])
async def test_explicit_tgfiles_ids_are_preserved_without_consulting_reply(file_ui, arguments, action):
    ui, source = file_ui
    command = SimpleNamespace(id=900, reply_to_msg_id=999)
    ui._request = AsyncMock(return_value={"ok": True, "status": "complete", "data": {}})

    await ui.handle_command(command, source, "tgfiles", arguments)

    assert ui._request.await_args.args[1:3] == (action, {"message_id": arguments.split()[-1]})


@pytest.mark.asyncio
async def test_missing_reply_does_not_fall_back_to_command_or_delivery_id(file_ui):
    ui, source = file_ui
    ui._request = AsyncMock()
    command = SimpleNamespace(id=900, reply_to_msg_id=None)

    assert await ui.handle_command(command, source, "tgfiles", "", reply_to=901)

    ui._request.assert_not_awaited()
    ui._show_result.assert_awaited_once()
    error = ui._show_result.await_args.args[1]["error"]
    assert error["code"] == "FILE_TARGET_MISSING"
    assert "Reply directly to a photo, video, audio, or file" in error["message"]
    assert "/tgfiles metadata <message ID>" in error["message"]


@pytest.mark.asyncio
async def test_metadata_errors_explain_non_media_and_wrong_scope_next_steps(file_ui):
    ui, source = file_ui
    ui.operations.request = AsyncMock(side_effect=[
        {"ok": False, "status": "error", "error": {"code": "VALUEERROR", "message": "That message has no accessible media"}},
        {"ok": False, "status": "error", "error": {"code": "VALUEERROR", "message": "The message does not belong to the current Telegram topic"}},
    ])
    first = await ui._request(source, "files_metadata", {"message_id": "73"}, render=False)
    second = await ui._request(source, "files_metadata", {"message_id": "74"}, render=False)
    assert "Reply to a photo, video, audio, or file message" in first["error"]["message"]
    assert "current Telegram chat/topic" in second["error"]["message"]


def test_invalid_or_missing_file_id_error_has_next_steps():
    result = TelegramClientOperationsUI._file_target_result({
        "ok": False, "status": "error", "error": {"code": "MESSAGE_ID_INVALID", "message": "invalid message id"}
    })
    assert "positive numeric ID from this chat/topic" in result["error"]["message"]
    assert "reply directly to the media" in result["error"]["message"]


def test_download_progress_and_completion_include_metrics_but_hide_cache_path():
    progress = TelegramClientOperationsUI._download_progress_text(42_012_051, 42_012_051, 8.0, 5_251_506.375)
    assert "Downloaded 42.01 MB\nof 42.01 MB (100.0%)." in progress
    assert "Elapsed: 8.0 s" in progress
    assert "Average throughput: 5.25 MB/s" in progress

    card = TelegramClientOperationsUI._render_download_result({
        "ok": True,
        "data": {"downloaded_bytes": 42_012_051, "cached_path": "/private/profile/cache/file.apk",
                 "elapsed_seconds": 8.0, "average_bytes_per_second": 5_251_506.375},
    })
    assert "Elapsed: 8.0 s" in card
    assert "Average throughput: 5.25 MB/s" in card
    assert "/private/profile/cache/file.apk" not in card
    assert "&quot;downloaded_bytes&quot;: 42012051" in card


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["metadata", "metadata 81", "download", "download 81"])
async def test_dm_reply_and_explicit_ids_use_real_scoped_resolver(tmp_path, arguments):
    from pathlib import Path

    dm = SimpleNamespace(user_id="7", chat_id="7", chat_type="dm", thread_id=None,
                         profile="alpha", message_id="900", is_bot=False, is_guest=False)
    document = SimpleNamespace(id=702, size=1, mime_type="application/octet-stream",
                               attributes=[SimpleNamespace(file_name="file.apk")])
    media = SimpleNamespace(id=81, chat_id="7", sender_id=7, date=None,
                            message="DM caption", media=SimpleNamespace(document=document), file=None)

    async def download(_message, *, file, progress_callback):
        Path(file).write_bytes(b"x")
        await progress_callback(1, 1)

    client = SimpleNamespace(get_messages=AsyncMock(return_value=[media]), download_media=download)
    adapter = SimpleNamespace(_client=client, _authorized=lambda _: True,
                              _settings_home=lambda src: tmp_path / src.profile,
                              _input_peer_for=AsyncMock(return_value="dm-peer"),
                              _message_thread_id=lambda _: None)
    ui = TelegramClientOperationsUI(adapter)
    ui._send_card = AsyncMock(return_value="901")
    ui._edit_card = AsyncMock()
    ui._show_result = AsyncMock()
    command = SimpleNamespace(id=900, reply_to=SimpleNamespace(reply_to_msg_id=81))
    await ui.handle_command(command, dm, "tgfiles", arguments, reply_to=900)
    client.get_messages.assert_awaited_once_with("dm-peer", ids=[81])
    if arguments.startswith("metadata"):
        result = ui._show_result.await_args.args[1]
        assert result["ok"] and result["data"]["text"] == "DM caption"
    else:
        assert len(list((tmp_path / "alpha").rglob("*file.apk"))) == 1
        assert "download complete" in ui._edit_card.await_args.args[1]
        assert str(tmp_path) not in ui._edit_card.await_args.args[1]
    assert all(call.args == ("7",) for call in adapter._input_peer_for.await_args_list)


@pytest.mark.asyncio
async def test_dm_foreign_account_wide_id_does_not_expose_other_chat_content(tmp_path):
    dm = SimpleNamespace(user_id="7", chat_id="7", chat_type="dm", thread_id=None,
                         profile="alpha", message_id="900", is_bot=False, is_guest=False)
    foreign = SimpleNamespace(id=81, chat_id="8", message="secret-other-chat", media=object())
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[foreign]))
    adapter = SimpleNamespace(_client=client, _authorized=lambda _: True,
                              _settings_home=lambda src: tmp_path / src.profile,
                              _input_peer_for=AsyncMock(return_value="dm-peer"),
                              _message_thread_id=lambda _: None)
    ui = TelegramClientOperationsUI(adapter)
    ui._show_result = AsyncMock()
    await ui.handle_command(SimpleNamespace(id=900), dm, "tgfiles", "metadata 81")
    result = ui._show_result.await_args.args[1]
    assert not result["ok"]
    assert "secret-other-chat" not in str(result)
    assert "reply" in result["error"]["message"].lower()
    client.get_messages.assert_awaited_once_with("dm-peer", ids=[81])
