from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import mongomock
import pytest

from agent import mongo_credentials
from agent.mongo_tenants import MongoTenantStore, Principal
from agent.mongo_accounting import MongoAccountingStore
from gateway.tenant_runtime import handle_tenant_message, TenantRuntimeDenied, room_key


@pytest.fixture
def production(tmp_path, monkeypatch):
    from gateway import mongo_multiuser
    from agent import mongo_accounting
    from hermes_cli import runtime_provider
    import gateway.run
    import run_agent
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=1\nMULTIUSER_ENABLED=true\nMONGODB_URI=mongodb://unused/hermes\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OWNER_CANARY_SECRET", "never-expose-owner-secret")
    monkeypatch.setattr(mongo_credentials, "_settings", lambda home: mongo_credentials._Settings("mongodb://unused/hermes"))
    database = mongomock.MongoClient().hermes
    store = MongoTenantStore(tmp_path, "1", database=database)
    store.ensure_owner()
    store.allow_user(Principal("1"), "2", True)
    store.allow_user(Principal("1"), "3", True)
    accounting = MongoAccountingStore(tmp_path, database=database)
    monkeypatch.setattr(mongo_accounting, "account_store", lambda root: accounting)
    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda home: store)
    @contextmanager
    def owner_scope(root):
        yield
    monkeypatch.setattr(gateway.run, "_profile_runtime_scope", owner_scope)
    def resolve_owner_provider():
        from agent.tenant_scope import current_tenant_scope
        assert current_tenant_scope() is None, "Trusted provider resolution must not inherit the tenant's denied secret scope"
        return {"provider": "custom", "api_key": "private-provider-key", "base_url": "https://example.invalid/v1"}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve_owner_provider)
    monkeypatch.setattr(runtime_provider, "load_config", lambda: {"model": {"default": "owner-approved-model"}})
    calls = []
    class Agent:
        def __init__(self, **kwargs):
            from hermes_constants import get_hermes_home
            from agent.secret_scope import get_secret
            assert get_secret("OWNER_CANARY_SECRET") is None
            self.kwargs = kwargs
            self.home = Path(get_hermes_home())
            self.tools = [{"function": {"name": "terminal"}}]
            self.valid_tool_names = {"terminal"}
            self.session_input_tokens = 10
            self.session_output_tokens = 3
            self.session_api_calls = 1
            self.session_estimated_cost_usd = 0.01
        def run_conversation(self, text, system_message=None, conversation_history=None):
            from hermes_constants import get_hermes_home
            from agent.secret_scope import get_secret
            assert Path(get_hermes_home()) == self.home
            assert get_secret("OWNER_CANARY_SECRET") is None
            assert self.tools == [] and self.valid_tool_names == set()
            assert "private-provider-key" not in str(system_message)
            assert "never-expose-owner-secret" not in str(system_message)
            calls.append({"text": text, "prompt": system_message, "history": conversation_history, "home": self.home, "kwargs": self.kwargs})
            return {"final_response": "isolated answer", "api_calls": 1}
        def close(self):
            pass
    monkeypatch.setattr(run_agent, "AIAgent", Agent)
    return tmp_path, store, accounting, calls


async def turn(production, uid="2", chat=None, mid=1, text="hello"):
    home, store, _accounting, _calls = production
    chat = chat or uid
    source = SimpleNamespace(user_id=uid, chat_id=chat, chat_type="group" if chat.startswith("-") else "dm")
    event = SimpleNamespace(message=SimpleNamespace(id=mid, message=text))
    return await handle_tenant_message(None, event, source, home, store=store)


@pytest.mark.asyncio
async def test_real_factory_user_separation_secrets_and_usage(production):
    _home, store, accounting, calls = production
    store.put(Principal("2"), "memory", "MEMORY.md", "private memory two")
    store.put(Principal("1"), "memory", "MEMORY.md", "owner private memory")
    await turn(production, "2")
    await turn(production, "3")
    assert "private memory two" in calls[0]["prompt"]
    assert "private memory two" not in calls[1]["prompt"]
    assert all("owner private memory" not in c["prompt"] for c in calls)
    assert calls[0]["home"] != calls[1]["home"]
    assert calls[0]["kwargs"]["model"] == "owner-approved-model"
    assert accounting.totals(Principal("2"))["input_tokens"] == 10
    assert accounting.totals(Principal("3"))["input_tokens"] == 10
    persisted = repr(list(store._data.find()))
    assert "private-provider-key" not in persisted
    assert "never-expose-owner-secret" not in persisted


