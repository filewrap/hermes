"""The restricted tenant runtime is not an OS sandbox: no tool may escape it.

Exercise real dispatcher entry points with private canaries, including warm
owner discovery caches, inline tools and calls whose thread lost its context.
These tests deliberately do not claim that full-tool tenant isolation exists.
"""
from contextvars import Context
from types import SimpleNamespace
import json

import pytest

from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope


@pytest.mark.parametrize("boundary", ["schemas", "registry_schemas", "model", "registry"])
def test_tenant_catalog_and_direct_dispatch_never_reach_private_resources(tmp_path, monkeypatch, boundary):
    import model_tools
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    private = tmp_path / "owner-and-other-user-state"
    private.write_text("PRIVATE-CANARY")
    called = []

    def read_private(args, **kwargs):
        called.append(args)
        return json.dumps({"private": private.read_text()})

    # A concrete handler behind each sensitive tool name, not mocked dispatch.
    names = {"terminal", "execute_code", "read_file", "write_file", "browser_navigate",
             "mcp_private", "delegate_task", "cronjob", "send_message", "process",
             "memory", "session_search", "todo_list"}
    for name in names:
        registry.register(name=name, toolset="terminal", schema={"name": name, "parameters": {"type": "object"}}, handler=read_private)
    monkeypatch.setattr(model_tools, "registry", registry)
    model_tools._clear_tool_defs_cache()
    owner_defs = model_tools.get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True, skip_tool_search_assembly=True)
    assert owner_defs  # Warm the same schema cache before entering a tenant.
    scope = TenantScope(tmp_path, object(), "room-a", tmp_path / "a", "tg-a")
    token = set_tenant_scope(scope)
    try:
        if boundary == "schemas":
            assert model_tools.get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True, skip_tool_search_assembly=True) == []
        elif boundary == "registry_schemas":
            assert registry.get_definitions(names, quiet=True) == []
        else:
            dispatch = model_tools.handle_function_call if boundary == "model" else registry.dispatch
            for name in names | {"tool_call", "tool_search", "tool_describe", "connectors__private__read"}:
                result = dispatch(name, {"path": str(private), "name": "read_file", "arguments": {"path": str(private)}})
                assert "not available" in result
                assert "PRIVATE-CANARY" not in result
        assert called == []
    finally:
        reset_tenant_scope(token)
        model_tools._clear_tool_defs_cache()
    assert registry.get_definitions(names, quiet=True)
    assert "PRIVATE-CANARY" in registry.dispatch("read_file", {})


@pytest.mark.parametrize("boundary", ["batch", "sequential", "concurrent", "segmented", "invoke", "middleware"])
@pytest.mark.parametrize("binding", ["context", "agent_after_context_loss"])
def test_tenant_inline_concurrent_delegated_and_background_doors_are_denied(tmp_path, boundary, binding):
    from run_agent import AIAgent
    from agent import tool_executor
    from agent.agent_runtime_helpers import invoke_tool

    scope = TenantScope(tmp_path, object(), "room-a", tmp_path / "a", "tg-a")
    agent = SimpleNamespace(_tenant_scope=scope if binding == "agent_after_context_loss" else None)
    names = ("memory", "todo_list", "session_search", "delegate_task", "cronjob", "terminal", "tool_call", "send_message")
    calls = [SimpleNamespace(id=f"call-{i}", function=SimpleNamespace(name=name, arguments="{}")) for i, name in enumerate(names)]
    message = SimpleNamespace(tool_calls=calls)

    def run():
        if boundary == "invoke":
            for name in names:
                result = invoke_tool(agent, name, {}, "task", pre_tool_block_checked=True,
                                     skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
                assert "not available" in result
        elif boundary == "middleware":
            def forbidden(args):
                pytest.fail("Tenant call reached a handler")
            state = tool_executor._run_agent_tool_execution_middleware(agent, function_name="memory", function_args={},
                effective_task_id="task", tool_call_id="call-0", execute=forbidden)
            assert state.blocked and "not available" in state.result
        else:
            execute = {"batch": AIAgent._execute_tool_calls,
                       "sequential": tool_executor.execute_tool_calls_sequential,
                       "concurrent": tool_executor.execute_tool_calls_concurrent,
                       "segmented": tool_executor.execute_tool_calls_segmented}[boundary]
            messages = []
            execute(agent, message, messages, "task")
            assert len(messages) == len(calls)
            assert [m["tool_call_id"] for m in messages] == [c.id for c in calls]
            assert all("not available" in m["content"] for m in messages)

    token = set_tenant_scope(scope)
    try:
        # Empty Context simulates an unpropagated background/thread callback.
        Context().run(run) if binding == "agent_after_context_loss" else run()
    finally:
        reset_tenant_scope(token)
