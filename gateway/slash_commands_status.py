"""Read-only gateway introspection commands: /status, /context, /usage, /agents, /insights, /topup.
Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``."""

from __future__ import annotations

import logging
import asyncio
import hashlib
import html
import os
import re
import time
from typing import Any

from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session_transcript import TranscriptReadError
from hermes_cli.status_report import build_status_fields

# Log-record parity with gateway/run.py and the origin module.
logger = logging.getLogger("gateway.run")

_LIST_CAP = 12  # /agents shows at most this many rows per section


def _clean_str(value: Any) -> str:
    """Strip and return a non-empty string value, or empty string."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _telegram_status_tables(lines: list[str], platforms: list[str]) -> str:
    """Render real HTML tables, which Telegram Rich Messages can display."""
    header = lines[0] if lines else "Hermes Gateway Status"
    details: list[tuple[str, str]] = []
    notes: list[str] = []
    for line in lines[2:]:
        if not line.strip():
            continue
        if line.startswith("**") and ":**" in line:
            label, value = line[2:].split(":**", 1)
            if label.strip().casefold() != "connected platforms":
                value = value.strip()
                if " _(" in value and value.endswith("_"):
                    value, note = value.rsplit(" _(", 1)
                    if note.endswith(")_"):
                        notes.append(note[:-2])
                details.append((label.strip(), value))
        else:
            notes.append(line.strip())
    def inline_markup(value: Any) -> str:
        safe = html.escape(str(value).replace("\n", " "), quote=False)
        safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)

    result = [f"<h3>{inline_markup(header)}</h3>", "<h4>Session details</h4>",
              "<table><thead><tr><th>Detail</th><th>Value</th></tr></thead><tbody>"]
    result.extend(f"<tr><td>{inline_markup(label)}</td><td>{inline_markup(value)}</td></tr>"
                  for label, value in details)
    result.extend(["</tbody></table>", "<h4>Connected Platforms</h4>",
                   "<table><thead><tr><th>Platform</th><th>State</th></tr></thead><tbody>"])
    result.extend(f"<tr><td>{inline_markup(platform)}</td><td>Connected</td></tr>"
                  for platform in sorted(set(platforms)))
    result.append("</tbody></table>")
    if notes:
        result.extend(f"<p>{inline_markup(note)}</p>" for note in notes)
    return "\n".join(result)


def _int_value(value: Any) -> int:
    """Safely coerce to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _n(obj, attr: str):
    return getattr(obj, attr, 0) or 0


def _fmt(n) -> str:
    return f"{n:,}"


def _pct(used, total) -> float:  # clamped occupancy percentage; 0 for an unknown window
    return min(100, used / total * 100) if total else 0


def _clip(text: str, limit: int) -> str:
    return text[: limit - 3] + "..." if len(text) > limit else text


def _transcript_estimate(history) -> tuple[int, int]:
    """``(approx_tokens, message_count)`` over the user/assistant messages of a transcript."""
    from agent.model_metadata import estimate_messages_tokens_rough
    msgs = [m for m in history if m.get("role") in {"user", "assistant"} and m.get("content")]
    return estimate_messages_tokens_rough(msgs), len(msgs)


async def _quiet(call, default=None):
    """Await ``call()`` fail-open: any exception (sync or in the awaitable) yields *default*."""
    try:
        return await call()
    except Exception:
        return default


# English source for the transcript-read-failure reply; callers render ``history_unreadable()``
# (``gateway.shared.history_unreadable``) so the active language applies.
HISTORY_UNREADABLE = ("I can't read this conversation's history right now (your earlier messages "
                      "exist but cannot be loaded). Run `hermes doctor --fix` on the host, or use /new "
                      "to start fresh.")


def history_unreadable() -> str:
    return t("gateway.shared.history_unreadable")


def _configured_provider() -> str:
    """``model.provider`` from the gateway config ("" when unset)."""
    from gateway.run import _load_gateway_config
    user_config = _load_gateway_config()
    model_cfg = user_config.get("model", {}) if isinstance(user_config, dict) else {}
    return _clean_str(model_cfg.get("provider")) if isinstance(model_cfg, dict) else ""


def _quiet_sync(call, default=None):
    """Sync twin of ``_quiet``."""
    try:
        return call()
    except Exception:
        return default


def _status_model_route(
    status_agent, active_override: dict, persisted_route: dict, session_row: dict, session_entry
):
    """``(model, provider, context_used, context_total, route)`` for /status.

    Order: live/cached agent route -> active session override -> persisted recent route ->
    SessionDB row -> gateway config (only loaded when something is still missing). ``route`` carries
    the ``provider`` / ``base_url`` / ``api_key`` of the winning source only, so a later context-window
    lookup queries the endpoint that serves the displayed model (never a losing route's endpoint);
    a winner without a ``base_url`` leaves the lookup on the default runtime route.
    """
    from gateway.run import _AGENT_PENDING_SENTINEL, _load_gateway_config, _resolve_gateway_model
    context_used = context_total = 0
    routes: list[tuple[str, str, dict]] = []
    if status_agent is not None and status_agent is not _AGENT_PENDING_SENTINEL:
        routes.append((_clean_str(getattr(status_agent, "model", "")),
                       _clean_str(getattr(status_agent, "provider", "")),
                       {"base_url": _clean_str(getattr(status_agent, "base_url", "")),
                        "api_key": _clean_str(getattr(status_agent, "api_key", ""))}))
        ctx = getattr(status_agent, "context_compressor", None)
        if ctx is not None:
            context_used = max(0, _int_value(getattr(ctx, "last_prompt_tokens", 0)))
            context_total = _int_value(getattr(ctx, "context_length", 0))
    routes.append((_clean_str(active_override.get("model")),
                   _clean_str(active_override.get("provider")),
                   {"base_url": _clean_str(active_override.get("base_url")),
                    "api_key": _clean_str(active_override.get("api_key"))}))
    routes.append((_clean_str(persisted_route.get("model")),
                   _clean_str(persisted_route.get("billing_provider")), {}))
    row_route = (_clean_str(session_row.get("model")), _clean_str(session_row.get("billing_provider")), {})
    # First fully-resolved (model AND provider) route wins; the SessionDB row is used even if partial.
    model_name, provider_name, route = next((r for r in routes if r[0] and r[1]), row_route)
    context_used = context_used or _int_value(getattr(session_entry, "last_prompt_tokens", 0))
    user_config: dict[str, Any] = {}
    if not model_name or not provider_name:
        user_config = _quiet_sync(_load_gateway_config, {})
    model_cfg = user_config.get("model", {}) if isinstance(user_config, dict) else {}
    model_cfg = model_cfg if isinstance(model_cfg, dict) else {}
    model_name = model_name or _resolve_gateway_model(user_config)
    provider_name = provider_name or _clean_str(model_cfg.get("provider"))
    # No raw ``model.context_length`` pin here: the resolver applies it only while the displayed
    # route still matches the configured one (a session /model switch must not inherit it).
    return model_name, provider_name, context_used, context_total, {"provider": provider_name, **route}


