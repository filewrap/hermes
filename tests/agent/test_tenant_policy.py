"""Real local policy boundaries; no Telegram/provider calls or host secrets required."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from agent.mongo_tenants import Principal
from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope
from agent import tenant_policy as policy
from gateway.mongo_multiuser import store_for_home


# The namespace tests below execute real commands inside a bubblewrap
# sandbox; without the bwrap binary there is no boundary to test, so they
# skip instead of failing on hosts that cannot provide one.
requires_bubblewrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap (bwrap) is not installed")


@pytest.fixture
def tenant(tmp_path):
    (tmp_path / '.env').write_text('OWNER_TELEGRAM_ID=1\nTELEGRAM_ALLOWED_USERS=2\n')
    store = store_for_home(tmp_path)
    home = tmp_path / 'cache'; home.mkdir()
    scope = TenantScope(tmp_path, store.principal_for_scope('2','dm','2'), 'room', home, 'user-2',
                        user_id='2', chat_id='2', policy_allowed=True,
                        authorization_generation=store.authorization_generation('2'))
    return scope, store


def call(scope, name, args):
    return json.loads(policy.dispatch(scope, name, args))


@pytest.mark.platforms("linux")
@requires_bubblewrap
def test_actual_local_namespace_hides_host_and_secrets_and_network(tenant, monkeypatch):
    scope, _store = tenant
    assert policy.sandbox_available(scope), 'This Linux fixture requires working Bubblewrap'
    canary = scope.root / 'host-canary'; canary.write_text('owner-data')
    monkeypatch.setenv('PRIVATE_TENANT_TEST_SECRET', 'never-pass-this')
    command = "test ! -e " + str(canary) + " && test -z \"$PRIVATE_TENANT_TEST_SECRET\" && test ! -e /home/ubuntu/.hermes && printf namespace-ok"
    result = call(scope,'terminal',{'command':command})
    assert result['returncode'] == 0 and result['output'] == 'namespace-ok'
    result = call(scope,'terminal',{'command':"python3 -c \"import socket; s=socket.socket(); s.settimeout(.1); s.connect(('1.1.1.1',443))\""})
    assert result['returncode'] != 0
    assert 'Network is unreachable' in result['output']


@pytest.mark.platforms("linux")
@requires_bubblewrap
def test_file_operations_canonical_paths_and_symlinks(tenant):
    scope, _ = tenant
    assert call(scope,'write_file',{'path':'/workspace/note.txt','content':'private'})['success']
    assert call(scope,'read_file',{'path':'/workspace/note.txt'})['content'] == 'private'
    root=policy._workspace(scope)
    (root/'outside').symlink_to(scope.root/'.env')
    assert 'error' in call(scope,'read_file',{'path':'/workspace/outside'})
    policy.save_policy(scope.root,'2',{'denied_paths':['/workspace/note.txt']})
    (root/'alias').symlink_to('note.txt')
    (root/'hardlink').hardlink_to(root/'note.txt')
    assert 'error' in call(scope,'read_file',{'path':'/workspace/alias'})
    assert 'error' in call(scope,'read_file',{'path':'/workspace/hardlink'})
    assert 'error' in call(scope,'write_file',{'path':'/workspace/x/../note.txt','content':'stolen'})
    assert 'error' in call(scope,'terminal',{'command':'zip /workspace/leak.zip /workspace/note.txt'})
    assert 'terminal' not in policy.supported_tool_names(scope)
    assert (root/'note.txt').read_text() == 'private'


@pytest.mark.platforms("linux")
def test_tools_remain_usable_and_side_effect_denials_are_enforced(tenant):
    scope,_=tenant
    policy.save_policy(scope.root,'allowed',{'permitted_effects':['read']})
    assert policy.supported_tool_names(scope)=={'read_file'}
    for name,args in [('write_file',{'path':'/workspace/a','content':'no'}),
                      ('terminal',{'command':'echo no'}),('send_message',{'file':'/workspace/a'}),
                      ('execute_code',{'code':'open("/etc/passwd").read()'}),('delegate_task',{}),
                      ('cronjob',{}),('image_generate',{}),('upload_file',{}),('publish',{})]:
        assert 'error' in call(scope,name,args)
    assert not (policy._workspace(scope)/'a').exists()
    assert 'read_file' in policy.policy_guidance(scope)


def test_guest_allowall_revoked_and_banned_cannot_gain_tools(tenant):
    scope,store=tenant
    guest=replace(scope,policy_allowed=False)
    assert policy.supported_tool_names(guest)==set()
    assert 'error' in call(guest,'terminal',{'command':'true'})
    bound=replace(scope,authorization_generation=store.authorization_generation('2'))
    store.allow_user(Principal('1'),'2',False)
    assert 'error' in call(scope,'terminal',{'command':'true'})
    store.allow_user(Principal('1'),'2',True)
    assert 'error' in call(bound,'terminal',{'command':'true'})
    store.ban_user(Principal('1'),'2',True)
    assert 'error' in call(scope,'read_file',{'path':'/workspace/note'})


@pytest.mark.platforms("linux")
@requires_bubblewrap
def test_direct_registry_model_cached_and_lost_context_callbacks(tenant):
    import model_tools
    from tools.registry import registry
    from agent.tool_executor import _run_agent_tool_execution_middleware
    scope,_=tenant
    # Warm the owner cache, then enter a restricted user context.
    owner=model_tools.get_tool_definitions(enabled_toolsets=['file','terminal'],quiet_mode=True)
    assert owner
    token=set_tenant_scope(scope)
    try:
        definitions=model_tools.get_tool_definitions(enabled_toolsets=['file','terminal'],quiet_mode=True)
        assert {d['function']['name'] for d in definitions} <= policy.SUPPORTED
        assert json.loads(registry.dispatch('write_file',{'path':'/workspace/ok','content':'yes'}))['success']
        policy.save_policy(scope.root,'2',{'denied_tools':['write_file']})
        assert 'error' in json.loads(model_tools.handle_function_call('write_file',{'path':'/workspace/no','content':'no'}))
        assert 'write_file' not in {d['function']['name'] for d in model_tools.get_tool_definitions(enabled_toolsets=['file'],quiet_mode=True)}
    finally:
        reset_tenant_scope(token)
    def forbidden(args):
        pytest.fail('host executor must never be reached')
    from agent.agent_runtime_helpers import invoke_tool
    assert 'error' in json.loads(invoke_tool(SimpleNamespace(_tenant_scope=scope),'memory',{},'test',
        pre_tool_block_checked=True,skip_tool_request_middleware=True,skip_tool_execution_middleware=True))
    result=_run_agent_tool_execution_middleware(SimpleNamespace(_tenant_scope=scope),function_name='write_file',
        function_args={'path':'/workspace/no','content':'no'},effective_task_id='test',tool_call_id='call',execute=forbidden)
    assert result.blocked and 'error' in json.loads(result.result)
    assert not (policy._workspace(scope)/'no').exists()
    assert model_tools.get_tool_definitions(enabled_toolsets=['file','terminal'],quiet_mode=True)==owner


def test_policy_store_is_strict_and_profile_isolated(tmp_path):
    a=tmp_path/'a'; a.mkdir(); b=tmp_path/'b'; b.mkdir()
    policy.save_policy(a,'allowed',{'denied_tools':['terminal']})
    policy.save_policy(b,'2',{'denied_tools':['write_file']})
    assert policy.load_policy(a,'2')['denied_tools']==['terminal']
    assert policy.load_policy(b,'2')['denied_tools']==['write_file']
    assert policy.load_policy(a,'2')['denied_tools']==['terminal']
    for value in [{'unknown':[]},{'permitted_effects':['upload']},{'allowed_tools':['execute_code']},
                  {'denied_paths':['/workspace/../etc']},{'allowed_paths':['/workspace-other']},{'denied_tools':'terminal'}]:
        with pytest.raises(ValueError): policy.save_policy(a,'2',value)
    with pytest.raises(ValueError): policy.save_policy(a,'../owner',{})
    path=b/'tenant-tool-policies.json'; path.unlink(); path.symlink_to(a/'tenant-tool-policies.json')
    with pytest.raises(ValueError): policy.load_policy(b,'2')


@pytest.mark.platforms("linux")
def test_no_host_fallback_when_namespace_unavailable_and_no_deferred_options(tenant, monkeypatch):
    scope,_=tenant
    assert 'error' in call(scope,'terminal',{'command':'touch /workspace/no','background':True})
    monkeypatch.setattr(policy,'sandbox_available',lambda scope:False)
    assert 'error' in call(scope,'terminal',{'command':'touch /workspace/no'})
    assert not (policy._workspace(scope)/'no').exists()


def test_denials_notify_operations_without_banning_or_leaking_arguments(tenant, monkeypatch):
    from plugins.platforms.telegram import forum_audit
    scope,store=tenant
    notifications=[]
    monkeypatch.setattr(forum_audit,'enqueue_operation',lambda root,text:notifications.append((root,text)))
    assert 'error' in call(scope,'upload_file',{'file':'OWNER-SECRET-CANARY'})
    assert len(notifications)==1 and notifications[0][0]==scope.root
    assert 'user=2' in notifications[0][1] and 'tool=upload_file' in notifications[0][1]
    assert 'OWNER-SECRET-CANARY' not in notifications[0][1]
    assert store.access_for_scope('2','dm','2')['allowed']
    assert not store.access_for_scope('2','dm','2')['banned']


@pytest.mark.platforms("linux")
@requires_bubblewrap
def test_real_resource_configuration_and_foreground_pipeline(tenant):
    scope,_=tenant
    command="printf hello | cat; python3 -c \"import resource,os,json; print(json.dumps([resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrlimit(resource.RLIMIT_FSIZE)[0], os.statvfs('/tmp').f_blocks*os.statvfs('/tmp').f_frsize]))\""
    result=call(scope,'terminal',{'command':command})
    assert result['returncode']==0
    assert result['output'].startswith('hello')
    assert json.loads(result['output'][5:])==[268435456,8388608,16777216]
    result=call(scope,'terminal',{'command':"python3 -c \"print('x'*110000)\""})
    assert len(result['output'])==100000 and result['truncated']


@pytest.mark.platforms("linux")
@requires_bubblewrap
def test_directory_resource_restrictions_reject_hardlink_aliases(tenant):
    scope,_=tenant
    root=policy._workspace(scope)
    (root/'private').mkdir()
    (root/'public').mkdir()
    protected=root/'private'/'secret'
    protected.write_text('private-directory-canary')
    alias=root/'public'/'alias'
    alias.hardlink_to(protected)
    policy.save_policy(scope.root,'2',{'denied_paths':['/workspace/private']})
    assert 'error' in call(scope,'read_file',{'path':'/workspace/public/alias'})
    assert 'error' in call(scope,'write_file',{'path':'/workspace/public/alias','content':'modified'})
    assert protected.read_text()=='private-directory-canary'
    policy.save_policy(scope.root,'2',{'allowed_paths':['/workspace/public']})
    assert 'error' in call(scope,'read_file',{'path':'/workspace/public/alias'})
    assert call(scope,'write_file',{'path':'/workspace/public/ordinary','content':'allowed'})['success']
    assert call(scope,'read_file',{'path':'/workspace/public/ordinary'})['content']=='allowed'
    unicode_content='\U0001f680'*30000
    assert call(scope,'write_file',{'path':'/workspace/public/unicode','content':unicode_content})['success']
    assert call(scope,'read_file',{'path':'/workspace/public/unicode'})['content']==unicode_content