@pytest.mark.asyncio
async def test_groups_never_load_dm_memory_even_for_owner(production):
    _home, store, _accounting, calls = production
    for uid in ("1", "2", "3"):
        store.put(Principal(uid), "memory", "MEMORY.md", "PRIVATE-DM-" + uid)
        await turn(production, uid, "-100", mid=int(uid))
    assert len({str(c["home"]) for c in calls}) == 3
    assert all("PRIVATE-DM-" not in c["prompt"] for c in calls)
    assert all(c["history"] == [] for c in calls)


@pytest.mark.asyncio
async def test_safe_duplicate_and_durable_full_history(production):
    home, store, accounting, calls = production
    history = [{"role": "user", "content": str(i), "user_id": "2", "chat_id": "2",
                "thread_id": None, "profile": str(home.resolve())} for i in range(100)]
    history_key = room_key("2", "2", profile=home)
    store.put(Principal("2"), "profile", "history:" + history_key, history)
    await turn(production)
    await turn(production)
    assert len(calls) == 1
    assert len(calls[0]["history"]) == 40
    assert len(store.get(Principal("2"), "profile", "history:" + history_key)) == 102
    assert accounting.totals(Principal("2"))["api_call_count"] == 1
    with pytest.raises(TenantRuntimeDenied, match="Conflicting"):
        await turn(production, text="changed duplicate")


@pytest.mark.asyncio
async def test_accounting_failure_never_ack_or_recall_model(production, monkeypatch):
    home, store, accounting, calls = production
    def failure(*args, **kwargs):
        raise RuntimeError("opaque provider key must not leak")
    monkeypatch.setattr(accounting, "record_turn", failure)
    with pytest.raises(TenantRuntimeDenied, match="could not be completed"):
        await turn(production)
    with pytest.raises(TenantRuntimeDenied, match="uncertain"):
        await turn(production)
    assert len(calls) == 1
    room = room_key("2", "2", profile=home)
    assert store.get(Principal("2"), "profile", f"turn:telegram:{room}:1")["state"] == "calling"


@pytest.mark.asyncio
async def test_banned_revoked_and_disabled_group_never_call_provider(production):
    _home, store, _accounting, calls = production
    store.ban_user(Principal("1"), "2", True)
    with pytest.raises(TenantRuntimeDenied):
        await turn(production)
    store.ban_user(Principal("1"), "2", False)
    store.allow_user(Principal("1"), "2", False)
    with pytest.raises(TenantRuntimeDenied):
        await turn(production)
    store.allow_user(Principal("1"), "2", True)
    store.set_group_settings(Principal("1"), "-100", {"enabled": False})
    result = await turn(production, chat="-100")
    assert "disabled" in result["final_response"]
    assert calls == []


@pytest.mark.asyncio
async def test_cache_symlink_and_nontext_do_not_reach_model(production, tmp_path):
    _home, _store, _accounting, calls = production
    result = await turn(production, text=None)
    assert "text messages" in result["final_response"]
    assert calls == []


@pytest.mark.asyncio
async def test_cache_parent_symlink_never_writes_outside(production, tmp_path):
    home, _store, _accounting, calls = production
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".tenant-cache").symlink_to(outside, target_is_directory=True)
    with pytest.raises(TenantRuntimeDenied):
        await turn(production)
    assert not list(outside.iterdir())
    assert calls == []