def _context_compressor_lines(agent, ctx, used: int) -> list[str]:
    """/context full view: auto-compression threshold/headroom, compression count + last savings,
    and cumulative throughput (labelled as throughput, NOT context size)."""
    lines: list[str] = []
    from agent.context_breakdown import context_display_source
    mark = "~" if context_display_source(ctx) != "provider_usage" else ""
    threshold = _n(ctx, "threshold_tokens")
    threshold_pct = f"{_n(ctx, 'threshold_percent') * 100:.0f}"
    if threshold > 0:
        if used >= threshold:
            lines.append(t("gateway.context.over_threshold", threshold=_fmt(threshold),
                           threshold_pct=threshold_pct))
        else:
            lines.append(t("gateway.context.threshold", threshold=_fmt(threshold),
                           threshold_pct=threshold_pct, to_go=mark + _fmt(threshold - used)))
    compressions = _n(ctx, "compression_count")
    lines.append(t("gateway.context.compressions", count=compressions))
    savings = getattr(ctx, "_last_compression_savings_pct", None) if compressions else None
    if savings is not None:
        lines.append(t("gateway.context.last_savings", savings=f"{savings:.0f}"))
    lines += [
        "",
        t("gateway.context.totals_header", calls=_n(agent, "session_api_calls")),
        t("gateway.context.totals_line",
          input=_fmt(_n(agent, "session_input_tokens")),
          output=_fmt(_n(agent, "session_output_tokens")),
          reasoning=_fmt(_n(agent, "session_reasoning_tokens"))),
        t("gateway.context.total_billed", total=_fmt(_n(agent, "session_total_tokens"))),
        t("gateway.context.throughput_note"),
    ]
    return lines


