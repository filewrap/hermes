"""Context-local tenant identity, with an agent-bound fallback for tool callbacks.

Routing is not a sandbox. Allowed tools execute exclusively through tenant_policy's
local kernel namespace; guest conversations retain the original tool-free boundary.
"""
from __future__ import annotations
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class TenantScope:
    root: Path
    principal: Any
    room: str
    cache_home: Path
    tenant: str
    user_id: str = ''
    chat_id: str = ''
    chat_type: str = 'dm'
    thread_id: str | None = None
    policy_allowed: bool = False
    authorization_generation: int | None = None

_scope: ContextVar[TenantScope | None] = ContextVar('hermes_tenant_scope', default=None)

def current_tenant_scope() -> TenantScope | None:
    return _scope.get()

def set_tenant_scope(scope: TenantScope) -> Token:
    return _scope.set(scope)

def reset_tenant_scope(token: Token) -> None:
    _scope.reset(token)

def capture_tenant_scope() -> TenantScope | None:
    return _scope.get()

def bind_tenant_scope(scope: TenantScope | None) -> Token:
    return _scope.set(scope)

def scope_for_agent(agent: Any = None) -> TenantScope | None:
    bound = getattr(agent, '_tenant_scope', None)
    active = current_tenant_scope()
    # An agent's bound identity is authoritative if callback context was lost or wrong.
    return bound if isinstance(bound, TenantScope) else active

def tenant_tool_denial(agent: Any = None) -> str | None:
    scope = scope_for_agent(agent)
    if scope is not None and not scope.policy_allowed:
        return 'This tool is not available in isolated tenant sessions.'
    return None

def tenant_tool_dispatch(name: str, args: dict, agent: Any = None) -> str | None:
    scope = scope_for_agent(agent)
    if scope is None:
        return None  # Owner/ordinary runtime: no policy restriction.
    from agent.tenant_policy import dispatch
    return dispatch(scope, name, args)

def tenant_tool_definitions(definitions: list[dict]) -> list[dict]:
    scope = current_tenant_scope()
    if scope is None:
        return definitions
    from agent.tenant_policy import supported_tool_names
    allowed = supported_tool_names(scope)
    return [entry for entry in definitions if entry.get('function', {}).get('name', entry.get('name')) in allowed]

def deny_tenant_tool_batch(agent: Any, assistant_message: Any, messages: list) -> bool:
    """Intercept ALL tenant tools before hooks/inline executors/rewrites/workers.

    Tenant calls are deliberately serial and cannot schedule deferred host work.
    Every attempted call gets a matching result, including unsupported tool names.
    """
    if scope_for_agent(agent) is None:
        return False
    import json
    from agent.tool_dispatch_helpers import make_tool_result_message
    for call in assistant_message.tool_calls:
        function = getattr(call, 'function', None)
        name = getattr(function, 'name', None) or 'unknown'
        call_id = getattr(call, 'id', None) or 'tenant-denied'
        try:
            args = getattr(function, 'arguments', '{}')
            args = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            args = None
        result = tenant_tool_dispatch(str(name), args, agent)
        messages.append(make_tool_result_message(str(name), result, str(call_id), effect_disposition='none' if '"error"' in result else 'unknown'))
    return True
