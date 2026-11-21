from types import SimpleNamespace

import pytest

from gateway.tenant_runtime import TenantRuntime


class Store:
    def __init__(self):
        self.allowed = {"101": True, "202": True}
        self.rows = {}
    def authorization_generation(self, uid): return 0
    def group_authorization_generation(self, chat_id): return 0
    def principal_tenant(self, principal): return "tg-" + principal.user_id
    def get_access(self, uid): return {"allowed": self.allowed.get(str(uid), False), "banned": False}
    def get(self, principal, kind, key): return self.rows.get((principal.user_id, kind, key))
    def put(self, principal, kind, key, payload): self.rows[(principal.user_id, kind, key)] = payload


@pytest.mark.asyncio
async def test_default_agent_factory_is_used_with_empty_tools_and_duplicate_replay(monkeypatch, tmp_path):
    import run_agent
    import agent.mongo_accounting
    monkeypatch.setattr(agent.mongo_accounting, "account_store", lambda root: SimpleNamespace(record_turn=lambda *a: True))
    class MockAgent:
        calls = 0
        def __init__(self, **kwargs):
            assert kwargs["enabled_toolsets"] == []
            assert kwargs["skip_context_files"] and kwargs["skip_memory"]
            self.tools = ["must-be-removed"]
        def run_conversation(self, text, conversation_history=None, system_message=None):
            assert "isolated user conversation" in system_message
            assert "Never claim owner privileges" in system_message
            type(self).calls += 1
            return {"final_response": "hello", "messages": [], "usage": {"total_tokens": 2}}
    monkeypatch.setattr(run_agent, "AIAgent", MockAgent)
    store = Store()
    runtime = TenantRuntime(tmp_path, store, None, None)
    runtime._resolve_owner_provider = lambda principal=None: {"model": "approved", "provider": "test"}
    event = SimpleNamespace(text="hi", message_id=9, source=SimpleNamespace(user_id="101", chat_id="room"))
    first = await runtime.handle_message(None, event, event.source)
    second = await runtime.handle_message(None, event, event.source)
    assert first["final_response"] == second["final_response"] == "hello"
    assert MockAgent.calls == 1
    assert len([k for k in store.rows if k[2].startswith("history:")]) == 1
