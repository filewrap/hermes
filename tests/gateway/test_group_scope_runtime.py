"""Scoped access reaches the real isolated runtime without becoming global access."""
from types import SimpleNamespace

import pytest

from agent.mongo_tenants import Principal, TenantStoreError
from gateway.tenant_runtime import handle_tenant_message, TenantRuntimeDenied
from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings


@pytest.fixture
def scoped_runtime(tmp_path, monkeypatch):
    from gateway.mongo_multiuser import store_for_home
    import run_agent
    (tmp_path / '.env').write_text('OWNER_TELEGRAM_ID=1\nTELEGRAM_ALLOWED_USERS=1\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = store_for_home(tmp_path)
    store.put(Principal('1'), 'memory', 'MEMORY.md', 'OWNER PRIVATE CANARY')
    broker = SimpleNamespace(for_tenant=lambda **kwargs: {'provider': 'openrouter', 'model': 'openrouter/free', 'api_key': 'provider-test-key'})
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            from agent.secret_scope import get_secret
            from hermes_constants import get_hermes_home
            self.home = get_hermes_home()
            # A transport-scoped sudo identity is opted into the bounded local
            # policy (gateway/tenant_runtime.py _scope/_make_agent): it gets
            # enabled_toolsets ["terminal", "file"] and the bounded
            # {terminal, read_file, write_file} definitions. Every other
            # scoped identity gets no toolsets and no tools.
            assert kwargs['enabled_toolsets'] in ([], ['terminal', 'file']), kwargs['enabled_toolsets']
            self._policy_tools = kwargs['enabled_toolsets'] == ['terminal', 'file']
            assert kwargs['model'] == 'openrouter/free'
            self.tools = [{'function': {'name': 'terminal'}}]
            self.valid_tool_names = {'terminal'}
            assert get_secret('TELEGRAM_BOT_TOKEN') is None

        def run_conversation(self, text, system_message=None, conversation_history=None):
            if self._policy_tools:
                # _make_agent replaced the constructor tools with the bounded
                # tenant definitions; nothing outside that set may leak in.
                assert {t['function']['name'] for t in self.tools} == {'terminal', 'read_file', 'write_file'}
                assert self.valid_tool_names == {'terminal', 'read_file', 'write_file'}
            else:
                assert self.tools == [] and self.valid_tool_names == set()
            assert 'OWNER PRIVATE CANARY' not in system_message
            calls.append((text, self.home, conversation_history))
            return {'final_response': 'scoped answer', 'api_calls': 1}

        def close(self):
            pass

    monkeypatch.setattr(run_agent, 'AIAgent', Agent)

    async def turn(uid='2', chat='-100', mid=1):
        source = SimpleNamespace(user_id=uid, chat_id=chat, chat_type='group' if chat.startswith('-') else 'dm')
        event = SimpleNamespace(message=SimpleNamespace(id=mid, message='hello'))
        return await handle_tenant_message(SimpleNamespace(_bot_username='NainaAssistantBot'), event, source, tmp_path,
                                           store=store, provider_broker=broker)

    return tmp_path, store, calls, turn


@pytest.mark.asyncio
async def test_allowall_replies_without_global_admission_or_admin(scoped_runtime):
    home, store, calls, turn = scoped_runtime
    update_chat_settings(home, 'group', '-100', {'allow_all': True})
    result = await turn()
    assert result['final_response'] == 'scoped answer'
    assert len(calls) == 1
    assert store.get_access('2')['allowed'] is False
    assert store.access_for_scope('2', 'group', '-100')['sudo'] is False
    assert store.access_for_scope('2', 'group', '-200')['allowed'] is False
    with pytest.raises(TenantRuntimeDenied):
        await turn(chat='2', mid=2)
    with pytest.raises(TenantRuntimeDenied):
        await turn(chat='-200', mid=3)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_allowall_off_and_bans_take_effect_at_runtime(scoped_runtime):
    home, store, calls, turn = scoped_runtime
    update_chat_settings(home, 'group', '-100', {'allow_all': True})
    store.ban_user(Principal('1'), '2', True)
    with pytest.raises(TenantRuntimeDenied):
        await turn()
    store.ban_user(Principal('1'), '2', False)
    await turn(mid=2)
    update_chat_settings(home, 'group', '-100', {'allow_all': False})
    with pytest.raises(TenantRuntimeDenied):
        await turn(mid=3)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_scoped_sudo_reaches_own_runtime_only(scoped_runtime):
    _home, store, calls, turn = scoped_runtime
    store.start_user('2')
    store.record_started_identity('2', 'seconduser', 'Second')
    store.grant_sudo(Principal('1'), '2', '-100')
    await turn()
    with pytest.raises(TenantRuntimeDenied):
        await turn(chat='2', mid=2)
    with pytest.raises(TenantRuntimeDenied):
        await turn(chat='-200', mid=3)
    assert len(calls) == 1
    assert '.tenant-cache/tg-2/' in str(calls[0][1])


@pytest.mark.asyncio
async def test_allowall_room_histories_and_receipts_remain_isolated(scoped_runtime):
    home, store, calls, turn = scoped_runtime
    update_chat_settings(home, 'group', '-100', {'allow_all': True})
    await turn('2')
    await turn('3')
    await turn('2')  # durable duplicate does not call the provider twice
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert calls[0][2] == calls[1][2] == []
    with pytest.raises(TenantStoreError):
        store.get(Principal('2'), 'profile', 'profile')  # no unscoped backdoor
