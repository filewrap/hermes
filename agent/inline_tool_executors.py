"""Agent-level ("inline") tool executors shared by the sequential and concurrent tool paths.

These tools need live ``AIAgent`` state (stores, callbacks, session DB) and therefore
bypass the tool registry. Each executor is ``fn(agent, args, ctx) -> result``; the
table replaces two hand-maintained if/elif chains (``invoke_tool`` and
``execute_tool_calls_sequential``) that had drifted apart. Tool modules are imported
lazily at call time so ``patch("tools.x.y")`` in tests keeps working.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Dict, Optional, Tuple

from agent.prompt_cache_scope import conversation_id
from tools.arg_coercion import coerce_tool_args
from tools.arg_validation import call_with_arg_checks
from tools.registry import registry


def tool_hook_ids(agent, effective_task_id: str, tool_call_id: Optional[str]) -> dict[str, str]:
    """Identity kwargs every tool hook/middleware call carries (all coerced to ``""``)."""
    return {
        "task_id": effective_task_id or "",
        "session_id": getattr(agent, "session_id", "") or "",
        "tool_call_id": tool_call_id or "",
        "turn_id": getattr(agent, "_current_turn_id", "") or "",
        "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
        "conversation_id": conversation_id(agent),
    }


def emit_terminal_post_tool_call(
    agent,
    *,
    function_name: str,
    function_args: dict,
    result: Any,
    effective_task_id: str,
    tool_call_id: Optional[str],
    duration_ms: int = 0,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    middleware_trace: Optional[list] = None,
) -> None:
    """Emit the one terminal ``post_tool_call`` hook for a tool_call_id (best-effort)."""
    try:
        from model_tools import _emit_post_tool_call_hook
        _emit_post_tool_call_hook(
            function_name=function_name,
            function_args=function_args,
            result=result,
            **tool_hook_ids(agent, effective_task_id, tool_call_id),
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
            middleware_trace=list(middleware_trace or []),
        )
    except Exception:
        pass


def apply_transform_tool_result(
    agent,
    *,
    function_name: str,
    function_args: dict,
    result: Any,
    effective_task_id: str,
    tool_call_id: Optional[str],
    duration_ms: int = 0,
) -> Any:
    """Apply ``transform_tool_result`` to an inline-dispatched tool's result.

    Registry tools get this inside ``handle_function_call``; inline executors never
    reach it, so the agent paths call the same helper (after the terminal
    ``post_tool_call``) to keep the hook's "every tool" contract. Fail-open."""
    try:
        from model_tools import _CallIds, _apply_transform_tool_result_hook
        return _apply_transform_tool_result_hook(
            function_name, function_args, result, duration_ms,
            _CallIds(**tool_hook_ids(agent, effective_task_id, tool_call_id)),
        )
    except Exception:
        return result


@dataclass
class InlineToolContext:
    """Per-call state an inline executor may need beyond its arguments."""

    effective_task_id: str
    tool_call_id: Optional[str] = None
    messages: Optional[list] = None


InlineToolExecutor = Callable[[Any, dict, InlineToolContext], Any]

# ``(kwarg, args_key)`` → ``args.get(key)``; ``(kwarg, args_key, default)`` → ``args.get(key, default)``.
_ArgSpec = tuple[Any, ...]


def _call_tool(module: str, func: str, args: dict, arg_specs: tuple[_ArgSpec, ...], **fixed: Any) -> Any:
    """Import ``module.func`` lazily and call it with args mapped per ``arg_specs`` plus ``fixed``."""
    fn = getattr(import_module(module), func)
    return fn(**{spec[0]: args.get(*spec[1:]) for spec in arg_specs}, **fixed)


def _tool(
    module: str, func: str, *arg_specs: _ArgSpec, **fixed: Callable[[Any, InlineToolContext], Any],
) -> InlineToolExecutor:
    """Executor calling ``module.func`` with mapped args plus ``fixed`` kwargs computed from ``(agent, ctx)``."""
    def _exec(agent, args: dict, ctx: InlineToolContext) -> Any:
        return _call_tool(module, func, args, arg_specs, **{k: f(agent, ctx) for k, f in fixed.items()})
    return _exec


