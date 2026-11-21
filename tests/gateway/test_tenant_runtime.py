from types import SimpleNamespace

import pytest

from gateway.tenant_runtime import (
    TenantRuntime, TenantRuntimeDenied, room_key, session_key,
    safe_tool_allowed, TENANT_SAFE_TOOLS, tenant_tool_environment,
)
from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope


class Store:
    def __init__(self):
        self.allowed = {"11": True, "22": True}
        self.owner_user_id = "99"
        self.data = {}
    def authorization_generation(self, uid):
        return 0
    def group_authorization_generation(self, chat_id):
        return 0
    def principal_tenant(self, principal):
        return "tg-" + str(principal.user_id)
    def get_access(self, uid):
        return {"allowed": self.allowed.get(str(uid), False), "banned": False}
    def get(self, principal, kind, key):
        return self.data.get((str(principal.user_id), kind, key))
    def put(self, principal, kind, key, payload):
        self.data[(str(principal.user_id), kind, key)] = payload


class Broker:
    def __init__(self):
        self.calls = 0
    def for_tenant(self, *, principal, root):
        self.calls += 1
        # Credential-like provider material is server-only and never copied to cache.
        return {"model": "owner-approved-model", "api_key": "broker-secret", "base_url": "https://provider.invalid"}


def event(uid, cid, text="hello"):
    return SimpleNamespace(text=text, source=SimpleNamespace(user_id=uid, chat_id=cid), user_id="forged")


@pytest.mark.asyncio
async def test_two_users_and_rooms_have_isolated_sessions_and_runtime_paths(tmp_path):
    store, broker, seen = Store(), Broker(), []
    def factory(**kw):
        seen.append((kw["user_id"], kw["chat_id"], kw["session_id"], kw["profile_home"], kw["provider"], kw["tool_allowlist"]))
        return object()
    async def run_agent(**kw):
        assert kw["provider"]["api_key"] == "broker-secret"  # passed only in-memory
        assert kw["tool_environment"] == {}
        return {"final_response": "ok", "messages": [], "usage": {}}
    runtime = TenantRuntime(tmp_path, store, broker, factory, run_agent)
    for uid, cid in (("11", "group"), ("22", "group"), ("11", "dm")):
        assert (await runtime.handle_message(None, event(uid, cid), event(uid, cid).source))["final_response"] == "ok"
    assert len({x[2] for x in seen}) == 3
    assert len({str(x[3]) for x in seen}) == 3 # each user+room has a distinct cache
    assert all("broker-secret" not in str(x[3]) for x in seen)
    assert all(x[5] == TENANT_SAFE_TOOLS == frozenset() for x in seen)
    assert session_key("11", "group") != session_key("22", "group")
    assert session_key("11", "group") != session_key("11", "dm")


@pytest.mark.asyncio
async def test_denied_and_revoked_access_fail_closed_before_provider(tmp_path):
    store, broker = Store(), Broker()
    async def run(**kw): return "should not run"
    runtime = TenantRuntime(tmp_path, store, broker, lambda **kw: object(), run)
    store.allowed["22"] = False
    with pytest.raises(TenantRuntimeDenied):
        await runtime.handle_message(None, event("22", "g"), event("22", "g").source)
    assert broker.calls == 0
    # Revoke between initial access validation and provider dispatch.
    class RevokingBroker:
        def for_tenant(self, *, principal, root):
            store.allowed["11"] = False
            return {"model": "owner-approved"}
    runtime.provider_broker = RevokingBroker()
    with pytest.raises(TenantRuntimeDenied):
        await runtime.handle_message(None, event("11", "g"), event("11", "g").source)


def test_tool_firewall_and_environment_are_deny_by_default():
    assert not safe_tool_allowed("terminal")
    assert not safe_tool_allowed("mcp__anything")
    assert not safe_tool_allowed("memory_search")
    assert tenant_tool_environment({"PATH": "/bin", "API_KEY": "sensitive", "HOME": "/owner"}) == {"PATH": "/bin"}
    assert room_key("a", "bc") != room_key("ab", "c")