def _telegram_context_html(
    *, agent, ctx, used: int, context_length: int, model_name: str, estimated: bool,
    payload: dict | None = None, details: dict | None = None, files: list[dict] | None = None,
    expanded: bool = False, transcript_estimate: tuple[int, int] | None = None,
    breakdown_error: str | None = None, persisted_usage: dict | None = None,
    files_available: bool = False,
) -> str:
    """Render the complete /context hierarchy using Telegram's native HTML format.

    Persisted occupancy and SessionDB throughput survive a gateway restart; compressor
    internals and prompt/tool inventories do not. Keep every explanatory section visible
    and label unavailable live-only facts instead of silently dropping them.
    """
    def esc(value: Any, limit: int = 160) -> str:
        text = str(value if value is not None else "")
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return html.escape(text, quote=True)

    def row(*cells: str, header: bool = False) -> str:
        tag = "th" if header else "td"
        return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"

    parts = ["<h1>Context window</h1>",
             f"<p><strong>Model</strong>　<code>{esc(model_name or 'Unknown')}</code></p>",
             "<p>The context window is what the model can consider in one request: instructions, tool definitions, skill information, and this conversation. Tokens are chunks of text, not messages.</p>",
             "<h2>Current model window</h2>",
             "<p>Current prompt occupancy, not the sum of tokens used over the session.</p>",
             "<table><thead>" + row("Measure", "Tokens", "Window", header=True) + "</thead><tbody>"]
    mark = "~" if estimated else ""
    if context_length > 0:
        parts.append(row("In use", f"{mark}{_fmt(max(0, used))}" if used > 0 else "Unavailable",
                         f"{mark}{used / context_length * 100:.0f}%" if used > 0 else "Unavailable"))
        parts.append(row("Capacity", _fmt(context_length), "100%"))
        headroom = max(0, context_length - used) if used > 0 else None
        parts.append(row("Headroom", f"{mark}{_fmt(headroom)}" if headroom is not None else "Unavailable",
                         f"{headroom / context_length * 100:.0f}%" if headroom is not None else "Unavailable"))
    else:
        parts.extend([row("In use", "Unavailable", "Unavailable"),
                      row("Capacity", "Unavailable", "Unavailable"),
                      row("Headroom", "Unavailable", "Unavailable")])
    parts.append("</tbody></table>")
    parts.append("<p><code>Usage % = in-use tokens ÷ capacity × 100</code><br>"
                 "<code>Headroom = capacity − in-use tokens</code><br>Displayed window percentages are rounded.</p>")
    if transcript_estimate is not None:
        estimate, message_count = transcript_estimate
        parts.append(
            f"<blockquote><strong>Transcript estimate</strong><br>~{_fmt(estimate)} tokens across "
            f"{_fmt(message_count)} messages. This is a rough transcript-only estimate, not provider "
            "usage or the current prompt size.</blockquote>"
        )

    parts.extend(["<h2>Automatic compression</h2>"])
    if ctx is not None and _n(ctx, "threshold_tokens") > 0:
        threshold = _n(ctx, "threshold_tokens")
        threshold_pct = _n(ctx, "threshold_percent") * 100
        if used > 0:
            remaining = max(0, threshold - used)
            state = (f"Auto-compression threshold reached at {_fmt(threshold)} tokens ({threshold_pct:.0f}%); "
                     "compression eligibility depends on the active compressor.") if used >= threshold else (
                     f"Auto-compression at {_fmt(threshold)} tokens ({threshold_pct:.0f}%); "
                     f"{mark}{_fmt(remaining)} tokens to go.")
        else:
            state = f"Threshold: {_fmt(threshold)} tokens ({threshold_pct:.0f}%); current distance unavailable."
        parts.append(f"<p>{esc(state)}</p>")
    else:
        parts.append("<p>Threshold and remaining tokens unavailable: no live compressor state is persisted across restart.</p>")
    if ctx is not None and hasattr(ctx, "compression_count"):
        compressions = max(0, _int_value(getattr(ctx, "compression_count", 0)))
        text = f"<strong>Compression count</strong>　{_fmt(compressions)}"
        savings = getattr(ctx, "_last_compression_savings_pct", None) if compressions else None
        if savings is not None:
            text += f"　·　<strong>Last savings</strong>　{esc(f'{savings:.0f}%')}"
        parts.append(f"<p>{text}</p>")
    else:
        parts.append("<p><strong>Compression count</strong>　Unavailable after restart (not persisted).</p>")

    # SessionDB is durable across restarts. Prefer the resident agent's live counters
    # when present; otherwise use the persisted session row, never fabricated zeroes.
    row_data = persisted_usage if isinstance(persisted_usage, dict) else {}
    usage_fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                    "reasoning_tokens", "api_call_count")
    has_usage = agent is not None or any(field in row_data for field in usage_fields)
    def metric(agent_name: str, db_name: str) -> int:
        if agent is not None and hasattr(agent, agent_name):
            return max(0, _int_value(getattr(agent, agent_name)))
        return max(0, _int_value(row_data.get(db_name)))
    parts.extend(["<h2>Session throughput</h2>",
                  "<p>Cumulative session totals; these are not the current model window.</p>",
                  "<p>Each model call sends its context again, so cumulative tokens can exceed the window capacity. They measure work across calls, not how much context is currently occupied.</p>",
                  "<p>Reasoning is reported separately and is part of output; the total avoids double-counting it.</p>",
                  "<table><thead>" + row("Measure", "Tokens / count", header=True) + "</thead><tbody>"])
    if has_usage:
        input_tokens = metric("session_input_tokens", "input_tokens")
        output_tokens = metric("session_output_tokens", "output_tokens")
        reasoning = metric("session_reasoning_tokens", "reasoning_tokens")
        total = (max(0, _int_value(getattr(agent, "session_total_tokens", 0))) if agent is not None and
                 hasattr(agent, "session_total_tokens") else sum(metric("", field) for field in (
                     "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")))
        parts.append(row("API calls", _fmt(metric("session_api_calls", "api_call_count"))))
        parts.append(row("Input", _fmt(input_tokens)))
        parts.append(row("Output", _fmt(output_tokens)))
        parts.append(row("Reasoning", _fmt(reasoning)))
        parts.append(row("Cache read", _fmt(metric("session_cache_read_tokens", "cache_read_tokens"))))
        parts.append(row("Cache write", _fmt(metric("session_cache_write_tokens", "cache_write_tokens"))))
        parts.append(row("Total input + output + cache", _fmt(total)))
    else:
        parts.append(row("Usage totals", "Unavailable: no resident agent or persisted usage record"))
    parts.append("</tbody></table>")

    parts.append("<h2>Estimated usage by category</h2>")
    parts.append("<p>Local estimates; category tokenization is approximate. Shares use the model-window capacity.</p>")
    parts.append("<p>System prompt: instructions and memory. Tool definitions: callable-tool schemas. Skills: the skill index and any loaded skill text. Conversation: messages and tool results. These local estimates may differ from provider-reported usage.</p>")
    if agent is None:
        parts.append("<p>After restart, only persisted conversation text can be estimated here; system-prompt, tool, and skill inventories require the resident agent.</p>")
    categories = list((payload or {}).get("categories") or [])
    if categories:
        parts.append("<table><thead>" + row("Category", "Estimate", "Share of window", header=True) + "</thead><tbody>")
        for category in categories:
            count = max(0, _int_value(category.get("tokens")))
            label = esc(category.get("label") or category.get("id") or "Other")
            share = f"~{count / context_length * 100:.1f}%" if context_length else "Unavailable"
            parts.append(row(label, f"~{_fmt(count)} tokens", share))
        parts.append("</tbody></table>")
    else:
        parts.append("<p>Category estimates unavailable: reconstructing system prompt, tools, and skills requires the "
                     "session's resident agent, which is not persisted across restart.</p>")
    if breakdown_error:
        parts.append(f"<p>{esc(breakdown_error)}</p>")

    file_rows = list(files or [])
    parts.append("<h2>Context files and sizes</h2>")
    if file_rows:
        labels = {
            "loaded": "Loaded", "truncated": "Truncated — over context_file_max_chars",
            "shadowed": "Not loaded — higher-priority context type wins",
            "blocked": "Not loaded — blocked by the prompt-injection scan",
            "flagged": "Loaded — matched prompt-injection pattern(s); review the file",
            "empty": "Not loaded — empty file", "unreadable": "Not loaded — could not be read",
            "suppressed": "Not loaded — cwd fell back to the Hermes install tree",
        }
        shown = file_rows[:20]
        parts.append(f"<table><thead>{row('File', 'Size', 'Estimate', 'Status', header=True)}</thead><tbody>")
        for item in shown:
            label = esc(item.get("label") or "Context file", 64)
            count = _fmt(max(0, _int_value(item.get("est_tokens"))))
            chars = max(0, _int_value(item.get("chars")))
            status = labels.get(str(item.get("status") or ""), "Unknown")
            parts.append(row(label, f"{_fmt(chars)} chars", f"~{count} tokens", esc(status)))
        parts.append("</tbody></table>")
        if len(shown) < len(file_rows):
            parts.append(f"<p>Showing {len(shown)} of {len(file_rows)} context files.</p>")
    elif files_available:
        parts.append("<p>No context files were found for the resident agent's routed profile.</p>")
    else:
        parts.append("<p>Inventory unavailable without the resident agent's routed profile and working directory; "
                     "no file list is inferred after restart.</p>")

    if expanded:
        toolsets = list((details or {}).get("toolsets") or [])
        skills = list((details or {}).get("skills") or [])
        parts.extend(["<h2>Toolsets and skills (/context all)</h2>",
                      "<p>Schema/index costs are local estimates; skill-file cost applies only when that skill is loaded.</p>"])
        if toolsets:
            shown = toolsets[:15]
            parts.append(f"<details><summary>Toolsets by schema cost ({len(toolsets)})</summary>")
            parts.append("<table><thead>" + row("Toolset", "Tools", "Schema estimate", header=True) + "</thead><tbody>")
            for item in shown:
                parts.append(row(esc(item.get("toolset") or "Unknown", 64), _fmt(_int_value(item.get("tool_count"))),
                                 f"~{_fmt(_int_value(item.get('schema_tokens')))} tokens"))
            parts.append("</tbody></table></details>")
            if len(shown) < len(toolsets):
                parts.append(f"<p>Showing {len(shown)} of {len(toolsets)} toolsets.</p>")
        elif details is not None:
            parts.append("<p>No active toolsets were reported for this session.</p>")
        else:
            parts.append("<p>Toolset breakdown unavailable without the resident agent's active tool schema.</p>")
        if skills:
            shown = skills[:15]
            parts.append(f"<details><summary>Skills by cost ({len(skills)})</summary>")
            parts.append("<table><thead>" + row("Skill", "Index", "SKILL.md", header=True) + "</thead><tbody>")
            for item in shown:
                md = item.get("skill_md_tokens")
                md_text = "n/a" if md is None else f"~{_fmt(_int_value(md))} tokens"
                parts.append(row(esc(item.get("name") or "Unnamed skill", 64),
                                 f"~{_fmt(_int_value(item.get('index_tokens')))} tokens", esc(md_text)))
            parts.append("</tbody></table></details>")
            if len(shown) < len(skills):
                parts.append(f"<p>Showing {len(shown)} of {len(skills)} skills.</p>")
        elif details is not None:
            parts.append("<p>No skill-index entries were reported for this session.</p>")
        else:
            parts.append("<p>Skill breakdown unavailable without the resident agent's resolved skill index.</p>")
    else:
        parts.append("<p>Send <code>/context all</code> for per-skill and per-toolset token estimates.</p>")

    rendered = "\n".join(parts)
    return rendered