def _callback_tool(module: str, func: str, callback_attr: str, *arg_specs: _ArgSpec) -> InlineToolExecutor:
    """Executor for a GUI-callback tool: mapped args plus ``callback=getattr(agent, callback_attr, None)``."""
    return _tool(module, func, *arg_specs, callback=lambda agent, ctx: getattr(agent, callback_attr, None))


def _session_db_or_unavailable(agent):
    """Live SessionDB for the F2 registry tools, or an unavailable error body."""
    session_db = agent._get_session_db_for_recall()
    if not session_db:
        from hermes_state import format_session_db_unavailable

        return None, json.dumps({"success": False, "error": format_session_db_unavailable()})
    return session_db, None


def _sessions_list(agent, args: dict, ctx: InlineToolContext) -> Any:
    session_db, error = _session_db_or_unavailable(agent)
    if error is not None:
        return error
    return _call_tool(
        "tools.sessions_tools", "sessions_list", args,
        (
            ("limit", "limit", 20), ("offset", "offset", 0),
            ("pinned_only", "pinned_only", False), ("archived", "archived", "active"),
            ("search", "search"), ("source", "source"), ("group", "group"),
            ("include_children", "include_children", False), ("profile", "profile"),
        ),
        db=session_db, current_session_id=agent.session_id,
    )


def _sessions(agent, args: dict, ctx: InlineToolContext) -> Any:
    session_db, error = _session_db_or_unavailable(agent)
    if error is not None:
        return error
    return _call_tool(
        "tools.sessions_tools", "sessions", args,
        (
            ("session_id", "session_id"), ("action", "action", "get"),
            ("label", "label"), ("pinned", "pinned"), ("archived", "archived"),
            ("hidden", "hidden"), ("group", "group"), ("name", "name"), ("to", "to"),
            ("targets", "targets"), ("profile", "profile"), ("confirm", "confirm", False),
        ),
        db=session_db, current_session_id=agent.session_id, parent_agent=agent,
    )


def _agents_list(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.sessions_tools", "agents_list", args, (),
        db=getattr(agent, "_session_db", None), current_session_id=agent.session_id,
    )


def _session_status(agent, args: dict, ctx: InlineToolContext) -> Any:
    # No _session_db_or_unavailable gate: unlike the F2 registry tools, the
    # model/usage half answers without a store, and the tool reports a missing
    # session block per-section instead of failing the whole call.
    return _call_tool(
        "tools.session_status_tool", "session_status", args,
        (("session_id", "session_id"),),
        db=getattr(agent, "_session_db", None), agent=agent,
        current_session_id=agent.session_id,
    )