@pytest.mark.asyncio
async def test_isolated_prompt_recognizes_transport_bot_identity(production):
    home, store, _accounting, calls = production
    source = SimpleNamespace(user_id="1", chat_id="-100", chat_type="group")
    event = SimpleNamespace(message=SimpleNamespace(id=81, message="@NainaAssistantBot hey"))
    await handle_tenant_message(SimpleNamespace(_bot_username="NainaAssistantBot"),
                                event, source, home, store=store)
    assert "You are the Telegram assistant @NainaAssistantBot, powered by Hermes." in calls[0]["prompt"]
    assert "Mentions of that handle address you" in calls[0]["prompt"]
    assert "PRIVATE-DM" not in calls[0]["prompt"]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_local_store_real_agent_uses_only_bounded_tools_and_sandboxed_model_round(tmp_path, monkeypatch):
    import json
    from agent import local_tenants, mongo_accounting, process_bootstrap
    from agent.local_tenants import LocalTenantStore
    from agent.mongo_tenants import Principal
    from agent.tenant_policy import _workspace
    from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope
    from gateway.tenant_runtime import TenantRuntime
    from run_agent import AIAgent

    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("OWNER_TELEGRAM_ID=99\nTELEGRAM_ALLOWED_USERS=11\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = LocalTenantStore(home, "99", allowed_user_ids=["11"])

    class ToolCall:
        def __init__(self, name, args, ident):
            self.id = ident
            self.type = "function"
            self.function = SimpleNamespace(name=name, arguments=json.dumps(args))

    class API:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            names = {tool["function"]["name"] for tool in kwargs.get("tools") or []}
            turn = len(self.requests)
            if turn <= 3:
                assert names == {"terminal", "read_file", "write_file"}
            else:
                assert turn == 4 and not names
                assert kwargs.get("tools") is None
            if turn == 1:
                message = SimpleNamespace(content=None, tool_calls=[ToolCall(
                    "write_file", {"path": "/workspace/turn.txt", "content": "written inside tenant"}, "write-1")])
            elif turn == 2:
                message = SimpleNamespace(content=None, tool_calls=[ToolCall(
                    "read_file", {"path": "/workspace/turn.txt"}, "read-1")])
            elif turn == 3:
                message = SimpleNamespace(content="File written and verified.", tool_calls=None)
            else:
                message = SimpleNamespace(content="Guest conversation only.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if turn < 3 else "stop")], usage=None)

    api = API()
    class ChatCompletions:
        create = api.create
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=ChatCompletions()), close=lambda: None)
    monkeypatch.setattr(process_bootstrap, "OpenAI", lambda **_kwargs: fake_client)
    monkeypatch.setattr(mongo_accounting, "account_store", lambda _root: SimpleNamespace(record_turn=lambda *_a, **_k: None))

    class Broker:
        def for_tenant(self, **_kwargs):
            return {"provider": "openai", "model": "tenant-test-model", "api_key": "test-key",
                    "base_url": "https://no-network.invalid/v1"}

    runtime = TenantRuntime(home, store, Broker(), None, None)
    source = SimpleNamespace(user_id="11", chat_id="11", chat_type="dm", thread_id=None)
    result = await runtime.handle_message(None, SimpleNamespace(text="write and read a file", message_id="local-agent"), source)
    assert result["final_response"] == "File written and verified."
    assert len(api.requests) == 3
    assert set(api.requests[0]["tools"][i]["function"]["name"] for i in range(3)) == {
        "terminal", "read_file", "write_file",
    }
    assert api.requests[1]["messages"][-1]["role"] == "tool"
    assert "written inside tenant" in api.requests[2]["messages"][-1]["content"]
    principal = store.principal_for_scope("11", "dm", "11")
    scope_room = store.get(principal, "profile", "history:" + room_key("11", "11", profile=home))
    assert scope_room[-1]["content"] == "File written and verified."
    assert _workspace(SimpleNamespace(cache_home=next((home / ".tenant-cache").rglob("room-*")))).joinpath("turn.txt").read_text() == "written inside tenant"

    # An allow-all group guest gets a genuine AIAgent instance, but no schema or tool round.
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    update_chat_settings(home, "group", "-10011", {"allow_all": True})
    guest = SimpleNamespace(user_id="12", chat_id="-10011", chat_type="group", thread_id=None)
    guest_store = LocalTenantStore(home, "99", allowed_user_ids=["11"])
    guest_runtime = TenantRuntime(home, guest_store, Broker(), None, None)
    guest_result = await guest_runtime.handle_message(None, SimpleNamespace(text="hello", message_id="guest"), guest)
    assert guest_result["final_response"] == "Guest conversation only."
    assert len(api.requests) == 4 and api.requests[-1].get("tools") is None

    # Outside a tenant ContextVar, the ordinary owner tool surface remains intact.
    owner = AIAgent(api_key="test-key", base_url="https://no-network.invalid/v1", model="tenant-test-model",
                    quiet_mode=True, enabled_toolsets=["file", "terminal", "todo", "memory"],
                    skip_context_files=True, skip_memory=True, session_db=None)
    assert "todo_list" in owner.valid_tool_names
    assert "write_file" in owner.valid_tool_names
    owner.close()