def _agents_delegation_lines(d: dict) -> list[str]:
    """/agents rows for one background delegation. Live per-child activity comes from the
    registry's progress sampler: api calls, current tool, seconds since last activity."""
    goal = _clip(" ".join(str(d.get("goal") or "").split()), 70)
    status = d.get("status", "?")
    row = f"- `{d.get('delegation_id', '?')}` · {status}"
    quiet = d.get("stalled_after_quiet_seconds")
    if status == "stalling" and quiet is not None:
        row += t("gateway.agents.no_progress", seconds=f"{quiet:.0f}")
    elif status != "stalling" and d.get("seconds_since_progress", 0) >= 60:
        row += t("gateway.agents.quiet_for", seconds=f"{d['seconds_since_progress']:.0f}")
    if goal:
        row += f" · {goal}"
    lines = [row]
    for i, child in enumerate(d.get("children_activity") or []):
        if not isinstance(child, dict):
            continue
        tool = child.get("current_tool")
        doing = f"`{tool}`" if tool else t("gateway.agents.between_turns")
        part = t("gateway.agents.child_row", index=i + 1, calls=child.get("api_calls", "?"), doing=doing)
        idle = child.get("seconds_since_activity")
        lines.append(part + (t("gateway.agents.active_ago", seconds=f"{idle:.0f}") if idle is not None else ""))
    return lines


def _usage_agent_stats_lines(agent) -> list[str]:
    """/usage session block for a live agent: rate limits, token breakdown (matches the CLI),
    context window and compression count."""
    lines: list[str] = []
    rl_state = agent.get_rate_limit_state()
    if rl_state and rl_state.has_data:
        from agent.rate_limit_tracker import format_rate_limit_compact
        lines += [t("gateway.usage.rate_limits", state=format_rate_limit_compact(rl_state)), ""]
    lines += [
        t("gateway.usage.header_session"),
        t("gateway.usage.label_model", model=agent.model),
        t("gateway.usage.label_input_tokens", count=_fmt(_n(agent, "session_input_tokens"))),
        t("gateway.usage.label_output_tokens", count=_fmt(_n(agent, "session_output_tokens"))),
        t("gateway.usage.label_total", count=_fmt(agent.session_total_tokens)),
        t("gateway.usage.label_api_calls", count=agent.session_api_calls),
    ]
    ctx = agent.context_compressor
    if ctx.last_prompt_tokens > 0:
        pct = _pct(ctx.last_prompt_tokens, ctx.context_length)
        from agent.context_breakdown import context_display_source
        mark = "~" if context_display_source(ctx) != "provider_usage" else ""
        lines.append(t("gateway.usage.label_context", used=mark + _fmt(ctx.last_prompt_tokens),
                       total=_fmt(ctx.context_length), pct=f"{mark}{pct:.0f}"))
    if ctx.compression_count:
        lines.append(t("gateway.usage.label_compressions", count=ctx.compression_count))
    return lines


def _capped_rows(items: list, render) -> list[str]:
    """Render up to ``_LIST_CAP`` items via *render* (list of lines each) plus an overflow line."""
    lines: list[str] = []
    for item in items[:_LIST_CAP]:
        lines.extend(render(item))
    if len(items) > _LIST_CAP:
        lines.append(t("gateway.agents.more", count=len(items) - _LIST_CAP))
    return lines


