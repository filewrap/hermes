"""Exercise admitted Telethon diagnostics and owner-bound redacted log exports."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest
from telethon import types

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.mongo_multiuser import store_for_home
from agent.mongo_tenants import Principal
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.diagnostic_logs import recent_logs, export_logs


@pytest.fixture
def transport(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={'require_mention': True}))
    adapter._bot_username = 'HermesBot'
    adapter._bot_id = 900
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: tmp_path)
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._refresh_command_menu = AsyncMock()
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=800))
    adapter._input_peer_for = AsyncMock(side_effect=lambda chat: types.InputPeerUser(int(chat), 0))
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs' / 'gateway.log').write_text('ready\nAPI_KEY=sk-secret-secret-secret-secret123\nBearer abcdefghijklmnopqrstuvwxyz123\n')
    return adapter, tmp_path


def incoming(text, uid='100', kind='dm', chat=None):
    source = SimpleNamespace(user_id=uid, user_name='Owner', chat_id=chat or uid,
                             chat_type=kind, thread_id=None, is_bot=False)
    return SimpleNamespace(source=source, message=SimpleNamespace(message=text, id=44, reply_to_msg_id=None))


@pytest.mark.asyncio
async def test_logs_native_command_callback_exports_all_retained_logs_and_rechecks_identity(transport):
    adapter, home = transport
    logs = home / 'logs'
    (logs / 'agent.log.1').write_text('rotated\nTELEGRAM_BOT_TOKEN=123456:secret-secret-secret-secret-secret\n')
    (logs / 'arbitrary.txt').write_text('DO NOT EXPORT')
    (logs / 'errors.log').symlink_to(home / '.env')
    await adapter._on_new_message(incoming('/logs@HermesBot'))
    request = adapter._client.await_args.args[0]
    assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
    assert 'ready' in request.rich_message.markdown
    assert 'abcdefghijklmnopqrstuvwxyz123' not in request.rich_message.markdown
    assert 'sk-secret-secret-secret-secret123' not in request.rich_message.markdown
    button = request.reply_markup.rows[0].buttons[0]
    source = incoming('/logs').source
    callback = SimpleNamespace(data=button.type.data, source=source, message_id=800, answer=AsyncMock())
    delivered = []
    async def capture(chat, path, **kwargs):
        with zipfile.ZipFile(path) as archive:
            delivered.append({name: archive.read(name).decode() for name in archive.namelist()})
        return SendResult(success=True, message_id='900')
    adapter.send_document = AsyncMock(side_effect=capture)
    await adapter._on_callback_query(callback)
    assert len(delivered) == 1
    assert set(delivered[0]) == {'README.txt', 'gateway.log', 'agent.log.1'}
    assert 'secret-secret' not in ''.join(delivered[0].values())
    assert 'rotated' in delivered[0]['agent.log.1']
    for change in ({'message_id': 801}, {'source': incoming('/logs', uid='200').source},
                   {'source': incoming('/logs', chat='101').source}):
        denied = SimpleNamespace(**{**vars(callback), **change, 'answer': AsyncMock()})
        await adapter._on_callback_query(denied)
        assert denied.answer.await_count == 1
    assert len(delivered) == 1
    state = next(iter(adapter._diagnostics_ui()._states.values()))
    state['created'] -= 901
    await adapter._on_callback_query(callback)
    assert len(delivered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['logs', 'hf'])
async def test_diagnostics_owner_private_only_and_never_enters_model(transport, monkeypatch, command):
    adapter, home = transport
    adapter.handle_message = AsyncMock(side_effect=AssertionError('must not run model'))
    if command == 'hf':
        from plugins.platforms.telegram import hf_diagnostics
        monkeypatch.setattr(hf_diagnostics, 'collect_hf_diagnostics', lambda home, adapter: '# HF verified local checks')
    await adapter._on_new_message(incoming('/' + command))
    assert adapter._client.await_count == 1
    assert isinstance(adapter._client.await_args.args[0].rich_message, types.InputRichMessageMarkdown)
    store = store_for_home(home)
    store.start_user('200')
    store.grant_sudo(Principal('100'), '200', 'dm')
    adapter._client.reset_mock()
    await adapter._on_new_message(incoming('/' + command, uid='200'))
    assert 'not available in your isolated profile' in adapter._client.await_args.args[0].rich_message.markdown
    # The isolated-profile denial above is a separate request: clear it so
    # the group open below is counted as exactly ack + DM delivery.
    adapter._client.reset_mock()
    await adapter._diagnostics_ui().open(incoming('/' + command, kind='group', chat='999').source, command)
    # The owner in a group no longer hits the denial: the group gets only an
    # acknowledgement, and the diagnostics are delivered to the owner's DM.
    # Log content never enters the shared chat.
    requests = [call.args[0] for call in adapter._client.await_args_list]
    assert len(requests) == 2
    # The ack resolves the group peer; the DM delivery carries the owner's
    # user id as its peer (Telethon resolves the raw id at send time, as the
    # existing DM path does).
    assert getattr(requests[0].peer, "user_id", requests[0].peer) in {999, "999"}
    assert "Sending your diagnostics to your DM." in requests[0].rich_message.markdown
    assert getattr(requests[1].peer, "user_id", requests[1].peer) in {100, "100"}
    if command == "logs":
        assert "ready" in requests[1].rich_message.markdown
    else:
        assert "# HF verified local checks" in requests[1].rich_message.markdown
    # A non-owner in a group is still denied, with nothing delivered.
    adapter._client.reset_mock()
    await adapter._diagnostics_ui().open(
        incoming('/' + command, uid='200', kind='group', chat='999').source, command)
    assert adapter._client.await_count == 1
    assert 'only to the owner in a private chat' in adapter._client.await_args.args[0].rich_message.markdown
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['logs', 'hf'])
async def test_owner_in_group_gets_ack_and_dm_delivery_not_denial(transport, monkeypatch, command):
    """The owner running diagnostics in a group is no longer denied: the group
    gets only an acknowledgement, and the diagnostics are delivered to the
    owner's DM — log content never enters the shared chat. The download button
    on the DM card still exports, bound to the DM."""
    adapter, _home = transport
    if command == 'hf':
        from plugins.platforms.telegram import hf_diagnostics
        monkeypatch.setattr(hf_diagnostics, 'collect_hf_diagnostics', lambda home, adapter: '# HF verified local checks')
    await adapter._diagnostics_ui().open(incoming('/' + command, kind='group', chat='999').source, command)
    requests = [call.args[0] for call in adapter._client.await_args_list]
    assert len(requests) == 2
    # The ack resolves the group peer; the DM delivery carries the owner's
    # user id as its peer (Telethon resolves the raw id at send time, as the
    # existing DM path does).
    assert getattr(requests[0].peer, "user_id", requests[0].peer) in {999, "999"}
    assert "Sending your diagnostics to your DM." in requests[0].rich_message.markdown
    assert getattr(requests[1].peer, "user_id", requests[1].peer) in {100, "100"}
    if command == 'logs':
        assert 'ready' in requests[1].rich_message.markdown
    else:
        assert '# HF verified local checks' in requests[1].rich_message.markdown
    if command == 'logs':
        button = requests[1].reply_markup.rows[0].buttons[0]
        source = incoming('/logs').source
        callback = SimpleNamespace(data=button.type.data, source=source, message_id=800, answer=AsyncMock())
        delivered = []

        async def capture(chat, path, **kwargs):
            with zipfile.ZipFile(path) as archive:
                delivered.append({name: archive.read(name).decode() for name in archive.namelist()})
            return SendResult(success=True, message_id='900')

        adapter.send_document = AsyncMock(side_effect=capture)
        await adapter._on_callback_query(callback)
        assert len(delivered) == 1
        assert 'gateway.log' in delivered[0]


def test_log_export_rejects_symlinked_directory_and_size_overflow(transport, monkeypatch, tmp_path):
    _adapter, home = transport
    from plugins.platforms.telegram import diagnostic_logs
    monkeypatch.setattr(diagnostic_logs, 'MAX_FILE_BYTES', 8)
    with pytest.raises(ValueError, match='safe export size'):
        export_logs(home, home / 'too-big.zip')
    (home / 'logs').rename(home / 'real-logs')
    (home / 'logs').symlink_to(home / 'real-logs', target_is_directory=True)
    with pytest.raises(OSError):
        recent_logs(home)


def test_log_export_refuses_entry_floods_without_silently_omitting_logs(transport, monkeypatch):
    _adapter, home = transport
    from plugins.platforms.telegram import diagnostic_logs
    for number in range(3):
        (home / 'logs' / f'agent.log.{number}').touch()
    monkeypatch.setattr(diagnostic_logs, 'MAX_LOG_FILES', 2)
    with pytest.raises(ValueError, match='Too many retained log files'):
        export_logs(home, home / 'flood.zip')
    monkeypatch.setattr(diagnostic_logs, 'MAX_LOG_FILES', 256)
    monkeypatch.setattr(diagnostic_logs, 'MAX_DIRECTORY_ENTRIES', 2)
    with pytest.raises(ValueError, match='scan limits'):
        recent_logs(home)