def _presence(agent, args: dict, ctx: InlineToolContext) -> Any:
    # No _session_db_or_unavailable gate: like _session_status, the tool
    # reports per-section availability itself, and the live DB is only one
    # of its snapshot sources (other profiles, PairingStore, swarm, dashboard).
    return _call_tool(
        "tools.presence_tool", "presence", args,
        (
            ("action", "action", "list"), ("person", "person"),
            ("deviceId", "deviceId"), ("include", "include"),
            ("limit", "limit", 50),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
        current_session_id=agent.session_id,
    )


def _dashboard(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9b: boards live in state_meta under the current home; the tool
    # resolves its own handle when the agent carries none (registry
    # dispatch outside a turn), and reports store failures itself.
    return _call_tool(
        "tools.dashboard_tool", "dashboard", args,
        (
            ("action", "action", "read"), ("tabId", "tabId"),
            ("title", "title"), ("presentation", "presentation"),
            ("position", "position"), ("tabIds", "tabIds"),
            ("name", "name"), ("after", "after"),
            ("sizeW", "sizeW"), ("sizeH", "sizeH"), ("size", "size"),
            ("pluginKind", "pluginKind"), ("template", "template"),
            ("props", "props"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
        current_session_id=agent.session_id,
    )


def _portal(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9c: portal routes live in state_meta under the current home; same
    # handle pattern as _dashboard.
    return _call_tool(
        "tools.portal_tool", "portal", args,
        (
            ("action", "action", "list"), ("port", "port"),
            ("title", "title"), ("description", "description"),
            ("path", "path"), ("portal_id", "id"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
    )


def _plugins(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9d: plugin lifecycle over the Hermes plugin machinery; the tool reads
    # config itself, so no DB handle is needed.
    return _call_tool(
        "tools.plugins_tool", "plugins", args,
        (
            ("action", "action", "list"), ("pluginId", "pluginId"),
            ("name", "name"), ("query", "query"), ("force", "force"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
    )


def _gateway(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9d: gateway self-management; config/update seams are home-scoped, so
    # no DB handle is needed.
    return _call_tool(
        "tools.gateway_tool", "gateway", args,
        (
            ("action", "action"), ("path", "path"), ("dryRun", "dryRun"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
    )


def _nodes(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9e: paired-device registry + invoke; the tool resolves its own
    # registry handle from the agent's DB when it carries one.
    return _call_tool(
        "tools.nodes_tool", "nodes", args,
        (
            ("action", "action"), ("node", "node"), ("name", "name"),
            ("platform", "platform"), ("endpoint", "endpoint"),
            ("token", "token"), ("caps", "caps"), ("commands", "commands"),
            ("requestId", "requestId"), ("title", "title"), ("body", "body"),
            ("invokeCommand", "invokeCommand"), ("params", "params"),
            ("bins", "bins"), ("deviceId", "deviceId"),
            ("ptzOperation", "ptzOperation"),
            ("notificationKey", "notificationKey"),
            ("notificationAction", "notificationAction"),
            ("notificationReplyText", "notificationReplyText"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
    )


def _mobile_ui(agent, args: dict, ctx: InlineToolContext) -> Any:
    # F9e: Android UI observe/act via eligible nodes; same handle pattern.
    return _call_tool(
        "tools.mobile_ui_tool", "mobile_ui", args,
        (
            ("action", "action"), ("node", "node"),
            ("snapshotId", "snapshotId"), ("mobileAction", "mobileAction"),
            ("confirmed", "confirmed"),
        ),
        db=getattr(agent, "_session_db", None), agent=agent,
    )


def _sessions_yield(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.turn_control_tools", "sessions_yield", args,
        (("message", "message"), ("acknowledgment", "acknowledgment"), ("wait_for", "waitFor")),
        parent_agent=agent,
    )


def _agents_wait(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.turn_control_tools", "agents_wait", args,
        (("ids", "ids"), ("timeout_seconds", "timeoutSeconds"), ("await_results", "awaitResults")),
        parent_agent=agent,
    )


def _autonomous_done(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.autonomous_done_tool", "autonomous_done", args,
        (("action", "action"), ("summary", "summary")),
        parent_agent=agent,
    )


def _conversations_list(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.conversation_tools", "conversations_list", args,
        (("limit", "limit", 20), ("search", "search"), ("platform", "platform", "telegram")),
        parent_agent=agent,
    )


def _conversations_turn(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.conversation_tools", "conversations_turn", args,
        (("conversation_ref", "conversation_ref"), ("message", "message"),
         ("timeout_seconds", "timeoutSeconds", 60)),
        parent_agent=agent,
    )


def _sessions_send(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.sessions_send_tool", "sessions_send", args,
        (("session_key", "sessionKey"), ("message", "message")),
        db=getattr(agent, "_session_db", None), current_session_id=agent.session_id,
        parent_agent=agent,
    )


def _suggest_task(agent, args: dict, ctx: InlineToolContext) -> Any:
    # Blocks on the owner's card answer (approvals.timeout) like
    # conversations_turn blocks on the other side's reply: inline, on the
    # agent thread, so the wait is human-wait time for the batch deadline.
    return _call_tool(
        "tools.suggest_task_tool", "suggest_task", args,
        (("title", "title"), ("prompt", "prompt"), ("tldr", "tldr")),
        parent_agent=agent, current_session_id=agent.session_id,
    )


def _dismiss_task(agent, args: dict, ctx: InlineToolContext) -> Any:
    return _call_tool(
        "tools.suggest_task_tool", "dismiss_task", args,
        (("task_id", "task_id"),),
        current_session_id=agent.session_id,
    )


def _session_search(agent, args: dict, ctx: InlineToolContext) -> Any:
    session_db = agent._get_session_db_for_recall()
    if not session_db:
        from hermes_state import format_session_db_unavailable

        return json.dumps({"success": False, "error": format_session_db_unavailable()})
    return _call_tool(
        "tools.session_search_tool", "session_search", args,
        (
            ("query", "query", ""), ("role_filter", "role_filter"), ("limit", "limit"),
            ("session_id", "session_id"), ("around_message_id", "around_message_id"),
            ("window", "window", 5), ("sort", "sort"), ("profile", "profile"),
            ("detail", "detail", "adaptive"), ("after", "after"), ("before", "before"),
            ("exclude_session_ids", "exclude_session_ids"),
        ),
        db=session_db, current_session_id=agent.session_id,
    )


def _record_memory_write_provenance(agent, args: dict, result: Any) -> None:
    """Tag the entries a memory-tool call committed with their provenance class
    (``agent/memory_model.py``), durably in the store's sidecar, so a later session
    retrieves each fact WITH its tag. Best-effort: recording never fails the write,
    and an unresolvable entry stays untagged rather than mis-tagged."""
    store = getattr(agent, "_memory_store", None)
    if store is None:
        return
    from agent.memory_model import committed_memory_write_entries, resolve_memory_origin_class

    rows = committed_memory_write_entries(args, result)
    if not rows:
        return
    origin = resolve_memory_origin_class(agent)
    session_id = getattr(agent, "session_id", None) or None
    for row in rows:
        if row["action"] == "remove":
            store.drop_entry_origin(row["target"], row["entry"])
            continue
        if row.get("previous_entry"):
            store.drop_entry_origin(row["target"], row["previous_entry"])
        store.record_entry_origin(
            row["target"], row["entry"], origin, session_id=session_id,
            replace=row["action"] in {"replace", "patch"},
        )


def _memory(agent, args: dict, ctx: InlineToolContext) -> Any:
    result = _call_tool(
        "tools.memory_tool", "memory_tool", args,
        (
            ("action", "action"), ("target", "target"), ("content", "content"),
            ("old_text", "old_text"), ("new_text", "new_text"), ("operations", "operations"),
        ),
        store=agent._memory_store,
    )
    # Tag the committed entries with their provenance class (durable sidecar);
    # a recording failure must never fail the write itself.
    with suppress(Exception):
        _record_memory_write_provenance(agent, args, result)
    # Mirror built-in memory writes to external providers; gating lives in
    # MemoryManager.notify_memory_tool_write.
    if agent._memory_manager:
        agent._memory_manager.notify_memory_tool_write(
            result,
            args,
            build_metadata=lambda: agent._build_memory_write_metadata(
                task_id=ctx.effective_task_id,
                tool_call_id=ctx.tool_call_id,
            ),
        )
    return result


_read_preview = _callback_tool(
    "tools.read_preview_tool", "read_preview_tool", "read_preview_callback",
    ("start", "start"), ("count", "count"),
)


def _desktop_preview(agent, args: dict, ctx: InlineToolContext) -> Any:
    # action=read needs the GUI callback (agent-level); open/close go through the
    # registry handler like any other tool.
    if (args.get("action") or "").strip() == "read":
        return _read_preview(agent, args, ctx)
    from tools.preview_tool import _handle_preview

    return _handle_preview(args)


def _preview_is_default_vault_target(agent, task_id: str) -> bool:
    """An omitted ``browser_vault`` target means the in-app browser when this session can drive it (its
    ``drive_preview``, eager or deferred behind ``tool_call``) and has no managed browser page of its own:
    a Desktop agent that opened a page with browser_exec keeps filling that page."""
    names = getattr(agent, "valid_tool_names", ()) or ()
    preview = "drive_preview" in names
    if not preview and "tool_call" in names:
        from agent.tool_executor import _tool_search_scoped_names

        preview = "drive_preview" in _tool_search_scoped_names(agent)
    if not preview:
        return False
    from agent.browser_page_registry import backend_for_task
    from tools.browser_session import session_for_task
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    # A plugin backend (e.g. Camofox) holding the task's live page owns the fill: browser_vault's own
    # backend selection already picks it, so an omitted target must NOT be forced to preview over it.
    # Fall to preview only when nothing else owns the page.
    return (not session_for_task(task_id) and SUPERVISOR_REGISTRY.get(task_id) is None
            and backend_for_task(task_id) is None)


def _browser_vault(agent, args: dict, ctx: InlineToolContext) -> Any:
    # The preview transport is the agent's GUI callback, which registry dispatch never forwards.
    import tools.browser_vault_tool  # registers the real handler
    from model_tools import _approval_observability, _CallIds
    from tools.registry import registry

    task_id = ctx.effective_task_id or "default"
    if "target" not in args and (args.get("action") in ("fill", "save_login", "enter_code")
                                 and _preview_is_default_vault_target(agent, task_id)):
        args = {**args, "target": "preview"}
    # A plugin may override browser_vault: give its handler (and the approval prompts it raises)
    # the same session/turn identity the registry path in model_tools._execute_tool binds. No
    # turn_origin: a plugin fork never holds an inline tool (plugin_session_fork.fork_tool_allowlist).
    ids = tool_hook_ids(agent, ctx.effective_task_id, ctx.tool_call_id)
    with _approval_observability(_CallIds(**ids)):
        return registry.dispatch("browser_vault", args, task_id=ctx.effective_task_id,
                                 session_id=ids["session_id"], conversation_id=ids["conversation_id"],
                                 preview_callback=getattr(agent, "drive_preview_callback", None))


def _manage_connections(agent, args: dict, ctx: InlineToolContext) -> Any:
    # The GUI callback lives on the agent; registry dispatch never forwards it.
    from tools.connectors import manage_connections
    from tools.connectors.gateway import config as gateway_config

    result = manage_connections(
        args, session_id=getattr(agent, "session_id", None), tool_call_id=ctx.tool_call_id,
        turn_id=getattr(agent, "_current_turn_id", None) or None,
        connection_callback=getattr(agent, "connection_callback", None),
        connectors_available=gateway_config.connectors_available,
        card_surface=getattr(agent, "platform", None) == "desktop",
    )
    _scope_in_connected_mcp_servers(agent, result)
    return result


def _scope_in_connected_mcp_servers(agent, result: Any) -> None:
    """Add the MCP servers this call connected to the agent's toolset selection.

    ``tool_describe``/``tool_call`` resolve names inside that selection, and it was fixed when the
    agent was built, so a server registered a moment ago is otherwise "not found" for the rest of
    the turn the result calls it available in. Only the selection changes; ``agent.tools`` does
    not, so the sent tool schema bytes stay the same."""
    enabled = getattr(agent, "enabled_toolsets", None)
    if enabled is None or "no_mcp" in enabled:  # None already means every toolset
        return
    try:
        targets = json.loads(result).get("targets") or []
    except (AttributeError, TypeError, ValueError):
        return
    connected = [str(t.get("name")) for t in targets if isinstance(t, dict)
                 and t.get("kind") == "mcp" and t.get("state") == "connected" and t.get("tools")]
    added = [name for name in connected if name not in enabled]
    if added:
        agent.enabled_toolsets = [*enabled, *added]


def _manage_catalog(agent, args: dict, ctx: InlineToolContext) -> Any:
    # The card callback lives on the agent; only a desktop chat draws catalog rows.
    from tools.connectors.catalog_tool import manage_catalog

    return manage_catalog(
        args, session_id=getattr(agent, "session_id", None), tool_call_id=ctx.tool_call_id,
        connection_callback=getattr(agent, "connection_callback", None),
        card_surface=getattr(agent, "platform", None) == "desktop",
    )


def _setup_beat(agent, args: dict, ctx: InlineToolContext) -> Any:
    # The run record lives on the session row and the card callback on the agent. A card the user wrote
    # past ends the turn as a new message does: setup_beat runs inline (never parallel), so its result is
    # kept and the loop stops before the next model call.
    from agent.interrupt_control import _REASON_NEW_MESSAGE
    from tools.setup_beat_tool import setup_beat_tool

    return setup_beat_tool(
        args, db=getattr(agent, "_session_db", None), session_id=getattr(agent, "session_id", None) or "",
        tool_call_id=ctx.tool_call_id or "", callback=getattr(agent, "setup_beat_callback", None),
        end_turn=lambda: agent.interrupt(tool_reason=_REASON_NEW_MESSAGE),
    )


def _setup_mcp_shim(agent, args: dict, ctx: InlineToolContext) -> Any:
    # Replay shim for conversations whose cached prompt still names setup_mcp.
    # Not in _LEGACY_TOOL_ALIASES: inline tools bypass handle_function_call.
    return _manage_connections(agent, {
        "action": args.get("action", "install"),
        "connectors": [{"name": args.get("server", ""), "mcp": True}],
    }, ctx)


# Order is the historical if/elif order of ``execute_tool_calls_sequential``.
_RAW_INLINE_TOOL_EXECUTORS: dict[str, InlineToolExecutor] = {
    "todo_list": _tool(
        "tools.todo_tool", "todo_tool", ("todos", "todos"), ("merge", "merge", False),
        store=lambda agent, ctx: agent._todo_store,
    ),
    # Bot Mode teammate DM is injected, not registered: only a canonical Bot
    # Chat session carries the schema, and the tool re-gates on the title.
    "message_agent": _tool(
        "tools.bot_mode_dm", "message_agent_tool", ("target", "target", ""), ("message", "message", ""),
        ("expects_reply", "expects_reply", True), ("cancel", "cancel", ""),
        ("constraints", "constraints", ""), ("expecting", "expecting", ""),
        task_id=lambda agent, ctx: ctx.effective_task_id, agent=lambda agent, ctx: agent,
    ),
    "session_search": _session_search,
    "sessions_list": _sessions_list,
    "sessions": _sessions,
    "agents_list": _agents_list,
    "session_status": _session_status,
    "presence": _presence,
    "dashboard": _dashboard,
    "portal": _portal,
    "plugins": _plugins,
    "gateway": _gateway,
    "nodes": _nodes,
    "mobile_ui": _mobile_ui,
    "sessions_yield": _sessions_yield,
    "agents_wait": _agents_wait,
    "autonomous_done": _autonomous_done,
    "conversations_list": _conversations_list,
    "conversations_turn": _conversations_turn,
    "sessions_send": _sessions_send,
    "suggest_task": _suggest_task,
    "dismiss_task": _dismiss_task,
    "memory": _memory,
    "memory_search": _tool(
        "tools.memory_search_tool", "memory_search",
        ("query", "query", ""), ("target", "target", "all"), ("max_results", "max_results"),
        store=lambda agent, ctx: agent._memory_store,
    ),
    "memory_get": _tool(
        "tools.memory_search_tool", "memory_get",
        ("target", "target", "memory"), ("index", "index"),
        ("start_char", "start_char", 0), ("max_chars", "max_chars"),
        store=lambda agent, ctx: agent._memory_store,
    ),
    "clarify": _tool(
        "tools.clarify_tool", "clarify_tool", ("questions", "questions"),
        callback=lambda agent, ctx: agent.clarify_callback,
    ),
    "read_terminal": _callback_tool(
        "tools.read_terminal_tool", "read_terminal_tool", "read_terminal_callback",
        ("start_line", "start_line"), ("count", "count"),
    ),
    "desktop_preview": _desktop_preview,
    "browser_vault": _browser_vault,
    "drive_preview": _callback_tool(
        "tools.drive_preview_tool", "drive_preview_tool", "drive_preview_callback",
        ("action", "action", ""), ("ref", "ref"), ("selector", "selector"), ("text", "text"),
        ("key", "key"), ("submit", "submit"), ("amount", "amount"), ("to", "to"), ("limit", "max"),
    ),
    "annotate_preview": _callback_tool(
        "tools.annotate_preview_tool", "annotate_preview_tool", "drive_preview_callback",
        ("action", "action", "add"), ("ref", "ref"), ("selector", "selector"), ("label", "label"),
    ),
    "read_window_below": _callback_tool(
        "tools.read_window_tool", "read_window_below_tool", "read_window_below_callback",
    ),
    "gui_tour": _callback_tool(
        "tools.tour_tool", "tour_tool", "tour_callback",
        ("action", "action", ""), ("surface", "surface"), ("selector", "selector"), ("title", "title"),
        ("text", "text"), ("side", "side"), ("steps", "steps"), ("step_index", "step_index"),
        ("preset", "preset"),
    ),
    "set_theme": _callback_tool(
        "tools.set_theme_tool", "set_theme_tool", "theme_callback",
        ("accent", "accent"), ("mode", "mode"), ("layout", "layout"), ("picker", "picker"),
    ),
    "setup_beat": _setup_beat,
    "manage_connections": _manage_connections,
    "manage_catalog": _manage_catalog,
    "setup_mcp": _setup_mcp_shim,
    "delegate_task": lambda agent, args, ctx: agent._dispatch_delegate_task(args),
}


def _coerced(name: str, executor: InlineToolExecutor) -> InlineToolExecutor:
    def _exec(agent, args: dict, ctx: InlineToolContext) -> Any:
        from tools.hosted_tool_policy import hosted_tool_refusal
        if refusal := hosted_tool_refusal(name):  # the table is an execution primitive: same allowlist
            return json.dumps({"error": refusal}, ensure_ascii=False)
        return call_with_arg_checks(
            registry.get_entry(name), coerce_tool_args(name, args), lambda checked: executor(agent, checked, ctx))
    return _exec


INLINE_TOOL_EXECUTORS: dict[str, InlineToolExecutor] = {
    name: _coerced(name, executor) for name, executor in _RAW_INLINE_TOOL_EXECUTORS.items()
}

# ``invoke_tool`` (concurrent path) consults the memory manager right after these three
# names and before the remaining inline tools; ``message_agent`` falls through to the
# registry there (Bot Mode DM is only injected into the sequential path's schema).
INVOKE_TOOL_PRE_MEMORY_MANAGER_NAMES = frozenset({"todo_list", "session_search", "memory", "memory_search", "memory_get"})


def resolve_invoke_tool_executor(agent, function_name: str) -> Optional[InlineToolExecutor]:
    """Inline executor for ``invoke_tool`` (concurrent path), or None for registry dispatch.

    Precedence: todo_list/session_search/memory, then memory-manager tools, then the
    remaining inline tools (``message_agent`` excluded).
    """
    if function_name in INVOKE_TOOL_PRE_MEMORY_MANAGER_NAMES:
        return INLINE_TOOL_EXECUTORS[function_name]
    if is_memory_provider_tool(agent, function_name):
        return lambda agent, args, ctx: agent._memory_manager.handle_tool_call(
            function_name, args,
            caller_gsk=(getattr(agent, "_gateway_session_key", "") or ""),
        )
    if function_name == "message_agent":
        return None
    return INLINE_TOOL_EXECUTORS.get(function_name)


def is_memory_provider_tool(agent, function_name: str) -> bool:
    """``function_name`` is served by the agent's memory provider (both paths, before the registry)."""
    memory_manager = getattr(agent, "_memory_manager", None)
    return bool(memory_manager) and memory_manager.has_tool(function_name)


def is_context_engine_tool(agent, function_name: str) -> bool:
    """``function_name`` is served by the agent's context engine (sequential path, before the registry)."""
    names = getattr(agent, "_context_engine_tool_names", None)
    return bool(names) and function_name in names


def pre_registry_route(function_name: str, agent: Any = None) -> Optional[str]:
    """Which seam dispatches ``function_name`` instead of its tool-registry handler on some path
    (``model_tools.handle_function_call``, the sequential path, ``invoke_tool``), or None when every
    path reaches the registry handler: ``alias``, ``bridge``, ``inline``, ``connector``, and with
    ``agent`` also ``context_engine`` / ``memory_provider``. Each test is the one the dispatcher
    runs, so a registration under such a name never runs its own handler there."""
    from model_tools import _AGENT_LOOP_TOOLS, _LEGACY_TOOL_ALIASES
    from tools.connectors import is_connector_name
    from tools.tool_search import is_bridge_tool

    if function_name in _LEGACY_TOOL_ALIASES:
        return "alias"
    if is_bridge_tool(function_name):
        return "bridge"
    if function_name in INLINE_TOOL_EXECUTORS or function_name in _AGENT_LOOP_TOOLS:
        return "inline"
    if is_connector_name(function_name):
        return "connector"
    if agent is not None and is_context_engine_tool(agent, function_name):
        return "context_engine"
    if agent is not None and is_memory_provider_tool(agent, function_name):
        return "memory_provider"
    return None