class GatewayStatusCommandsMixin:
    """Read-only gateway introspection commands: /status, /context, /usage, /agents, /insights, /topup."""

    async def _handle_status_command(self, event: MessageEvent) -> str | None:
        """Handle /status command."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        # Keep the sentinel distinct: a starting/pending run is not a usable agent for
        # model/context display, but it still occupies the session slot.
        agent = self._running_agents.get(session_key)
        is_running = agent is not None and agent is not _AGENT_PENDING_SENTINEL
        # Pending /queue follow-ups (slot + overflow).
        adapter = self.adapters.get(source.platform) if source else None
        queue_depth = self._queue_depth(session_key, adapter=adapter)
        title, session_row, db_total_tokens, persisted_route = await self._status_session_db_facts(
            session_entry.session_id
        )
        # Prefer the live or cached agent (actual runtime route + context compressor); fall back
        # to an active /model override, then SessionDB metadata + last_prompt_tokens so /status
        # stays useful between turns. Rehydrate first so this precedence survives gateway restarts.
        status_agent = agent if is_running else self._cached_agent_for(session_key)
        self._rehydrate_session_model_override(session_key)
        active_override = self._session_model_override(session_key) or {}
        model_name, provider_name, context_used, context_total, route = _status_model_route(
            status_agent, active_override, persisted_route, session_row, session_entry
        )
        if not context_total and model_name:
            # Same resolver /context uses (off-loop: it can probe /models). A window the resolver only
            # invented (unknown model → DEFAULT_FALLBACK_CONTEXT) stays hidden rather than being shown
            # as a real limit; the occupancy-only line below is honest for that case.
            resolved = await self._resolve_route_context(source, model_name, route)
            if resolved is not None and resolved.context_source != "default":
                context_total = _int_value(resolved.context_length)

        fields = build_status_fields(
            session_entry.session_id, None, session_row, title=title, model=model_name, provider=provider_name,
            created=session_entry.created_at, last_activity=session_entry.updated_at,
            tokens=db_total_tokens, agent_running=is_running,
        )
        lines = [t("gateway.status.header"), "",
                 t("gateway.status.session_id", session_id=fields["session_id"])]
        if fields["title"]:
            lines.append(t("gateway.status.title", title=fields["title"]))
        lines += [t("gateway.status.created", timestamp=fields["created"]),
                  t("gateway.status.last_activity", timestamp=fields["last_activity"])]
        if fields["model"] and fields["provider"]:
            lines.append(t("gateway.status.model_provider", model=fields["model"], provider=fields["provider"]))
        elif fields["model"]:
            lines.append(t("gateway.status.model", model=fields["model"]))
        from agent.context_breakdown import context_display_source
        mark = "~" if context_display_source(getattr(status_agent, "context_compressor", None)) != "provider_usage" else ""
        if context_total:
            pct = min(100, round((context_used / context_total) * 100))
            lines.append(t("gateway.status.context", used=mark + _fmt(context_used), total=_fmt(context_total),
                           pct=f"{mark}{pct}"))
        elif context_used:
            lines.append(t("gateway.status.context_used", used=mark + _fmt(context_used)))
        state = t("gateway.status.state_yes") if fields["agent_running"] else t("gateway.status.state_no")
        lines += [t("gateway.status.tokens", tokens=fields["tokens"]),
                  t("gateway.status.agent_running", state=state)]
        if queue_depth:
            lines.append(t("gateway.status.queued", count=queue_depth))
        if source.platform == Platform.MATRIX:
            scope = getattr(self.adapters.get(Platform.MATRIX), "_matrix_session_scope",
                            os.getenv("MATRIX_SESSION_SCOPE", "auto"))
            lines += [
                "",
                t("gateway.status.matrix_scope_header"),
                t("gateway.status.matrix_scope_room", room=source.chat_name or source.chat_id),
                t("gateway.status.matrix_scope_room_id", room_id=source.chat_id),
                t("gateway.status.matrix_scope_thread", thread_id=source.thread_id or t("gateway.shared.none_value")),
                t("gateway.status.matrix_scope_mode", scope=scope),
                t("gateway.status.matrix_scope_key",
                  session_key=self._redact_matrix_session_key(session_key)),
            ]
        lines += ["", t("gateway.status.platforms", platforms=', '.join(p.value for p in self.adapters))]
        if source.platform == Platform.TELEGRAM:
            platform_names = [getattr(platform, "value", str(platform)) for platform in self.adapters]
            html = _telegram_status_tables(lines, platform_names)
            copies: list[tuple[str, str]] = []
            if fields["session_id"]:
                copies.append(("Copy session id", fields["session_id"]))
            if fields["model"]:
                copies.append(("Copy model", fields["model"]))
            if await self._send_telegram_command_card(
                event, title="Status", html=html,
                nav=("context", "usage", "sessions", "model"), copies=tuple(copies),
            ):
                return None
            return html
        return "\n".join(lines)

    async def _status_session_db_facts(self, session_id: str):
        """``(title, session_row, db_total_tokens, persisted_route)`` for /status; each fail-open.

        Token totals come from the SQLite session DB, not SessionStore: run_agent.py persists per-turn
        token deltas into sessions_db, never into SessionEntry (its total_tokens is always 0).
        """
        db = self._session_db
        if not db:
            return None, {}, 0, {}
        title = await _quiet(lambda: db.get_session_title(session_id))
        row = await _quiet(lambda: db.get_session(session_id))
        session_row = row if isinstance(row, dict) else {}
        db_total_tokens = sum(
            _int_value(session_row.get(k))
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
        )
        route = await _quiet(lambda: db.get_recent_session_model_route(session_id))
        return title, session_row, db_total_tokens, route if isinstance(route, dict) else {}

    @staticmethod
    def _redact_matrix_session_key(session_key: str) -> str:
        """Return a stable Matrix session-key fingerprint for shared room status."""
        digest = hashlib.sha256(str(session_key or "").encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"

    async def _handle_last_command(self, event: MessageEvent) -> str | None:
        """Handle /last — show the last thing the agent did in this session.

        The last completed tool call + its response (v1 shows internal content
        as recorded; secrets are redacted at record time). Works mid-turn too:
        a running tool is shown first, then the last completed call.
        """
        from agent.session_activity import format_last_tool_activity
        from gateway.run import _AGENT_PENDING_SENTINEL

        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        agent = self._running_agents.get(session_key)
        is_running = agent is not None and agent is not _AGENT_PENDING_SENTINEL
        status_agent = agent if is_running else self._cached_agent_for(session_key)
        summary: dict = {}
        if status_agent is not None and hasattr(status_agent, "get_activity_summary"):
            try:
                summary = status_agent.get_activity_summary() or {}
            except Exception:
                summary = {}
        current = summary.get("current_tool")
        block = format_last_tool_activity(summary.get("last_tool_activity"))
        lines: list[str] = []
        if current:
            lines.append(f"Currently running: `{current}`")
        if block is not None:
            lines.append(block)
        elif not lines:
            desc = str(summary.get("last_activity_desc") or "").strip()
            if desc:
                text = f"No tool calls yet in this session.\nLast activity: {desc}"
            else:
                text = "No activity recorded yet in this session — the agent hasn't run a tool call here."
            if await self._send_telegram_command_card(
                event, title="Last activity", text=text, nav=("status", "agents"),
            ):
                return None
            return text
        text = "\n\n".join(lines)
        if await self._send_telegram_command_card(
            event, title="Last activity", text=text, nav=("status", "agents"),
        ):
            return None
        return text

    async def _send_telegram_context_card(self, event: MessageEvent, **kwargs) -> str | None:
        """Send /context HTML as a native Telegram rich card; plain HTML otherwise."""
        html = _telegram_context_html(**kwargs)
        if await self._send_telegram_command_card(
            event, title="Context window", html=html, nav=("status", "usage", "model"),
        ):
            return None
        return html

    async def _handle_context_command(self, event: MessageEvent) -> str | None:
        """Handle /context — the deep context-window view (/status has the one-line summary).

        Gauge, auto-compression threshold/headroom, compression count + last savings, and cumulative
        throughput (labelled as throughput, NOT context size). Resolution: running agent -> cached
        agent -> SessionStore/SessionDB metadata -> transcript estimate. ``all`` adds listings.
        """
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        expanded = event.get_command_args().strip().lower() in {"all", "full", "details"}
        # Running agent first (mid-turn), then cached agent (between turns).
        agent = self._resident_agent_for(self._session_key_for_source(source)) or None
        ctx = getattr(agent, "context_compressor", None) if agent else None
        used, context_length, model_name = await self._resolve_context_figures(
            agent, ctx, session_entry, source
        )
        from agent.context_breakdown import context_display_source
        mark = "~" if context_display_source(ctx) != "provider_usage" else ""
        is_telegram = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None)) == "telegram"
        persisted_usage = None
        if is_telegram and self._session_db:
            row = await _quiet(lambda: self._session_db.get_session(session_entry.session_id))
            persisted_usage = row if isinstance(row, dict) else None
        # Gauge path: preserve the provenance of the selected occupancy figure.
        if used > 0 and context_length > 0:
            pct = _pct(used, context_length)
            filled = round(pct / 100 * 24)
            lines = [
                t("gateway.context.header"), "",
                t("gateway.context.model", model=model_name or "?"),
                t("gateway.context.window", total=_fmt(context_length)),
                t("gateway.context.in_use", used=mark + _fmt(used), total=_fmt(context_length), pct=f"{mark}{pct:.0f}"),
                t("gateway.context.bar", bar="█" * max(0, filled) + "░" * max(0, 24 - filled)),
                t("gateway.context.headroom", headroom=mark + _fmt(max(0, context_length - used))),
                "",
            ]
            # Full view — compression / throughput need the live agent.
            lines += _context_compressor_lines(agent, ctx, used) if ctx is not None else [
                t("gateway.context.detail_after_first")]
            # Per-category estimated breakdown (+ optional expanded listings). Same chars/4 engine
            # the desktop popover and /usage use; plain text (monospace isn't guaranteed on
            # messaging platforms). Fail-open: rendering errors never break /context.
            if is_telegram:
                breakdown_data = await asyncio.to_thread(self._context_breakdown_data, agent, source, expanded)
                return await self._send_telegram_context_card(event,
                    agent=agent, ctx=ctx, used=used, context_length=context_length,
                    model_name=model_name, estimated=bool(mark),
                    payload=breakdown_data.get("payload"), details=breakdown_data.get("details"),
                    files=breakdown_data.get("files"), expanded=expanded,
                    breakdown_error=breakdown_data.get("error"),
                    transcript_estimate=breakdown_data.get("transcript_estimate"),
                    persisted_usage=persisted_usage,
                    files_available=breakdown_data.get("files_available", False),
                )
            breakdown = await asyncio.to_thread(self._context_breakdown_block, agent, source, expanded) if agent else []
            return "\n".join(lines + ([""] + breakdown if breakdown else []))
        # Last resort: rough estimate from transcript
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            if is_telegram:
                return await self._send_telegram_context_card(event,
                    agent=agent, ctx=ctx, used=used, context_length=context_length,
                    model_name=model_name, estimated=True, persisted_usage=persisted_usage,
                    breakdown_error="Transcript history is unavailable; a transcript-only estimate cannot be calculated.",
                )
            return history_unreadable()
        if not history:
            no_data = t("gateway.context.no_data")
            if is_telegram:
                return await self._send_telegram_context_card(event,
                    agent=agent, ctx=ctx, used=used, context_length=context_length,
                    model_name=model_name, estimated=True, persisted_usage=persisted_usage,
                )
            return no_data
        approx, count = _transcript_estimate(history)
        if is_telegram:
            return await self._send_telegram_context_card(event,
                agent=agent, ctx=ctx, used=used, context_length=context_length, model_name=model_name,
                estimated=True, transcript_estimate=(approx, count), persisted_usage=persisted_usage,
                payload={"categories": [{"id": "conversation", "label": "Conversation transcript (rough)",
                                          "tokens": approx}]},
            )
        return "\n".join([
            t("gateway.context.header"), "",
            t("gateway.context.estimated", count=_fmt(approx), messages=count),
            t("gateway.context.detail_after_first"),
        ])

    async def _resolve_context_figures(self, agent, ctx, session_entry, source):
        """``(used, context_length, model_name)`` for /context: used = compressor -> SessionStore;
        model = agent -> SessionDB row; window = compressor -> gateway model route -> model metadata."""
        used = max(0, _n(ctx, "last_prompt_tokens")) or max(0, _int_value(getattr(session_entry, "last_prompt_tokens", 0)))
        context_length = _n(ctx, "context_length")
        model_name = _clean_str(getattr(agent, "model", "")) if agent is not None else ""
        if not model_name and self._session_db:
            row = await _quiet(lambda: self._session_db.get_session(session_entry.session_id))
            model_name = _clean_str(row.get("model", "")) if isinstance(row, dict) else ""
        if not context_length:
            resolved = await self._resolve_route_context(source, model_name)
            if resolved is not None:
                model_name = model_name or resolved.model
                context_length = _int_value(resolved.context_length)
        if not context_length and model_name:
            from agent.model_metadata import get_model_context_length
            context_length = _int_value(
                await _quiet(lambda: asyncio.to_thread(get_model_context_length, model_name))
            )
        return used, context_length, model_name

    async def _resolve_route_context(self, source, model_name: str, route: dict | None = None):
        """``_resolve_gateway_model_context`` for a non-resident session, run off the event loop inside
        the profile serving ``source`` (multiplex). Fail-open: None on any error."""
        from gateway.run import _profile_runtime_scope, _resolve_gateway_model_context

        def _resolve():
            if getattr(getattr(self, "config", None), "multiplex_profiles", False):
                with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                    return _resolve_gateway_model_context(model_name or None, route)
            return _resolve_gateway_model_context(model_name or None, route)
        return await _quiet(lambda: asyncio.to_thread(_resolve))

    async def _handle_agents_command(self, event: MessageEvent) -> str | None:
        """Handle /agents command - list active agents and running tasks."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        from tools.process_registry import format_uptime_short, process_registry
        now = time.time()
        current_session_key = self._session_key_for_source(event.source)
        running_started: dict = getattr(self, "_running_agents_ts", {}) or {}
        agent_rows: list[dict] = []
        for session_key, agent in (getattr(self, "_running_agents", {}) or {}).items():
            pending = agent is _AGENT_PENDING_SENTINEL
            agent_rows.append({
                "session_key": session_key,
                "elapsed": max(0, int(now - float(running_started.get(session_key, now)))),
                "state": t("gateway.agents.state_starting") if pending else t("gateway.agents.state_running"),
                "session_id": "" if pending else str(getattr(agent, "session_id", "") or ""),
                "model": "" if pending else str(getattr(agent, "model", "") or ""),
            })
        agent_rows.sort(key=lambda row: row["elapsed"], reverse=True)
        procs = _quiet_sync(process_registry.list_sessions, [])
        running_processes = [p for p in procs if p.get("status") == "running"]
        background_tasks = [task for task in (getattr(self, "_background_tasks", set()) or set())
                            if hasattr(task, "done") and not task.done()]

        # Background (async) delegations — delegate_task(background=true).
        # Live per-child activity comes from the registry's progress sampler (#51690): api calls, current
        # tool, seconds since last activity.
        from tools.async_delegation import list_async_delegations
        delegations = [d for d in _quiet_sync(list_async_delegations, [])
                       if d.get("status") in ("running", "stalling", "finalizing")]

        def _agent_row(idx_row):
            idx, row = idx_row
            current = t("gateway.agents.this_chat") if row["session_key"] == current_session_key else ""
            sid = f" · `{row['session_id']}`" if row["session_id"] else ""
            model = f" · `{row['model']}`" if row["model"] else ""
            return [f"{idx}. `{row['session_key']}` · {row['state']} · "
                    f"{format_uptime_short(row['elapsed'])}{sid}{model}{current}"]

        def _proc_row(proc):
            cmd = _clip(" ".join(str(proc.get("command", "")).split()), 90)
            return [f"- `{proc.get('session_id', '?')}` · "
                    f"{format_uptime_short(int(proc.get('uptime_seconds', 0)))} · `{cmd}`"]

        lines = [t("gateway.agents.header"), "", t("gateway.agents.active_agents", count=len(agent_rows))]
        lines += _capped_rows(list(enumerate(agent_rows, 1)), _agent_row)
        lines += ["", t("gateway.agents.running_processes", count=len(running_processes))]
        lines += _capped_rows(running_processes, _proc_row)
        lines += ["", t("gateway.agents.async_jobs", count=len(background_tasks))]
        if delegations:
            lines += ["", t("gateway.agents.background_delegations", count=len(delegations))]
            lines += _capped_rows(delegations, _agents_delegation_lines)
        if not (agent_rows or running_processes or background_tasks or delegations):
            lines += ["", t("gateway.agents.none")]
        text = "\n".join(lines)
        if await self._send_telegram_command_card(
            event, title="Agents", text=text, nav=("status", "swarm", "sessions"),
        ):
            return None
        return text

    async def _handle_topup_command(self, event: MessageEvent) -> str | None:
        """Handle /topup -- show the Nous balance and hand off to the portal. Does NOT charge, confirm,
        or track payment (that happens in the browser; the next /topup shows the new balance)."""
        from agent.account_usage import build_credits_view
        view = await _quiet(lambda: asyncio.to_thread(build_credits_view, markdown=True))
        if view is None or not view.logged_in:
            text = t("gateway.credits.not_logged_in")
            if await self._send_telegram_command_card(
                event, title="Top up", text=text, nav=("status", "usage"),
            ):
                return None
            return text
        # Drop the helper's 📈 header; we print our own. The portal URL leaves the
        # text entirely: on Telegram it becomes a URL button, never a bare link.
        lines = [t("gateway.topup.header")] + [ln for ln in view.balance_lines if not ln.lstrip().startswith("📈")]
        if view.identity_line:
            lines += ["", view.identity_line]
        links: list[tuple[str, str]] = []
        if view.topup_url:
            links.append(("Open billing portal", view.topup_url))
            lines += ["", t("gateway.topup.hint")]
        text = "\n".join(lines)
        if await self._send_telegram_command_card(
            event, title="Top up", text=text, nav=("status", "usage"), links=tuple(links),
        ):
            return None
        if view.topup_url:
            lines.insert(-1, t("gateway.topup.portal_link", url=view.topup_url))
            text = "\n".join(lines)
        return text

    def _context_breakdown_block(self, agent, source, expanded: bool) -> list[str]:
        """/context per-category block (plain text, chars/4 estimate, same engine as /usage).
        Runs in a thread; returns [] and never raises."""
        try:
            from agent.context_breakdown import compute_context_details, render_context_breakdown_lines
            try:
                payload = self._session_context_breakdown(agent, source)
            except TranscriptReadError:
                return [history_unreadable()]  # a read failure is not an empty transcript
            if not (payload.get("categories") or []):
                return []
            details = _quiet_sync(lambda: compute_context_details(agent), {"skills": [], "toolsets": []}) if expanded else None
            from agent.context_file_sources import context_file_sources_for_agent, render_context_file_lines
            file_lines = _quiet_sync(lambda: render_context_file_lines(context_file_sources_for_agent(agent)), [])
            return render_context_breakdown_lines(payload, details=details, grid=False) + ([""] + file_lines if file_lines else [])
        except Exception:
            return []

    def _context_breakdown_data(self, agent, source, expanded: bool) -> dict:
        """Return structured category, expanded detail, and file data.

        After a restart, only the conversation transcript is available for a local estimate;
        reconstruct no prompt, tool schema, skill list, or profile-specific file inventory.
        """
        try:
            from agent.context_breakdown import compute_context_details
            if agent is None:
                try:
                    session_id = self.session_store.get_or_create_session(source).session_id
                    history = self.session_store.load_transcript(session_id) or []
                except TranscriptReadError:
                    return {"error": "Transcript history is unavailable; a transcript-only estimate cannot be calculated."}
                except Exception:
                    history = []
                if not history:
                    return {"payload": None, "transcript_estimate": None, "files": [],
                            "files_available": False}
                estimate, count = _transcript_estimate(history)
                return {
                    "payload": {"categories": [{
                        "id": "conversation", "label": "Conversation transcript (rough)", "tokens": estimate,
                    }], "estimated_total": estimate},
                    "transcript_estimate": (estimate, count), "files": [], "files_available": False,
                }
            try:
                payload = self._session_context_breakdown(agent, source)
            except TranscriptReadError:
                return {"error": "Transcript history is unavailable; local category estimates are incomplete."}
            details = _quiet_sync(lambda: compute_context_details(agent), None) if expanded else None
            from agent.context_file_sources import context_file_sources_for_agent
            files = _quiet_sync(lambda: context_file_sources_for_agent(agent), None)
            return {"payload": payload, "details": details, "files": files or [],
                    "files_available": files is not None}
        except Exception:
            return {"error": "Local context estimates could not be calculated from the available session data."}

    def _session_context_breakdown(self, agent, source) -> dict:
        """Per-category context estimate (chars/4) for *agent* over the session transcript (sync).
        Raises ``TranscriptReadError`` (unreadable rows must not pass as an empty transcript)."""
        from agent.context_breakdown import compute_session_context_breakdown
        store = self.session_store
        try:
            history = store.load_transcript(store.get_or_create_session(source).session_id) or []
        except TranscriptReadError:
            raise
        except Exception:
            history = []
        return compute_session_context_breakdown(agent, history)

    def _context_breakdown_lines(self, agent, source) -> list[str]:
        """/usage per-category context breakdown (chars/4 estimate). Returns [] and never raises."""
        try:
            try:
                payload = self._session_context_breakdown(agent, source)
            except TranscriptReadError:
                return [history_unreadable()]
            categories = payload.get("categories") or []
            if not categories:
                return []
            total = payload.get("estimated_total") or 0
            out = [t("gateway.usage.breakdown_header")]
            for cat in categories:
                tokens = int(cat.get("tokens") or 0)
                if tokens <= 0:
                    continue
                cat_id = str(cat.get("id") or "")
                label = t(f"gateway.usage.breakdown_cat_{cat_id}")
                if label.endswith(f"breakdown_cat_{cat_id}"):  # missing key: t() echoes it back
                    label = str(cat.get("label") or cat_id)
                pct = round(tokens / total * 100) if total else 0
                out.append(t("gateway.usage.breakdown_line", label=label, count=_fmt(tokens), pct=f"~{pct}"))
            return out if len(out) > 1 else []
        except Exception:
            return []

    async def _handle_usage_command(self, event: MessageEvent) -> str | None:
        """Handle /usage -- token usage for the current session (live or cached agent) plus
        account/credit blocks; ``/usage reset [--force]`` redeems a banked Codex reset credit."""
        source = event.source

        async def _reply(text: str, nav: tuple[str, ...] = ("status", "context", "topup")) -> str | None:
            if await self._send_telegram_command_card(event, title="Usage", text=text, nav=nav):
                return None
            return text

        session_key = self._session_key_for_source(source)
        raw_args = event.get_command_args().strip()
        args = [a.lower() for a in raw_args.split()] if raw_args else []
        wants_reset = bool(args) and args[0] == "reset"
        if args and not wants_reset:
            return await _reply(t("gateway.usage.unknown_subcommand", args=raw_args), ("status",))

        # Running agent first (mid-turn), then cached agent (between turns).
        agent = self._resident_agent_for(session_key)

        # Provider/base_url/api_key for the account-usage fetch: live agent first, else persisted
        # billing data on the SessionDB row so `/usage` still returns account info between turns.
        provider, base_url, api_key = (
            getattr(agent, k, None) if agent else None for k in ("provider", "base_url", "api_key")
        )
        if not provider and getattr(self, "_session_db", None) is not None:
            provider, base_url = await self._persisted_billing_route(source)
        if not provider:
            # Fresh or evicted session with no persisted route (e.g. /usage right after login):
            # fall back to the configured provider, as /status does, so account limits such as
            # Codex subscription windows still render from on-disk credentials (#15167).
            provider = await _quiet(lambda: asyncio.to_thread(_configured_provider)) or None
        if wants_reset:
            if str(provider or "").strip().lower() != "openai-codex":
                return await _reply(t("gateway.usage.reset_wrong_provider"), ("status",))
            from agent.account_usage import redeem_codex_reset_credit
            result = await asyncio.to_thread(
                redeem_codex_reset_credit, base_url=base_url, api_key=api_key, force="--force" in args[1:],
            )
            return await _reply(result.message, ("status",))

        # Account usage off the event loop so slow provider APIs don't block the gateway;
        # failures are non-fatal (account_lines stays []).
        account_snapshot = provider and await _quiet(
            lambda: asyncio.to_thread(fetch_account_usage, provider, base_url=base_url, api_key=api_key)
        )
        account_lines = (
            render_account_usage_lines(account_snapshot, markdown=True) if account_snapshot else []
        )

        # Nous credits + monthly-grant gauge (shared with CLI/TUI). Gates on "a Nous account is
        # logged in" — NOT the inference provider — so a Nous user inferring elsewhere still sees
        # a balance. Fail-open: never break /usage.
        from agent.account_usage import nous_credits_lines
        credits_state = getattr(agent, "_credits_state", None) if agent else None
        credits_lines = await _quiet(
            lambda: asyncio.to_thread(nous_credits_lines, markdown=True, credits_state=credits_state),
            [],
        )
        # Window plans: a chat's /topup has no Keep going control, so point the setting at the Portal.
        from agent.plan_limits import portal_wording
        credits_lines = [portal_wording(line) for line in credits_lines]

        def _with_account_blocks(lines: list[str]) -> str:
            # Each block is preceded by a blank divider only when something precedes it.
            for block in (account_lines, credits_lines):
                if block:
                    if lines:
                        lines.append("")
                    lines.extend(block)
            return "\n".join(lines)
        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = _usage_agent_stats_lines(agent)
            # Per-category breakdown (chars/4 estimate, same engine as the desktop popover): prompt
            # / tools / skills / memory off the live agent, conversation from the transcript.
            breakdown_lines = await asyncio.to_thread(self._context_breakdown_lines, agent, source)
            if breakdown_lines:
                lines += [""] + breakdown_lines
            return await _reply(_with_account_blocks(lines))

        # No agent at all -- rough count from session history
        session_entry = await self.async_session_store.get_or_create_session(source)
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            return await _reply(history_unreadable())
        if history:
            approx, count = _transcript_estimate(history)
            return await _reply(_with_account_blocks([
                t("gateway.usage.header_session_info"),
                t("gateway.usage.label_messages", count=count),
                t("gateway.usage.label_estimated_context", count=_fmt(approx)),
                t("gateway.usage.detailed_after_first"),
            ]))
        if account_lines or credits_lines:
            return await _reply(_with_account_blocks([]))
        return await _reply(t("gateway.usage.no_data"))

    async def _persisted_billing_route(self, source):
        """``(provider, base_url)`` from the SessionDB row / most recent route when no agent is resident."""
        async def _rows():
            entry = await self.async_session_store.get_or_create_session(source)
            persisted = await self._session_db.get_session(entry.session_id) or {}
            route = await self._session_db.get_recent_session_model_route(entry.session_id)
            return persisted, route if isinstance(route, dict) else {}
        persisted, recent = await _quiet(_rows, ({}, {}))
        row = recent if recent.get("billing_provider") else persisted
        return row.get("billing_provider"), row.get("billing_base_url")

    async def _handle_insights_command(self, event: MessageEvent) -> str | None:
        """Handle /insights [N | --days N] [--source S] -- usage insights and analytics."""
        # Normalize Unicode dashes (Telegram/iOS auto-converts -- to em/en dash)
        args = re.sub(r'[\u2012\u2013\u2014\u2015](days|source)', r'--\1', event.get_command_args().strip())
        days, source = 30, None
        parts = args.split()
        i = 0
        while i < len(parts):
            flag, value = parts[i], parts[i + 1] if i + 1 < len(parts) else None
            if flag == "--days" and value is not None:
                try:
                    days = int(value)
                except ValueError:
                    text = t("gateway.insights.invalid_days", value=value)
                    if await self._send_telegram_command_card(
                        event, title="Insights", text=text, nav=("status", "usage"),
                    ):
                        return None
                    return text
                i += 2
            elif flag == "--source" and value is not None:
                source, i = value, i + 2
            else:
                days = int(flag) if flag.isdigit() else days
                i += 1
        try:
            from hermes_state_registry import acquire
            from agent.insights import InsightsEngine

            def _run_insights():
                db = acquire()
                try:
                    engine = InsightsEngine(db)
                    return engine.format_gateway(engine.generate(days=days, source=source))
                finally:
                    from hermes_state_registry import release_or_close
                    release_or_close(db)

            # Not a bare hop: ``SessionDB()`` resolves ``get_hermes_home()`` at call time, a
            # contextvar set by ``_profile_runtime_scope``; a default-executor hop starts with an
            # EMPTY context and would read the DEFAULT profile's state.db.
            text = await self._run_in_executor_with_context(_run_insights)
        except Exception as e:
            logger.error("Insights command error: %s", e, exc_info=True)
            text = t("gateway.insights.error", error=e)
        if await self._send_telegram_command_card(
            event, title="Insights", text=text, nav=("status", "usage"),
        ):
            return None
        return text