def test_private_skill_expansion_is_per_user_and_stable_across_aba(tmp_path):
    from gateway.telegram_skills import SkillCandidate, install_skill
    from gateway.tenant_runtime import expand_personal_skill_command
    from agent.skill_commands import personal_skill_command_alias

    (tmp_path / "SOUL.md").write_text("OWNER-ONLY-CANARY")
    for uid, body in (("11", "SKILL-11-ONLY"), ("22", "SKILL-22-ONLY")):
        content = f"---\nname: leanrea\ndescription: private test skill\n---\n\n{body}\n"
        install_skill(tmp_path, uid, SkillCandidate("leanrea", "private test skill", {"SKILL.md": content.encode()}))
        private_help = f"---\nname: help\ndescription: private help\n---\n\nHELP-{uid}-ONLY\n"
        install_skill(tmp_path, uid, SkillCandidate("help", "private help", {"SKILL.md": private_help.encode()}))
        zebra = f"---\nname: zebra\ndescription: private zebra\n---\n\nZEBRA-{uid}-ONLY\n"
        install_skill(tmp_path, uid, SkillCandidate("zebra", "private zebra", {"SKILL.md": zebra.encode()}))
        if uid == "11":
            alias_name = personal_skill_command_alias("zebra")
            collision = f"---\nname: {alias_name}\ndescription: alias-name collision\n---\n\nALIAS-NAME-COLLISION\n"
            install_skill(tmp_path, uid, SkillCandidate(alias_name, "alias-name collision", {"SKILL.md": collision.encode()}))

    a1 = expand_personal_skill_command("/leanrea@HermesBot summarize this", tmp_path, "11")
    b = expand_personal_skill_command("/leanrea summarize this", tmp_path, "22")
    a2 = expand_personal_skill_command("/leanrea summarize this", tmp_path, "11")
    assert a1 == a2
    assert "SKILL-11-ONLY" in a1 and "summarize this" in a1
    assert "SKILL-22-ONLY" not in a1 and "OWNER-ONLY-CANARY" not in a1
    assert "SKILL-22-ONLY" in b and "SKILL-11-ONLY" not in b
    assert expand_personal_skill_command("/missing do something", tmp_path, "11") == "/missing do something"
    assert expand_personal_skill_command("/help", tmp_path, "11") == "/help"
    alias = "/" + personal_skill_command_alias("help")
    private_a = expand_personal_skill_command(alias + " explain", tmp_path, "11")
    private_b = expand_personal_skill_command(alias + " explain", tmp_path, "22")
    assert "HELP-11-ONLY" in private_a and "HELP-22-ONLY" not in private_a
    assert "HELP-22-ONLY" in private_b and "HELP-11-ONLY" not in private_b
    zebra_alias = "/" + personal_skill_command_alias("zebra")
    zebra_result = expand_personal_skill_command(zebra_alias, tmp_path, "11")
    assert "ZEBRA-11-ONLY" in zebra_result and "ALIAS-NAME-COLLISION" not in zebra_result


@pytest.mark.asyncio
async def test_runtime_expands_private_skill_but_persists_raw_user_turn(tmp_path):
    from gateway.telegram_skills import SkillCandidate, install_skill
    from agent.mongo_tenants import Principal

    content = b"---\nname: leanrea\ndescription: private test skill\n---\n\nONLY-USER-11\n"
    install_skill(tmp_path, "11", SkillCandidate("leanrea", "private test skill", {"SKILL.md": content}))
    store, broker, seen = Store(), Broker(), []

    async def run_agent(**kw):
        seen.append(kw["text"])
        return {"final_response": "ok", "messages": [], "usage": {}}

    runtime = TenantRuntime(tmp_path, store, broker, lambda **kw: object(), run_agent)
    await runtime.handle_message(None, event("11", "11", "/leanrea summarize"), event("11", "11").source)
    assert len(seen) == 1 and "ONLY-USER-11" in seen[0] and "summarize" in seen[0]
    key = room_key("11", "11", profile=tmp_path)
    assert store.get(Principal("11"), "profile", "history:" + key)[0]["content"] == "/leanrea summarize"


def test_central_dispatch_denies_tools_in_tenant_scope(tmp_path):
    from run_agent import AIAgent
    call = SimpleNamespace(id="call-1", function=SimpleNamespace(name="terminal"))
    assistant = SimpleNamespace(tool_calls=[call])
    messages = []
    scope = TenantScope(tmp_path, object(), "room-x", tmp_path / "tenant", "tg-11")
    token = set_tenant_scope(scope)
    try:
        AIAgent._execute_tool_calls(object(), assistant, messages, "task")
    finally:
        reset_tenant_scope(token)
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call-1"
    assert "not available" in messages[0]["content"]
