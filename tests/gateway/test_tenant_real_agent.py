"""Tenant-runtime integration coverage using the real AIAgent constructor and turn loop.

Provider I/O is replaced at the agent API boundary; prompt assembly, security
hooks, tool filtering, and run_conversation remain production code.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import mongomock
import pytest

from agent import mongo_credentials
from agent.mongo_tenants import MongoTenantStore, Principal
from agent.mongo_accounting import MongoAccountingStore
from gateway.tenant_runtime import handle_tenant_message


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    from gateway import mongo_multiuser
    from agent import mongo_accounting
    from hermes_cli import runtime_provider
    import gateway.run
    import run_agent

    (tmp_path / ".env").write_text(
        "OWNER_TELEGRAM_ID=1\nMULTIUSER_ENABLED=true\n"
        "MONGODB_URI=mongodb://unused/hermes\n"
        "OWNER_CANARY_SECRET=never-expose-owner-secret\n"
        "OPENAI_API_KEY=owner-provider-key\n"
    )
    (tmp_path / "config.yaml").write_text("owner canary: ROOT-CONFIG-CANARY\n")
    (tmp_path / "SOUL.md").write_text("ROOT-SOUL-CANARY\n")
    (tmp_path / "AGENTS.md").write_text("ROOT-AGENTS-CANARY\n")
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text("ROOT-MEMORY-CANARY\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OWNER_CANARY_SECRET", "never-expose-owner-secret")
    monkeypatch.setattr(mongo_credentials, "_settings", lambda home: mongo_credentials._Settings("mongodb://unused/hermes"))
    database = mongomock.MongoClient().hermes
    store = MongoTenantStore(tmp_path, "1", database=database)
    store.ensure_owner()
    store.allow_user(Principal("1"), "2", True)
    accounting = MongoAccountingStore(tmp_path, database=database)
    monkeypatch.setattr(mongo_accounting, "account_store", lambda root: accounting)
    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda home: store)

    @contextmanager
    def owner_scope(root):
        yield
    monkeypatch.setattr(gateway.run, "_profile_runtime_scope", owner_scope)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda: {
        "provider": "openai", "api_key": "owner-provider-key",
        "base_url": "https://example.invalid/v1", "api_mode": "chat_completions",
    })
    monkeypatch.setattr(runtime_provider, "load_config", lambda: {"model": {"default": "fake-model"}})

    captured = []

    from openai.resources.chat.completions import Completions
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    import httpx

    class FakeStream:
        def __iter__(self):
            yield ChatCompletionChunk(id="offline", created=0, model="fake-model", object="chat.completion.chunk", choices=[{"index": 0, "delta": {"role": "assistant", "content": "safe answer"}, "finish_reason": None}])
            yield ChatCompletionChunk(id="offline", created=0, model="fake-model", object="chat.completion.chunk", choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    def fake_completion(client, *args, **kwargs):
        captured.append(kwargs)
        if kwargs.get("stream"):
            return FakeStream()
        return ChatCompletion(id="offline", created=0, model="fake-model", object="chat.completion", choices=[{"index": 0, "message": {"role": "assistant", "content": "safe answer"}, "finish_reason": "stop"}], usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Offline agent test attempted an unexpected network request")

    monkeypatch.setattr(Completions, "create", fake_completion)
    monkeypatch.setattr(httpx.Client, "send", forbidden_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_network)

    return tmp_path, store, accounting, captured


@pytest.mark.asyncio
async def test_real_agent_is_constructed_scoped_and_uses_safe_prompt(tenant):
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    root, store, accounting, captured = tenant
    store.put(Principal("2"), "memory", "MEMORY.md", "TENANT-TWO-MEMORY")
    source = SimpleNamespace(user_id="2", chat_id="2", chat_type="dm")
    event = SimpleNamespace(message=SimpleNamespace(id=1, message="hello"))
    result = await handle_tenant_message(None, event, source, root, store=store)

    assert "safe answer" in str(result)
    assert captured, "the real AIAgent conversation loop never reached its patched provider boundary"
    serialized = repr(captured)
    for canary in ("never-expose-owner-secret", "owner-provider-key", "ROOT-CONFIG-CANARY",
                   "ROOT-SOUL-CANARY", "ROOT-AGENTS-CANARY", "ROOT-MEMORY-CANARY"):
        assert canary not in serialized
    assert "TENANT-TWO-MEMORY" in serialized
    assert all(not request.get("tools") for request in captured)
    assert get_secret("OWNER_CANARY_SECRET") == "never-expose-owner-secret"
    assert accounting.totals(Principal("2"))["api_call_count"] == 1
    from pathlib import Path
    assert Path(get_hermes_home()) == root


@pytest.mark.asyncio
@pytest.mark.parametrize("uid,chat", [("3", "3"), ("2", "-100"), ("1", "-100")])
async def test_real_agent_other_users_and_groups_never_load_dm_or_owner_state(tenant, uid, chat):
    root, store, accounting, captured = tenant
    store.allow_user(Principal("1"), "3", True)
    for user in ("1", "2", "3"):
        store.put(Principal(user), "memory", "MEMORY.md", "DM-PRIVATE-" + user)
    source = SimpleNamespace(user_id=uid, chat_id=chat, chat_type="dm" if uid == chat else "group")
    event = SimpleNamespace(message=SimpleNamespace(id=1, message="hello"))
    result = await handle_tenant_message(None, event, source, root, store=store)
    assert "safe answer" in str(result)
    assert captured
    serialized = repr(captured)
    assert "owner-provider-key" not in serialized
    assert all(canary not in serialized for canary in ("ROOT-CONFIG-CANARY", "ROOT-SOUL-CANARY", "ROOT-AGENTS-CANARY", "ROOT-MEMORY-CANARY"))
    assert "DM-PRIVATE-2" not in serialized
    assert "DM-PRIVATE-1" not in serialized
    if uid == chat:
        assert "DM-PRIVATE-3" in serialized
    else:
        assert "DM-PRIVATE-3" not in serialized
    assert all(not request.get("tools") for request in captured)
    assert accounting.totals(Principal(uid))["api_call_count"] == 1
