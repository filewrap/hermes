"""Per-turn callback runner (progress/status/voice/run_sync) for the gateway agent turn.

``TurnRunner`` owns the per-turn callbacks ``GatewayRunner._run_agent_inner`` binds. ``gateway.run``
internals are imported lazily inside method bodies (import cycle), so ``patch("gateway.run.X")``
keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import queue
import re
import threading
import time
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from agent.i18n import t
from agent.interrupt_compat import _accepts_keyword
from agent.replay_cleanup import canonicalize_replay_history
from gateway.config import Platform
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.base_exec_approval import ea_default_reason_text
from gateway.turn_context import TurnContext
from hermes_cli.config import cfg_get
from hermes_cli.plugins_dispatch import stream_held
from utils import is_truthy_value

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Consecutive turns a session's persisted transcript may lag its live cached history before
# _load_turn_history escalates the lag line from WARNING to ERROR (#114266: 11 days of WARNING).
_TRANSCRIPT_LAG_ESCALATION_TURNS = 3

# Exact refusals retained for older adapters/connectors without destination preflight.
# Substring matching would also silence transient thread-resolution errors.
_CARD_DESTINATION_REFUSALS = {
    "No Slack thread target",
    "slack task_card requires a thread anchor",
    "slack task_card requires a thread anchor (Slack streams are thread replies)",
}

_TERMINAL_TOOL_NAMES = {"terminal", "execute_code"}
_TERMINAL_FAILURE_RE = re.compile(
    r"(?:\b(?:skipped|cancelled|canceled)\b.{0,100}\b(?:terminal|tool|execution)\b"
    r"|\b(?:terminal|tool|execution|command|process)\b.{0,100}\b(?:skipped|cancelled|canceled)\b"
    r"|\b(?:timeout|timed\s*out|ttl(?:\s+exceeded)?|time\s+limit(?:\s+exceeded)?|expired)\b)",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_HISTORY_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization)\b"
    r"([\"']?\s*[:=]\s*[\"']?|\s+)(?:(?:bearer|basic)\s+)?[^\s,;\"']+"
)


def _renders_exec_approval_buttons(adapter_cls: type) -> bool:
    """True when the adapter class renders native approval buttons. BasePlatformAdapter subclasses
    say so through ``supports_exec_approval_buttons``; anything else (test doubles, relay-style
    duck types) counts when it defines ``send_exec_approval`` itself."""
    probe = getattr(adapter_cls, "supports_exec_approval_buttons", None)
    if callable(probe) and issubclass(adapter_cls, BasePlatformAdapter):
        return bool(probe())
    return getattr(adapter_cls, "send_exec_approval", None) is not None


# Rendered on a native clarify card whose wait ended without a click (mirrors the notice the
# Slack click handler shows on a dead entry).
def _clarify_expired_notice() -> str:
    return t("gateway.clarify.expired")


# run_conversation result keys the gateway projection carries verbatim so a finite viewer's
# `-z --usage-file` ledger (hermes_cli/oneshot._USAGE_KEYS) reads what the in-process path did.
_LEDGER_PASSTHROUGH_KEYS = (
    "estimated_cost_usd", "cost_status", "cost_source", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_tokens", "provider", "turn_exit_reason", "service_tier",
)
# Beside ``failure_reason`` and ``error``: what ``agent.error_surface.build_error_surface_from_result`` reads,
# so the authority's ``message.complete`` carries the same error card the TUI path builds;
# ``usage_window_message`` also marks a window-plan refusal for the chat rewording.
_FAILURE_DESCRIPTOR_KEYS = ("free_tier", "failure_retryable", "failure_resets_at", "usage_window_message")


def _finite_viewer_turn() -> bool:
    from gateway.session_finite import finite_turn_required
    return finite_turn_required() is True


def _persisted_turn(agent, agent_history, result, compressions) -> dict | None:
    """``message.complete.persisted_turn`` for this turn, the receipt the TUI gateway's own turns
    publish: clients bind the streamed reply to its stored row instead of comparing words. The
    agent copies ``conversation_history`` into its message list, so an untouched pre-turn prefix
    is the same dicts (transcript rows here carry no ``_row_id`` to compare)."""
    from agent.persisted_turn_receipt import persisted_turn_receipt
    if not isinstance(result, dict):
        return None
    status = ("interrupted" if result.get("interrupted")
              else "error" if result.get("failed") or result.get("error") else "complete")
    return persisted_turn_receipt(
        result.get("messages"), getattr(agent, "_persist_user_message_idx", None), agent_history,
        result.get("final_response"), status,
        compression_unchanged=type(compressions) is int and compressions == getattr(
            getattr(agent, "context_compressor", None), "compression_count", None),
        prefix_row_matches=lambda before, after: before is after)


class _NoStreamConsumer(RuntimeError):
    """Raised by the delta callback when nothing consumed the text; ``_call_quietly`` turns it into
    'not delivered' so the agent's partial-delivery accounting matches what the user saw."""

_SEARCH_TASK_PROMPT = """For a `/search ...` request, do a real multi-round web search, not one query and an answer. Use the real agent tool loop: search with varied queries, inspect the tool results, open and read relevant sources, cross-check material claims against independent evidence, then run follow-up searches for gaps, contradictions, and missing angles until the question is actually answered with evidence. A response containing tool calls is an intermediate step, not completion: let the runner execute them, inspect their results, and make another model turn for the next search/extract/cross-check cycle. Do not stop after planning, one search call, or a tool error. If a provider returns an error or no useful results, inspect the error and try another available search route/query; if every route fails, say so plainly instead of presenting an error as a researched answer. Maintain a concise, query-specific task list with the `todo_list` tool for this multi-step search. Add tasks only for work you intend to perform, update them when work actually starts or completes, and mark a task completed only after its work is done. The Telegram progress card renders only the actual `todo_list` result; never invent UI tasks, statuses, phases, or percentages. Use `web_search` to find credible sources and `web_extract` to read and cross-check relevant pages. Return a concise, well-structured Markdown answer with direct source links and nearby numbered citations; use native Markdown tables, blockquotes, or other rich structures when they improve clarity, never put tables in fenced code blocks, and do not force a template. Use interactive controls only when the platform or a trusted tool actually provides them; do not invent dropdowns or buttons in plain text. Do not call `execute_code` or `terminal` solely to compose, format, or synthesize ordinary prose; write the answer directly. Do not claim to have read a source unless its content was retrieved. If the question clearly needs deeper investigation than a quick search can give, say so briefly and point to `/deepresearch <question>` for a full cited report. Keep all existing approval and security checks in force; this is an ordinary chat turn, not a guard-free research session."""

_DEEP_RESEARCH_TASK_PROMPT = """You are conducting a dedicated deep-research session that the user explicitly confirmed and configured. This is DEEP research: go far beyond a surface search. The user expects a comprehensive, evidence-grounded report, not a summary of the first page of results. Depth is the point: cover the subquestions, read widely, cross-check, investigate disagreements, and write a report that stands up to re-reading.

{depth_guidance}

Report size is an expectation, not a cap. When the topic warrants it, aim for a large, thorough report on the order of 200KB of substantive Markdown: full sections per subquestion, timelines, comparisons, worked evidence, dissenting views, gaps, and limitations. Never pad with repetition, never stop at a short summary because it feels tidy, and never treat a small file as done. If the topic is genuinely narrow, cover it completely and say so, rather than inventing length. A report that only scratches the surface is a failed session, even if it is well formatted.

Work in rounds, and do not declare the investigation finished until every round below has actually happened:

Round 1 — Scope and plan. Restate the question, split it into subquestions, state boundaries and assumptions. Your first action is a todo_list call adopting the pre-loaded task plan: confirm it, adjust it to this question, and keep it current for the whole session.
Round 2 — Broad search. Search widely with varied queries and angles, prioritizing credible primary sources and authoritative publications. Do not rely on one search provider, one query phrasing, or one cluster of results.
Round 3 — Read and ledger. Read the relevant source pages in full, from independent hostnames. Immediately after each successful source read, document its exact URL verbatim, source identity/hostname, page title (or explicitly unavailable), publication date when available, supported claims, disagreements, and uncertainty in research_notes.md.
Round 4 — Cross-check and fill gaps. Cross-check every material claim against independent evidence. Investigate meaningful contradictions with targeted follow-up research. When a subquestion has thin or one-sided evidence, search again with different terms, sources, and languages of inquiry until it is covered or honestly declared a gap.
Round 5 — Dissent and limits. Actively look for evidence that contradicts the emerging findings. Consider source provenance, date, relevance, and limitations. Do not force a consensus when the evidence does not support one.
Round 6 — Write. Write the finished, readable cited report to `report.md`. Use nearby numeric citations such as [1] and end with a References section containing exact source URLs and descriptive titles. Clearly distinguish well-supported findings from inference, and explicitly describe material disagreements, evidence gaps, source limitations, and residual uncertainty.
Round 7 — Verify. Read back both research_notes.md and report.md. Every successfully read URL must appear in the ledger, and nearby numbered report citations must map to exact read URLs in References, including at least two distinct successfully read hostnames. Fix what does not check out, then verify again. Never declare completion just because a bibliography or report file exists.

Use the real Hermes agent loop for the entire investigation: after each tool result, assess what it established, then make the next useful tool call until the evidence is cross-checked and the final report has been written. Do not stop after an initial model turn, a plan, one search, or the first file write. Tool-call responses are intermediate states, never the final answer: let the runner execute each batch, inspect the results, and continue in another model turn. After writing report.md, read it back and deliver the verified contents. On Telegram, keep progress, clarifications, follow-ups, and final report delivery in the existing research topic; a message in that topic continues the same session, not a new root-chat task.

Task lifecycle is mandatory: keep exactly one task in_progress while doing its relevant tool work so the runtime can attribute verification; after any failed/skipped tool call, successfully re-check the affected work before completing it. Create the plan before research, mark a task in_progress BEFORE doing its work, inspect the resulting evidence or artifact, then complete that task in a separate todo_list update. Never jump pending tasks directly to completed, batch-complete a backlog, or mark the whole plan done merely because files exist. Complete at most one task per update; do not make cosmetic start/complete calls back-to-back without doing and verifying the intervening work. Use small, meaningful tasks and update promptly at each boundary so occasional coalesced chat edits do not hide the investigation. Task elapsed runtime is measured by the runtime, not estimated by you; never supply invented timestamps or durations. A completed research task means that task's verified work is done, NOT that the session or final delivery succeeded. Do not create or complete a task claiming Telegram delivery: only the delivery callback can confirm that. Keep report writing and report verification as distinct tasks. If you stop or encounter a failure, leave unfinished tasks unfinished and explain the saved artifacts and missing final answer truthfully.

If a file tool rejects a path outside the private folder, treat it as a protective boundary, not a loss of all research capability: retry the intended in-session file using its session-relative name. Web-tool cache paths outside this session are NOT readable with read_file/search_files; inspect the returned source content or call web_extract on the original URL instead. Do not repeatedly attempt shared cache paths. Do not tell the user the entire workspace is restricted when only an invalid path was rejected. Never relax safeguards or access unrelated files to work around the boundary. Report a storage blocker only if the correctly scoped operation also fails.

Search broadly, prioritizing credible primary sources and authoritative publications. Read at least two relevant source pages from independent hostnames, and cite only pages whose contents were actually retrieved; treat two as the floor, not the target — a deep session reads many more; cross-check material claims against independent evidence and consider source provenance, date, relevance, and limitations. Investigate meaningful contradictions with targeted follow-up research; stop once material uncertainties are addressed rather than searching for reassurance. Immediately after each successful source read, document its exact URL verbatim, source identity/hostname, page title (or explicitly unavailable), publication date when available, supported claims, disagreements, and uncertainty in research_notes.md. A list of names such as Wikipedia, Britannica, or Museum is NOT a URL ledger. Search-result URLs alone are NOT successfully read sources. The runtime also maintains a marked observed-source section in these notes from actual retrieved content; preserve that section and add your findings separately, without inventing missing metadata or treating retrieval as proof of claim support. Before completion, read back both notes and report: every successfully read URL must appear in the ledger, and nearby numbered report citations must map to exact read URLs in References, including at least two distinct successfully read hostnames. Never declare completion just because a bibliography or report file exists.

Write the finished, readable cited report to `report.md` and return it in the chat. Use nearby numeric citations such as [1] and end with a References section containing exact source URLs and descriptive titles. Clearly distinguish well-supported findings from inference, and explicitly describe material disagreements, evidence gaps, source limitations, and residual uncertainty; do not force a consensus when the evidence does not support one.

The configured report format for this session is {report_format}. Always write report.md first and verify it. If the format is pdf or docx, generate that file from the verified report.md inside the session folder with the isolated shell (for example pandoc) as its own task, after report verification and before finishing; name it report.pdf or report.docx. If conversion tooling is unavailable, say so plainly in the session and leave the verified report.md as the deliverable; never claim a pdf or docx exists when it does not.

Use up to {max_turns} agent turns; do not stop early merely because the user is quiet, and do not burn turns on repetition either: every turn must either gather new evidence, verify something, or improve the report. If a clarification is non-essential, state a reasonable assumption and continue. Ask only when a missing choice materially changes the research outcome. All artifacts must stay in the verified private session directory {session_dir}. For file tools (for example, write_file, read_file, or patch tools), use only session-relative names such as research_notes.md and report.md; never pass `/workspace/...` or the host session path to a file tool because the file boundary correctly rejects absolute paths outside the private root. If using the isolated shell, /workspace is the session mount and may be used there.

Approval prompts are disabled for this session's scoped work by the owner's configuration: shell commands, file operations, and already-selected MCP tools inside the session run without approval stops, so an autonomous session never stalls waiting for a click in a background topic. That does not widen the boundary: stay inside the session folder, and never perform consequential external actions in a research session — no purchases, public posts, account or security changes, official submissions, or messages to anyone. Research is read-only toward the outside world plus file writes inside the session folder.

{custom_instructions}

Interim topic updates may use the telegram_client rich_message_send tool with in-body buttons, sparingly and only for milestones: a copy button carrying the session id, or buttons linking the key sources just read. Never use buttons to present the report itself, never send interim findings to the parent chat, and never let a button update substitute for ledgering a source or verifying the report. The Telegram progress card renders only the actual `todo_list` snapshot and measured tool counts; never invent UI tasks, statuses, phases, or percentages.

If a search/extract provider fails or returns no useful results, inspect the error and try another available search route or a revised query; if all available routes fail, document that limitation and do not claim the research is complete. If this session is a resume, read checkpoint.json, research_notes.md, and report.md first, continue only from that durable state, never restart from scratch or reset tasks, and inspect the previous failure before continuing the unfinished verification."""


class _ExecApprovalDeclined(RuntimeError):
    """The connector refused the approval card's destination.

    Raised (not returned) so it propagates out of `_approval_notify_sync` to
    `_await_gateway_decision`, whose notify-failure path drops the central
    approval queue entry and unblocks the waiting tool. A plain return
    suppressed the text fallback but left that entry pending.
    """


from gateway.run_turn_progress import GatewayTurnProgressMixin, _tool_lifecycle_payload, local_viewer_turn
from gateway.session_api_turn import api_runtime_locked
from gateway.session_execution import GatewaySessionAgentMixin


def _settle_api_runtime_lock(agent, result, captured):
    """A confirmed API model lock settles only on the runtime it named (main's
    ``_finish_turn_result``): the served runtime rides the usage, a mismatch fails the turn."""
    from gateway.session_api_turn import api_runtime_locked
    api = api_runtime_locked()
    if api is None or not isinstance(result, dict):
        return
    settings = api['settings']
    try:
        captured['usage']['runtime'] = api['adapter']._turn_runtime_metadata(
            agent, route=settings.get('route'), requested_runtime=settings.get('requested_runtime'),
            route_source=settings.get('route_source') or '', confirmed_runtime_lock=True)
    except RuntimeError as exc:
        result.update(final_response='', failed=True, completed=False, error=str(exc))


def _resolution_failure_reply(exc: Exception, platform: Platform) -> tuple[str, str, bool]:
    """``(chat text, failure_reason, retryable)`` for a model/credential resolution failure before the turn."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    from hermes_cli.runtime_provider_custom import LocalRuntimeUnavailableError
    from hermes_state_runtime import RuntimeStoreError
    auth_permanent = FailoverReason.auth_permanent.value
    if isinstance(exc, LocalRuntimeUnavailableError):
        # The chosen model is configured; its local server is down. Not a credential verdict, so
        # the client must not offer "Update API key" or sign-in: the sentence names the fix.
        return f"⚠️ {exc}", exc.error_code, exc.retryable
    if isinstance(exc, RuntimeStoreError) and exc.reason == 'launch_credentials_unavailable':
        # The session's launch key is gone (a restart revoked a CLI --api-key, a route key changed):
        # the shared refusal copy names /new, never a resend.
        from agent.turn_failure_copy import launch_credentials_copy
        return launch_credentials_copy(exc), 'launch_credentials_unavailable', False
    if isinstance(exc, RuntimeStoreError):
        # Session-policy refusals carry a stable reason code (e.g. a CLI launch key revoked by
        # daemon restart): keep it in the reply so clients can act on it, and do not suggest
        # /login — the profile's own credentials were never in play.
        return (f"⚠️ This session's launch credentials are no longer available ({exc.reason}), so this "
                "message wasn't processed. Start a new session from the CLI to bind them again.", auth_permanent, False)
    if is_rate_limited_auth_error(exc.__cause__):
        # Quota cap with valid credentials: /login cannot help; name the reset window (#89401).
        from gateway.run import _gateway_provider_error_reply
        return _gateway_provider_error_reply(str(exc)), auth_permanent, False
    if platform == Platform.LOCAL:
        # The local operator reads the terminal: the resolver's own sentence ("provider 'custom'
        # resolved without credentials ...") is the diagnosis; /login is not.
        return f"⚠️ {exc}", auth_permanent, False
    seen: set[int] = set()
    host = auth = False
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        host = host or isinstance(link, OSError)
        auth = auth or isinstance(link, AuthError)
        link = link.__cause__ or link.__context__
    if host and not auth:
        # A host-side failure (a CA bundle deleted under a long-lived process, an unreadable file)
        # with no credential verdict: /login cannot fix it, the gateway log can (#140344).
        # host_error sits on the gateway layer, so clients don't offer "Switch provider"; not retryable,
        # since a deleted bundle or unreadable store fails the same way until the host is fixed.
        return t("gateway.errors.generic_failed"), "host_error", False
    return t("gateway.errors.no_credentials"), auth_permanent, False


class TurnRunner(GatewayTurnProgressMixin, GatewaySessionAgentMixin):
    """Per-turn collaborator carrying ``GatewayRunner._run_agent_inner``'s tool-progress callbacks."""

    # ``None`` is a legitimate state: a turn with no owning session authority (messaging
    # adapters without a canonical session) publishes no execution events and takes no
    # controls snapshot. Class-level so every publish/snapshot seam can read it unconditionally.
    _approval_owner = None

    # Fork-added search/deep-research flags, defaulted at class level so the
    # seams that read them (progress publishing, checkpoint restore) work on
    # instances built without ``__init__`` (``TurnRunner.__new__`` in tests):
    # ``__init__`` overwrites them from the turn context when it runs.
    _search_task_requested = False
    _deep_research_requested = False
    _search_progress_enabled = False

    def __init__(self, runner: GatewayRunner, ctx: TurnContext) -> None:
        self._runner = runner
        self._ctx = ctx
        self._search_task_requested = bool(re.match(
            r"^\s*/search(?:@[A-Za-z0-9_]+)?\s+\S", str(getattr(ctx, "message", "") or ""), re.IGNORECASE,
        ))
        _deep_research = getattr(ctx, "deep_research", None)
        self._deep_research_requested = isinstance(_deep_research, dict) and bool(_deep_research.get("session_id"))
        self._task_progress_name = "Deep research" if self._deep_research_requested else "Search"
        _source = getattr(ctx, "source", None)
        self._search_progress_enabled = (
            (self._search_task_requested or self._deep_research_requested)
            and getattr(_source, "platform", None) == Platform.TELEGRAM
        )
        self.bind_deep_research_routing()
        self._search_progress_message_id: Optional[str] = None
        self._search_progress_scheduler = None
        self._search_progress_key: Optional[str] = None
        self._search_progress_last_submitted_text: Optional[str] = None
        self._search_progress_terminal = False
        self._deep_research_checkpoint_lock = asyncio.Lock()
        self._search_progress_requested = 0
        self._search_progress_rendered = 0
        self._search_progress_todos: Optional[list[dict[str, str]]] = None
        self._search_progress_todo_revision: Optional[int] = None
        self._search_progress_rendered_todos: Optional[list[dict[str, str]]] = None
        self._search_task_transitions: list[dict] = []
        self._search_progress_activity = "The agent turn started; waiting for its first tool result."
        self._search_progress_last_edit = 0.0
        self._search_progress_update_task: Optional[asyncio.Task] = None
        self._search_progress_status = "running"
        self._search_progress_rendered_status: Optional[str] = None
        self._search_progress_rendered_stats: Optional[tuple[int, int, int, int, int]] = None
        self._search_progress_rendered_activity: Optional[str] = None
        self._search_progress_result: Optional[dict] = None
        self._search_started_at: Optional[float] = None
        self._deep_research_tool_history: list[dict[str, Any]] = []
        self._deep_research_checkpoint_restored = False
        self._deep_research_source_update_seen: set[str] = set()
        self._deep_research_source_update_count = 0
        self._deep_research_source_update_futures: list[Any] = []
        self._search_metrics = {
            "searches": 0, "result_urls": set(), "sites": set(),
            "pages_read": set(), "read_sites": set(),
        }
        self._search_metrics_lock = threading.Lock()
        self._telegram_activity_lock = threading.Lock()
        self._telegram_activity_event_count = 0
        self._telegram_activity_terminal_queued = False
        self._telegram_activity_message_ids: dict[str, str] = {}

        from gateway.session_authorities import active_authority
        authority = active_authority(runner)
        if authority is not None:
            source = getattr(ctx, 'source', None)
            owner_id = (source.chat_id if getattr(source, 'platform', None) == Platform.LOCAL
                        else authority.logical_owner(ctx.session_id))
            if owner_id in authority.sessions:
                generation = authority.db.get_session(owner_id)["runtime_generation"]
                self._approval_owner = (authority, owner_id, generation)

    def bind_deep_research_routing(self) -> None:
        """Rebind after generic turn wiring; invocation reply anchors are not topic routes."""
        ctx = self._ctx
        if not self._deep_research_requested or ctx.source.platform != Platform.TELEGRAM:
            return
        from types import SimpleNamespace
        from gateway.platforms.base import _augment_telegram_task_metadata, _thread_metadata_for_source

        event_metadata = ctx.event_metadata if isinstance(ctx.event_metadata, dict) else {}
        event_metadata["deep_research"] = ctx.deep_research
        ctx.event_metadata = event_metadata
        # The source is already owner/chat/topic-bound by research admission. Never
        # derive its destination from the parent command or an arbitrary latest reply.
        topic_id = str(getattr(ctx.source, "thread_id", "") or "")
        anchor = (ctx.deep_research.get("latest_reply_message_id")
                  or ctx.deep_research.get("topic_welcome_message_id") or topic_id)
        event_metadata["telegram_task_reply_to_message_id"] = str(anchor) if anchor else None
        for attr in ("_status_thread_metadata", "_progress_metadata"):
            metadata = dict(getattr(ctx, attr, None) or {})
            for key in ("thread_id", "direct_messages_topic_id", "telegram_reply_to_message_id",
                        "telegram_dm_topic_reply_fallback", "reply_to_message_id"):
                metadata.pop(key, None)
            metadata.update(_thread_metadata_for_source(ctx.source) or {})
            metadata["thread_id"] = topic_id  # Empty fails closed in the strict adapter.
            metadata = _augment_telegram_task_metadata(
                SimpleNamespace(source=ctx.source, metadata=event_metadata), metadata,
            )
            metadata["telegram_reply_to_message_id"] = str(anchor) if anchor else None
            setattr(ctx, attr, metadata)
        ctx._progress_reply_to = str(anchor) if anchor else None

    # ── shared thread→loop plumbing ─────────────────────────────────────────────────────────

    def _schedule(self, coro, log_message: str, loop=None):
        """Hop a coroutine from the agent's sync worker thread onto the gateway loop."""
        from gateway.run import safe_schedule_threadsafe
        return safe_schedule_threadsafe(
            coro, self._ctx._loop_for_step if loop is None else loop, logger=logger, log_message=log_message,
        )

    def _agent_interrupted(self) -> bool:
        """True once the user sent `stop` (agent_holder[0] is the shared agent handle)."""
        try:
            agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
            return bool(agent is not None and getattr(agent, "is_interrupted", False))
        except Exception:
            return False

    def _stream_consumer(self):
        holder = self._ctx.stream_consumer_holder
        return holder[0] if holder else None

    def _drain_progress_queue(self) -> None:
        q = self._ctx.progress_queue
        with suppress(Exception):
            while not q.empty():
                q.get_nowait()

    def _track_progress_result(self, result) -> None:
        """Remember a delivered progress/status message id for end-of-turn cleanup."""
        ctx = self._ctx
        if getattr(ctx, "telegram_activity_trail_enabled", False):
            return  # the user's completed activity card is intentionally left visible
        if ctx._cleanup_progress and getattr(result, "success", False) and getattr(result, "message_id", None):
            ctx._cleanup_msg_ids.append(str(result.message_id))

    def _track_future_cleanup_id(self, fut) -> None:
        try:
            res = fut.result()
        except Exception:
            return
        self._track_progress_result(res)

    # ── progress_callback (agent thread → progress queue) ───────────────────────────────────

    def progress_callback(self, event_type: str, tool_name: str | None = None, preview: str | None = None, args: dict | None = None, **kwargs):
        """Callback invoked by agent on tool lifecycle events."""
        ctx = self._ctx
        if local_viewer_turn(self):
            try:
                from gateway import local_viewer_events
                local_viewer_events.publish_progress(self, event_type, tool_name, preview, kwargs)
            except Exception:
                # Best-effort: a viewer frame never blocks the failure notice below, but a lost one is logged.
                logger.debug("Local viewer progress publish failed for %s", event_type, exc_info=True)
        # Swarm observer: relay subagent/MoA lifecycle events to the Telegram
        # swarm cards. Runs before every progress-queue gate so in-turn children
        # are visible as they spawn, not only via the 15 s poll. Must never
        # raise into the turn.
        if event_type.startswith(("subagent.", "moa.")) or event_type == "subagent_progress":
            try:
                from gateway.swarm_observer import SwarmRoute, observer_for_runner

                source = ctx.source
                platform = getattr(getattr(source, "platform", None), "value", None) or str(
                    getattr(source, "platform", "") or "")
                if platform == "telegram":
                    route = SwarmRoute(
                        platform="telegram",
                        chat_id=str(source.chat_id),
                        chat_type=str(getattr(source, "chat_type", "dm") or "dm"),
                        thread_id=str(getattr(source, "thread_id", "") or "") or None,
                        user_id=str(getattr(source, "user_id", "") or "") or None,
                        session_key=str(ctx.session_key or ""),
                    )
                    observer_for_runner(self._runner).on_progress_event(
                        event_type, tool_name, preview, args, route=route,
                        run_key=f"moa:{ctx.session_key}:{ctx.run_generation or 0}",
                        **kwargs,
                    )
            except Exception:
                logger.debug("Swarm observer progress event failed", exc_info=True)
        # Failed subagent → one clean user-facing notice, handled FIRST, before every progress-queue
        # gate: platforms with tool_progress off must still hear about a dead delegation.
        # The private activity lane uses only ID-bearing lifecycle callbacks, not preview/args,
        # thinking scratch text, onboarding hints or profile-mode progress formatting.
        if getattr(ctx, "telegram_activity_trail_enabled", False):
            return
        if event_type == "subagent.complete":
            self._progress_subagent_notice(preview, kwargs)
            return
        self._progress_live_status(event_type, tool_name, args)
        # "log" mode: append tool.started lines to the log queue, silent in chat. Handled before
        # the progress_queue guard because log mode runs without a chat progress queue.
        if ctx.log_queue is not None and event_type == "tool.started" and tool_name and tool_name != "_thinking":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            preview_str = f' "{preview}"' if preview else ""
            ctx.log_queue.put(f"{ts}  {tool_name}:{preview_str}".rstrip())
        if getattr(self, "_search_progress_enabled", False):
            self._search_progress_event(event_type, tool_name, kwargs)
            return
        if not ctx.progress_queue or not ctx._run_still_current():
            return
        if event_type == "tool.completed" and not ctx.long_tool_hint_fired[0]:
            self._progress_onboarding_hint(kwargs)
            return
        # "_thinking" is assistant scratch text between tool calls, never ordinary tool progress:
        # only relayed when the platform explicitly opted into thinking_progress.
        if event_type == "_thinking" or tool_name == "_thinking":
            thinking_text = (preview if tool_name == "_thinking" else tool_name) if ctx._thinking_enabled else None
            if thinking_text:
                ctx.progress_queue.put(f"💬 {thinking_text}")
            return
        # Native task cards consume the ID-bearing tool_start/tool_complete callbacks instead;
        # name-correlated text events would duplicate cards and mispair concurrent same-tool calls.
        if ctx._native_slack_task_cards and event_type in {"tool.started", "tool.completed"}:
            return
        # tool_progress off → only _thinking passes (above). Only tool.started renders. clarify:
        # send_clarify IS the user-facing rendering (a bubble would duplicate it, and verbose mode
        # would dump the raw args JSON right under the prompt). Post-`stop`: N parallel tool calls
        # fire N tool.started events before the interrupt check, so a late stop must not render them.
        if (
            not ctx.tool_progress_enabled
            or event_type != "tool.started"
            # The adapter's send_clarify IS the user-facing rendering (interactive buttons or the
            # numbered-text fallback), so a progress bubble is pure duplication — and in verbose mode it
            # dumps the raw tool-call args JSON ({"question": ..., "choices": [...]}) into the chat. Because
            # the progress queue drains on a background task, that raw JSON typically lands right underneath
            # the rendered prompt (#52374).
            or tool_name == "clarify"
            or self._agent_interrupted()
        ):
            return
        # "new" mode: only report when tool changes
        if ctx.progress_mode == "new" and tool_name == ctx.last_tool[0]:
            return
        ctx.last_tool[0] = tool_name
        msg = self._progress_build_message(tool_name, preview, args)
        if msg is not None:
            self._progress_emit(msg)

    @property
    def search_progress_enabled(self) -> bool:
        return self._search_progress_enabled

    @staticmethod
    def _search_payload(value: Any) -> Optional[dict]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else None
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _search_url_identity(value: Any) -> Optional[tuple[str, str]]:
        """Return a fragment-free URL key and host for count-only search telemetry."""
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value.strip())
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return None
            host = parsed.hostname.lower().removeprefix("www.")
            key = urlunsplit((
                parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/",
                parsed.query, "",
            ))
            return key, host
        except (TypeError, ValueError):
            return None

    def _search_stats_snapshot(self) -> tuple[int, int, int, int, int]:
        stats = getattr(self, "_search_metrics", {})
        lock = getattr(self, "_search_metrics_lock", None)
        if lock is None:
            return (
                int(stats.get("searches", 0)), len(stats.get("result_urls", ())),
                len(stats.get("sites", ())), len(stats.get("pages_read", ())), len(stats.get("read_sites", ())),
            )
        with lock:
            return (
                int(stats.get("searches", 0)), len(stats.get("result_urls", ())),
                len(stats.get("sites", ())), len(stats.get("pages_read", ())), len(stats.get("read_sites", ())),
            )

    def _research_evidence_snapshot(self) -> dict[str, Any]:
        """Capture successful web-search/extract observations for this run."""
        stats = getattr(self, "_search_metrics", {})
        lock = getattr(self, "_search_metrics_lock", None)
        if lock is None:
            return {
                "searches": int(stats.get("searches", 0)),
                "result_urls": sorted(stats.get("result_urls", ())),
                "read_urls": sorted(stats.get("pages_read", ())),
                "read_sources": [dict(item) for item in stats.get("read_sources", {}).values()],
            }
        with lock:
            return {
                "searches": int(stats.get("searches", 0)),
                "result_urls": sorted(stats.get("result_urls", ())),
                "read_urls": sorted(stats.get("pages_read", ())),
                "read_sources": [dict(item) for item in stats.get("read_sources", {}).values()],
            }

    def _research_current_task(self) -> Optional[dict[str, str]]:
        """Return an actual saved task, preferring active work over the next pending item."""
        todos = self._search_progress_todos_snapshot() or []
        for status in ("in_progress", "pending"):
            for item in todos:
                if item.get("status") == status and item.get("content"):
                    return {"content": str(item["content"]), "status": status}
        return None

    @staticmethod
    def _safe_research_source_url(value: Any) -> Optional[str]:
        """Keep exact measured URLs, but refuse credentials or secret-bearing query keys."""
        identity = TurnRunner._search_url_identity(value)
        if not identity:
            return None
        try:
            parsed = urlsplit(str(value).strip())
            if parsed.username or parsed.password:
                return None
            from urllib.parse import parse_qsl
            sensitive = re.compile(r"(?:token|key|secret|password|auth|signature|credential)", re.IGNORECASE)
            if any(sensitive.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
                return None
            return str(value).strip()
        except (TypeError, ValueError):
            return None

    def _research_tool_activity(self, task: Optional[dict[str, str]], tool_name: str, outcome: str) -> str:
        task_text = task.get("content") if isinstance(task, dict) else None
        if task_text:
            task_text = " ".join(task_text.split())[:180].replace("`", "ˋ")
            return f"Task: {task_text} — `{tool_name}`: {outcome}"
        return f"`{tool_name}`: {outcome}"

    async def _send_deep_research_source_update(self, sources: list[dict[str, str]]) -> None:
        """Send a compact, bounded receipt only to the research topic, never its parent anchor."""
        ctx = self._ctx
        if (not self._deep_research_requested or ctx.source.platform != Platform.TELEGRAM
                or not ctx.source.chat_id or not ctx.source.thread_id or not ctx.source.user_id
                or not ctx.deep_research.get("session_id") or not ctx._run_still_current()
                or self._agent_interrupted()):
            return
        lock = getattr(self, "_deep_research_source_update_lock", None)
        if lock is None:
            lock = self._deep_research_source_update_lock = asyncio.Lock()
        async with lock:
            if not ctx._run_still_current() or self._agent_interrupted():
                return
            seen = getattr(self, "_deep_research_source_update_seen", None)
            if not isinstance(seen, set):
                seen = self._deep_research_source_update_seen = set()
            fresh = []
            for source in sources:
                url = self._safe_research_source_url(source.get("url"))
                if not url or url in seen:
                    continue
                seen.add(url)
                title = self._safe_tool_history_output(str(source.get("title") or "Title not returned"))
                title = re.sub(r"[\x00-\x1f\x7f]", " ", title)
                title = " ".join(title.split())[:140]
                # Avoid interpreting a page-controlled title as Telegram Markdown.
                for char, replacement in (("`", "ˋ"), ("[", "［"), ("]", "］"), ("<", "‹"), (">", "›")):
                    title = title.replace(char, replacement)
                fresh.append((title or "Title not returned", url))
            # Each message coalesces up to three pages; cap attempts per resumed turn.
            fresh = fresh[:3]
            attempts = getattr(self, "_deep_research_source_update_count", 0)
            if not fresh or attempts >= 6:
                return
            self._deep_research_source_update_count = attempts + 1  # Reserve before awaiting transport.
            topic_id = str(ctx.source.thread_id)
            task = self._research_current_task()
            task_line = f" while working on {task['content'][:100]}" if task else ""
            lines = [f"Deep research read {len(fresh)} source page(s){task_line}:"]
            lines.extend(f"- {title} — {url}" for title, url in fresh)
            content = "\n".join(lines)[:1200]
            event_metadata = ctx.event_metadata if isinstance(ctx.event_metadata, dict) else {}
            try:
                from gateway.run import _interim_metadata, _non_conversational_metadata
                adapter = self._runner._delivery_adapter_for(ctx.source)
                route = dict(getattr(ctx, "_status_thread_metadata", None) or {})
                for key in ("telegram_reply_to_message_id", "telegram_task_reply_to_message_id",
                            "reply_to_message_id", "telegram_dm_topic_reply_fallback"):
                    route.pop(key, None)
                metadata = _interim_metadata(_non_conversational_metadata(route, platform=ctx.source.platform))
                metadata = dict(metadata or {})
                metadata.update({
                    "telegram_strict_thread": True,
                    "deep_research_content": True,
                    "deep_research_status_card": False,
                    "thread_id": topic_id,
                    "telegram_reply_to_message_id": topic_id,
                    "telegram_task_reply_to_message_id": topic_id,
                    "deep_research_session_id": str(ctx.deep_research["session_id"]),
                    "deep_research_user_id": str(ctx.source.user_id),
                    "deep_research": ctx.deep_research,
                    # Deliberately bypass stale task-chain anchors; reply_to and thread_id
                    # are both the admitted research topic, while failure metadata stays shared.
                    "telegram_task_reply_chain": False,
                    "telegram_task_event_metadata": event_metadata,
                    "telegram_task_source": ctx.source,
                })
                result = await adapter.send(ctx.source.chat_id, content, reply_to=topic_id, metadata=metadata)
                if not getattr(result, "success", False) or not getattr(result, "message_id", None):
                    event_metadata["telegram_task_delivery_failure"] = str(
                        getattr(result, "error", None) or "Telegram did not confirm the research source receipt"
                    )[:300]
            except Exception as exc:
                event_metadata["telegram_task_delivery_failure"] = (
                    f"Research source receipt failed: {type(exc).__name__}"
                )[:300]
                logger.debug("Could not send topic-bound deep-research source receipt", exc_info=True)

    async def _drain_deep_research_source_updates(self) -> None:
        """Wait briefly for already-admitted receipts; never retry or spawn late work."""
        futures = list(getattr(self, "_deep_research_source_update_futures", []))
        self._deep_research_source_update_futures = []
        awaitables = []
        for future in futures:
            if future is None or not callable(getattr(future, "done", None)):
                continue
            if future.done():
                continue
            try:
                awaitables.append(asyncio.wrap_future(future))
            except (TypeError, RuntimeError):
                continue
        if not awaitables:
            return
        done, pending = await asyncio.wait(awaitables, timeout=15)
        if pending:
            metadata = self._ctx.event_metadata
            if isinstance(metadata, dict):
                metadata["telegram_task_delivery_failure"] = "Telegram source-receipt delivery did not finish before terminal status."
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for future in done:
            with suppress(asyncio.CancelledError, Exception):
                future.result()

    async def _restore_deep_research_checkpoint(self) -> None:
        """Seed resumed counters, tasks, activity, and card identity before first render."""
        if not self._deep_research_requested or getattr(self, "_deep_research_checkpoint_restored", False):
            return
        ctx = self._ctx
        checkpoint = ctx.deep_research.get("checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        if not checkpoint:
            try:
                from gateway.deep_research_sessions import load_checkpoint
                home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                home = home_resolver(ctx.source) if callable(home_resolver) else None
                if home is None:
                    from hermes_constants import get_hermes_home
                    home = get_hermes_home()
                checkpoint = await asyncio.to_thread(
                    load_checkpoint, home, ctx.source.user_id, ctx.deep_research["session_id"],
                )
            except Exception:
                logger.debug("Could not load DeepResearch checkpoint before progress card", exc_info=True)
                checkpoint = {}

        stats = getattr(self, "_search_metrics", None)
        if not isinstance(stats, dict):
            stats = self._search_metrics = {
                "searches": 0, "result_urls": set(), "sites": set(), "pages_read": set(), "read_sites": set(),
            }
        lock = getattr(self, "_search_metrics_lock", None)
        if lock is None:
            lock = self._search_metrics_lock = threading.Lock()
        evidence = checkpoint.get("research_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        try:
            searches = max(0, min(int(evidence.get("searches", 0) or 0), 1_000_000_000))
        except (TypeError, ValueError, OverflowError):
            searches = 0
        with lock:
            stats["searches"] = max(int(stats.get("searches", 0) or 0), searches)
            for evidence_key, urls_key, sites_key in (
                ("result_urls", "result_urls", "sites"), ("read_urls", "pages_read", "read_sites"),
            ):
                urls = stats.setdefault(urls_key, set())
                sites = stats.setdefault(sites_key, set())
                values = evidence.get(evidence_key)
                for value in values if isinstance(values, list) else ():
                    identity = self._search_url_identity(value)
                    if identity:
                        urls.add(identity[0])
                        sites.add(identity[1])

            from gateway.deep_research_sources import source_identity
            for item in evidence.get("read_sources", []):
                source = source_identity(item)
                if source and self._search_url_identity(source["url"])[0] in stats["pages_read"]:
                    stats.setdefault("read_sources", {})[source["url"]] = source

            parsed_todos = self._progress_todo_snapshot_from_result({
                "todos": checkpoint.get("todos", []),
                "revision": checkpoint.get("todo_revision", checkpoint.get("revision")),
            })
            # A fresh routing-only checkpoint contains todos=[] before the model
            # has planned anything. That is unknown state, not an actual empty plan.
            resumed = bool(ctx.deep_research.get("resumed"))
            if parsed_todos is not None and (resumed or parsed_todos[0] or parsed_todos[1] is not None):
                todos, revision = parsed_todos
                current_revision = getattr(self, "_search_progress_todo_revision", None)
                if current_revision is None or (revision is not None and revision >= current_revision):
                    self._search_progress_todos = todos
                    self._search_progress_todo_revision = revision

            history = checkpoint.get("tool_history")
            if isinstance(history, list):
                safe_history = []
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    safe_history.append({
                        "name": str(item.get("name") or "tool")[:80],
                        "success": item.get("success") is True,
                        "output_excerpt": str(item.get("output_excerpt") or item.get("output") or "")[:1500],
                        "truncated": bool(item.get("truncated", False)),
                    })
                self._deep_research_tool_history = safe_history

        saved_id = ctx.deep_research.get("progress_message_id") or checkpoint.get("progress_message_id")
        if saved_id:
            self._search_progress_message_id = str(saved_id)[:100]
        phase = str(checkpoint.get("phase") or "").strip().replace("_", " ")
        if not ctx.deep_research.get("resumed"):
            self._search_progress_activity = "Starting deep research; waiting for the agent's first tool result."
        else:
            # Neutral resume activity: never assert the prior failure is still
            # unresolved, name a task as current, or restate restored counters
            # as fresh activity before any new tool output exists. Counters
            # live in the Measured activity table; the last issue, if any, is
            # shown by the session status, not guessed onto the card.
            phase_note = f" (last saved phase: {phase[:60]})" if phase else ""
            self._search_progress_activity = (
                f"Resumed saved session{phase_note}. Waiting for the first new verified tool output in this run."
            )
        self._deep_research_checkpoint_restored = True

    @staticmethod
    def _tool_result_success(name: str, kwargs: dict) -> bool:
        """Reject errors and synthetic terminal skips/timeouts as task-work evidence."""
        if kwargs.get("is_error"):
            return False
        result = kwargs.get("result")
        payload = TurnRunner._search_payload(result)
        if isinstance(payload, dict):
            if payload.get("success") is False or payload.get("error"):
                return False
            for key in ("exit_code", "returncode", "return_code"):
                if key in payload and payload[key] not in (0, "0"):
                    return False
            outcome = str(payload.get("status") or payload.get("outcome") or "").strip().lower()
            if outcome in {"error", "failed", "failure", "skipped", "cancelled", "canceled", "timeout", "timed_out", "expired", "running", "pending", "queued"}:
                return False
        if str(name or "").lower().rsplit(".", 1)[-1] in _TERMINAL_TOOL_NAMES:
            if isinstance(payload, dict) and any(payload.get(key) in (0, "0") for key in ("exit_code", "returncode", "return_code")):
                return True
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            if _TERMINAL_FAILURE_RE.search(text[:12000]):
                return False
        return True

    @staticmethod
    def _safe_tool_history_output(result: Any) -> str:
        """Keep a bounded, credential-redacted excerpt, never tool arguments."""
        try:
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        text = _TOOL_HISTORY_SECRET_RE.sub(r"\1=[redacted]", text)
        text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
        return text[:1500]

    def _record_deep_research_tool_history(self, name: str, kwargs: dict) -> None:
        if not self._deep_research_requested:
            return
        history = getattr(self, "_deep_research_tool_history", None)
        if not isinstance(history, list):
            history = self._deep_research_tool_history = []
        output = self._safe_tool_history_output(kwargs.get("result"))
        history.append({
            "name": str(name or "tool")[:80], "success": self._tool_result_success(name, kwargs),
            "output_excerpt": output, "truncated": len(output) >= 1500,
        })

    @staticmethod
    def _progress_todo_snapshot_from_result(result: Any) -> Optional[tuple[list[dict[str, str]], Optional[int]]]:
        """Parse only the real revisioned todo_list result; never synthesize missing tasks."""
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                return None
        if not isinstance(result, dict) or not isinstance(result.get("todos"), list):
            return None
        valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
        todos: list[dict[str, str]] = []
        for item in result["todos"][:256]:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "").split())[:4000]
            status = str(item.get("status") or "").strip().lower()
            if not content or status not in valid_statuses:
                continue
            normalized = {
                "id": str(item.get("id") or "")[:80],
                "content": content,
                "status": status,
            }
            parent = str(item.get("parent") or "").strip()[:80]
            if parent:
                normalized["parent"] = parent
            # Only tool-result timing is consumed; never derive a task's runtime
            # from when a coalesced Telegram card happened to be rendered.
            elapsed = item.get("elapsed_seconds")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0:
                normalized["elapsed_seconds"] = elapsed
            for key in ("started_at", "completed_at"):
                value = item.get(key)
                if isinstance(value, str) and len(value) <= 64:
                    try:
                        datetime.fromisoformat(value)
                    except ValueError:
                        continue
                    normalized[key] = value
            if item.get("work_observed") is True:
                normalized["work_observed"] = True
            todos.append(normalized)
        try:
            revision = max(0, int(result["revision"])) if result.get("revision") is not None else None
        except (TypeError, ValueError):
            revision = None
        return todos, revision

    def _search_progress_todos_snapshot(self) -> Optional[list[dict[str, str]]]:
        lock = getattr(self, "_search_metrics_lock", None)
        if lock is None:
            todos = getattr(self, "_search_progress_todos", None)
            return None if todos is None else [dict(item) for item in todos]
        with lock:
            todos = getattr(self, "_search_progress_todos", None)
            return None if todos is None else [dict(item) for item in todos]

    def _record_search_tool_result(self, name: str, kwargs: dict) -> Optional[str]:
        if not self._tool_result_success(name, kwargs):
            return None
        payload = self._search_payload(kwargs.get("result"))
        if name in {"web_search", "web_search_preview", "search_web"}:
            data = payload.get("data") if isinstance(payload, dict) else None
            rows = data.get("web") if isinstance(data, dict) else None
            if not isinstance(rows, list) and isinstance(payload, dict):
                rows = payload.get("web") or payload.get("results")
            identities = []
            for item in rows if isinstance(rows, list) else ():
                identity = self._search_url_identity(item.get("url") or item.get("link")) if isinstance(item, dict) else None
                if identity:
                    identities.append(identity)
            with self._search_metrics_lock:
                self._search_metrics["searches"] += 1
                for key, host in identities:
                    self._search_metrics["result_urls"].add(key)
                    self._search_metrics["sites"].add(host)
            return f"Web search returned {len(rows) if isinstance(rows, list) else 0} result(s)."
        from gateway.deep_research_sources import successful_read_sources
        sources = successful_read_sources(name, payload)
        if sources:
            with self._search_metrics_lock:
                for source in sources:
                    key, host = self._search_url_identity(source["url"])
                    self._search_metrics["pages_read"].add(key)
                    self._search_metrics["read_sites"].add(host)
                    records = self._search_metrics.setdefault("read_sources", {})
                    if not source["title"]:
                        source["title"] = records.get(source["url"], {}).get("title", "")
                    records[source["url"]] = source
        if name in {"web_extract", "web_extract_urls", "fetch_url"}:
            return f"Read {len(sources)} source page(s)."
        if sources:
            return f"Read {len(sources)} browser source page(s)."
        return None

    def _search_answer_link_count(self) -> int:
        result = getattr(self, "_search_progress_result", None)
        answer = result.get("final_response") if isinstance(result, dict) else None
        if not isinstance(answer, str):
            return 0
        links = set()
        for raw in re.findall(r"https?://[^\s<>()\]]+", answer, flags=re.IGNORECASE):
            identity = self._search_url_identity(raw.rstrip(".,;:!?`"))
            if identity:
                links.add(identity[0])
        return len(links)

    def _search_model_label(self) -> str:
        result = getattr(self, "_search_progress_result", None)
        model = result.get("model") if isinstance(result, dict) else None
        if not model:
            try:
                agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
                model = getattr(agent, "model", None) if agent else None
            except Exception:
                model = None
        return " ".join(str(model or "Not reported").split())[:160].replace("`", "ˋ")

    def _search_elapsed_text(self) -> str:
        started = getattr(self, "_search_started_at", None)
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        return self._format_elapsed(elapsed)

    @staticmethod
    def _format_elapsed(elapsed: float) -> str:
        if elapsed < 60:
            return f"{elapsed:.1f}s"
        minutes, seconds = divmod(round(elapsed), 60)
        return f"{minutes}m {seconds:02d}s"

    def _search_progress_text(self, *, status: Optional[str] = None) -> str:
        """Render actual todo_list state and observed telemetry as native Rich Message Markdown."""
        status = status or getattr(self, "_search_progress_status", "running")
        title = "Deep research" if self._deep_research_requested else "Search"
        status_line = {
            "running": "**Status: Running**",
            "complete": "**Status: Complete**",
            "cancelled": "**Status: Cancelled**",
            "paused": "**Status: Paused**",
            "failed": "**Status: Failed**",
        }.get(status, "**Status: Running**")
        searches, result_urls, sites, pages, sites_read = self._search_stats_snapshot()
        todos = self._search_progress_todos_snapshot()
        total_tasks = len(todos or [])
        task_counts = {
            item_status: sum(1 for item in todos or [] if item.get("status") == item_status)
            for item_status in ("completed", "in_progress", "pending", "cancelled")
        }
        rows = [
            "| Measured activity | Count |",
            "|---|---:|",
        ]
        if todos is not None:
            rows.extend([
                f"| Agent tasks | {total_tasks} |",
                f"| Tasks completed | {task_counts['completed']} |",
                f"| Tasks in progress | {task_counts['in_progress']} |",
                f"| Tasks pending | {task_counts['pending']} |",
                f"| Tasks cancelled | {task_counts['cancelled']} |",
            ])
        rows.extend([
            f"| Successful web searches | {searches} |",
            f"| Unique result URLs | {result_urls} |",
            f"| Unique websites found | {sites} |",
            f"| Source pages read | {pages} |",
            f"| Unique websites read | {sites_read} |",
        ])
        terminal = status != "running"
        if terminal:
            rows.append(f"| Source links in answer | {self._search_answer_link_count()} |")
        lines = [
            f"## {title}", status_line, "",
            f"**Current activity:** {self._search_progress_activity}", "",
            "---", "", "## Agent tasks",
        ]
        if todos is None:
            lines.extend(["", "__No task list has been returned by the agent yet.__"])
        elif not todos:
            lines.extend(["", "__The agent's current task list is empty.__"])
        else:
            by_id = {item.get("id"): item for item in todos if item.get("id")}
            status_labels = {
                "completed": "Completed", "in_progress": "In progress",
                "pending": "Pending", "cancelled": "Cancelled",
            }
            for item in todos[:12]:
                item_status = item["status"]
                marker = "x" if item_status == "completed" else " "
                depth, parent, seen = 0, item.get("parent"), set()
                while parent and parent in by_id and parent not in seen:
                    seen.add(parent)
                    depth += 1
                    parent = by_id[parent].get("parent")
                label = " ".join(item["content"].split())[:180].replace("`", "ˋ")
                timing = ""
                if item_status == "completed" and self._deep_research_requested:
                    elapsed = item.get("elapsed_seconds")
                    timing = (f" (elapsed: {self._format_elapsed(elapsed)})" if elapsed is not None
                              else " (elapsed: not recorded)")
                lines.append(f"{'  ' * min(depth, 8)}- [{marker}] **{status_labels[item_status]}** — {label}{timing}")
            if total_tasks > 12:
                lines.append(f"\n__{total_tasks - 12} additional tasks are present in the agent's actual todo_list.__")
        if self._deep_research_requested:
            lines.extend(["", "__Task completion records verified task work, not successful final delivery.__"])
            lock = getattr(self, "_search_metrics_lock", None)
            with lock if lock is not None else suppress():
                transitions = list(getattr(self, "_search_task_transitions", []))
            if transitions:
                lines.extend(["", "### Recent task changes", "", "__Observed during this run; chat edits may be combined.__"])
                for change in transitions[-16:]:
                    elapsed = change.get("elapsed_seconds")
                    timing = (f"; elapsed: {self._format_elapsed(elapsed)}" if elapsed is not None
                              else "; elapsed: not recorded" if change["status"] == "completed" else "")
                    lines.append(f"- +{change['at']}: {change['content']} — {change['status'].replace('_', ' ')}{timing}")
        lines.extend(["", "---", "", "## Measured activity", *rows])
        if terminal:
            lines.extend([
                "", "---", "", f"**Model:** `{self._search_model_label()}`",
                f"**Elapsed:** {self._search_elapsed_text()}",
            ])
        else:
            lines.extend(["", "---", "", f"__To cancel this {self._task_progress_name.lower()} session, send `/stop`.__"])
        return "\n".join(lines)

    async def start_search_progress(self) -> None:
        """Register one persistent task card; UI failures never block the agent turn."""
        if not self._search_progress_enabled or not self._ctx._run_still_current():
            return
        # Task-change history is scoped to this run only: changes observed in
        # a previous run must never reappear as fresh activity after a resume
        # or restart. New changes append from the restored todo snapshot.
        self._search_task_transitions = []
        await self._restore_deep_research_checkpoint()
        ctx = self._ctx
        if getattr(self, "_search_started_at", None) is None:
            self._search_started_at = time.monotonic()
        try:
            from gateway.run import _interim_metadata, _non_conversational_metadata
            from gateway.telegram_progress_scheduler import scheduler_for
            adapter = self._runner._delivery_adapter_for(ctx.source)
            route_metadata = dict(ctx._status_thread_metadata or {})
            if self._deep_research_requested and ctx.source.platform == Platform.TELEGRAM:
                topic_id = getattr(ctx.source, "thread_id", None)
                if topic_id:
                    route_metadata["thread_id"] = str(topic_id)
                    if getattr(ctx.source, "chat_type", None) == "dm":
                        route_metadata["direct_messages_topic_id"] = str(topic_id)
            metadata = _interim_metadata(_non_conversational_metadata(
                route_metadata, platform=ctx.source.platform,
            ))
            metadata = dict(metadata or {})
            metadata["telegram_progress_card"] = True
            # The card is authored as native Markdown. Request it explicitly so
            # its inline code spans (`/stop`, the model label) do not flip the
            # auto-route to blocks; the adapter honors this on send.
            metadata["rich_format"] = "markdown"
            if self._deep_research_requested:
                metadata["telegram_strict_thread"] = True
                metadata["telegram_send_in_topic"] = True
                metadata["deep_research_status_card"] = True

            def _current_anchor() -> Optional[str]:
                if self._deep_research_requested:
                    research = ctx.deep_research or {}
                    if getattr(ctx.source, "chat_type", None) == "dm":
                        # Threaded DMs echo neither reply_to_top_id nor a
                        # forum flag, so the strict-topic edit gate only
                        # verifies a card replying directly to the topic
                        # root. Anchoring to the latest reply or the welcome
                        # message leaves every card edit refused and the
                        # scheduler disables the card, freezing it forever.
                        thread_id = str(getattr(ctx.source, "thread_id", "") or "")
                        if thread_id:
                            return thread_id
                    return (str(research.get("latest_reply_message_id"))
                            if research.get("latest_reply_message_id") else
                            str(research.get("topic_welcome_message_id"))
                            if research.get("topic_welcome_message_id") else
                            str(getattr(ctx.source, "thread_id", "")) or None)
                event_metadata = ctx.event_metadata if isinstance(ctx.event_metadata, dict) else {}
                anchor = (event_metadata.get("telegram_task_reply_to_message_id")
                          or ctx.inbound_message_id or ctx.event_message_id)
                return str(anchor) if anchor else None

            # The first card must retain the original slash-command/topic-welcome
            # anchor even if FloodWait delays its actual send until later activity.
            start_anchor = _current_anchor()
            saved_progress_message_id = self._search_progress_message_id

            async def _send_start(content: str):
                if saved_progress_message_id:
                    try:
                        edited = await adapter.edit_message(
                            ctx.source.chat_id, str(saved_progress_message_id), content,
                            metadata=metadata,
                        )
                        if getattr(edited, "success", False):
                            from types import SimpleNamespace
                            return SimpleNamespace(success=True, message_id=str(saved_progress_message_id))
                    except Exception:
                        logger.debug("Could not edit saved DeepResearch progress card; sending a replacement", exc_info=True)
                return await adapter.send(
                    ctx.source.chat_id, content, reply_to=start_anchor, metadata=metadata,
                )

            async def _edit_card(message_id: str, content: str):
                return await adapter.edit_message(
                    ctx.source.chat_id, message_id, content,
                    metadata=metadata,
                )

            async def _on_started(message_id: str) -> None:
                self._search_progress_message_id = str(message_id)
                self._search_progress_last_edit = time.monotonic()
                self._search_progress_rendered = self._search_progress_requested
                self._search_progress_rendered_status = self._search_progress_status
                self._search_progress_rendered_stats = self._search_stats_snapshot()
                self._search_progress_rendered_todos = self._search_progress_todos_snapshot()
                self._search_progress_rendered_activity = self._search_progress_activity
                if self._deep_research_requested:
                    research = ctx.deep_research
                    research["progress_message_id"] = str(message_id)
                    research["latest_reply_message_id"] = str(message_id)
                    try:
                        from gateway.deep_research_sessions import update_checkpoint_metadata, update_session
                        home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                        home = home_resolver(ctx.source) if callable(home_resolver) else None
                        if home is None:
                            from hermes_constants import get_hermes_home
                            home = get_hermes_home()
                        update_session(
                            home, ctx.source.user_id, research["session_id"],
                            progress_message_id=str(message_id), latest_reply_message_id=str(message_id),
                        )
                        update_checkpoint_metadata(
                            home, ctx.source.user_id, research["session_id"],
                            progress_message_id=str(message_id), latest_reply_message_id=str(message_id),
                        )
                    except Exception:
                        logger.warning("Could not save deep-research progress-message identity", exc_info=True)
                elif isinstance(ctx.event_metadata, dict):
                    ctx.event_metadata["telegram_task_reply_chain"] = True
                    ctx.event_metadata["telegram_task_reply_to_message_id"] = str(message_id)

            self._search_progress_scheduler = scheduler_for(adapter, ctx.source.chat_id)
            self._search_progress_key = (
                f"{ctx.source.chat_id}:{ctx.session_key or 'turn'}:{ctx.run_generation or 0}:{id(self)}"
            )
            start_text = self._search_progress_text()
            self._search_progress_last_submitted_text = start_text
            self._search_progress_message_id = await self._search_progress_scheduler.begin(
                self._search_progress_key, "deep_research" if self._deep_research_requested else "search",
                start_text, send_start=_send_start, edit=_edit_card, on_started=_on_started,
            ) or self._search_progress_message_id
        except Exception:
            logger.warning("Could not register Telegram task progress card; continuing the agent turn", exc_info=True)

    def _search_progress_event(self, event_type: str, tool_name: Optional[str], kwargs: dict) -> None:
        name = str(tool_name or "").lower().rsplit(".", 1)[-1]
        if event_type == "tool.completed" and self._deep_research_requested:
            self._record_deep_research_tool_history(name, kwargs)
        if name == "todo_list":
            if event_type == "tool.completed":
                parsed = None if kwargs.get("is_error") else self._progress_todo_snapshot_from_result(kwargs.get("result"))
                if parsed is not None:
                    todos, revision = parsed
                    with self._search_metrics_lock:
                        current_revision = self._search_progress_todo_revision
                        if revision is not None and current_revision is not None and revision < current_revision:
                            return
                        if self._deep_research_requested:
                            previous = {item['id']: item for item in self._search_progress_todos or []}
                            history = getattr(self, "_search_task_transitions", [])
                            for item in todos:
                                old = previous.get(item['id'])
                                if item['status'] != 'pending' and (old is None or old['status'] != item['status']):
                                    history.append({
                                        'content': item['content'][:140], 'status': item['status'],
                                        'at': self._search_elapsed_text(),
                                        'elapsed_seconds': item.get('elapsed_seconds'),
                                    })
                            self._search_task_transitions = history[-16:]
                        self._search_progress_todos = todos
                        if revision is not None:
                            self._search_progress_todo_revision = revision
                    self._schedule(
                        self._request_search_progress(activity="The agent updated its actual task list."),
                        "search todo update scheduling error",
                    )
                    if self._deep_research_requested:
                        self._schedule(
                            self._checkpoint_deep_research(phase="planning", todos=todos),
                            "deep-research todo checkpoint scheduling error",
                        )
            return
        if event_type == "tool.started":
            task = self._research_current_task()
            if name in {"web_search", "web_search_preview", "search_web"}:
                outcome = "searching the web"
            elif name in {"web_extract", "web_extract_urls", "fetch_url"}:
                outcome = "reading source pages"
            elif name in {"browser", "browser_navigate", "browser_open", "browser_read", "browser_snapshot"}:
                outcome = "inspecting a source in the browser"
            elif name in {"write_file", "patch_file", "apply_patch"}:
                outcome = "writing a research artifact"
            elif name in {"read_file", "search_files"}:
                outcome = "reading files in the session workspace"
            elif name in {"terminal", "execute_code"}:
                outcome = "using the session-scoped execution environment"
            else:
                return
            activity = (self._research_tool_activity(task, name, outcome) if self._deep_research_requested
                        else outcome[0].upper() + outcome[1:] + ".")
            self._schedule(self._request_search_progress(activity=activity), "search activity progress scheduling error")
            return
        if event_type != "tool.completed":
            return
        payload = self._search_payload(kwargs.get("result"))
        task = self._research_current_task()
        source_updates: list[dict[str, str]] = []
        if self._deep_research_requested:
            agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
            store = getattr(agent, "_todo_store", None)
            note_work = getattr(store, "note_work_result", None)
            successful = self._tool_result_success(name, kwargs)
            if callable(note_work):
                note_work(success=successful)
                snapshot = (self._progress_todo_snapshot_from_result(store.snapshot())
                            if callable(getattr(store, "snapshot", None)) else None)
                if snapshot is not None:
                    with self._search_metrics_lock:
                        self._search_progress_todos, self._search_progress_todo_revision = snapshot
        if not self._tool_result_success(name, kwargs):
            if (name in {"web_search", "web_search_preview", "search_web"}
                    and isinstance(payload, dict) and payload.get("success") is False):
                outcome = "returned an error; the agent is deciding how to continue"
            else:
                outcome = "failed or was skipped; its output was not verified"
        else:
            outcome = self._record_search_tool_result(name, kwargs)
            payload = self._search_payload(kwargs.get("result"))
            if (self._deep_research_requested and name in {"web_search", "web_search_preview", "search_web"}
                    and isinstance(payload, dict)):
                rows = payload.get("web") or payload.get("results")
                if not isinstance(rows, list) and isinstance(payload.get("data"), dict):
                    rows = payload["data"].get("web")
                if isinstance(rows, list):
                    hosts = set()
                    for item in rows:
                        url = item.get("url") or item.get("link") if isinstance(item, dict) else None
                        identity = self._search_url_identity(url)
                        if identity:
                            hosts.add(identity[1])
                    outcome = f"returned {len(rows)} result(s) from {len(hosts)} distinct site(s)"
            if self._deep_research_requested:
                from gateway.deep_research_sources import successful_read_sources
                source_updates = successful_read_sources(name, payload)
                if source_updates:
                    shown = []
                    for source in source_updates[:2]:
                        url = self._safe_research_source_url(source.get("url")) or "URL withheld (unsafe or credential-bearing)"
                        title = " ".join(str(source.get("title") or "Title not returned").split())[:100]
                        shown.append(f"{title} — {url}")
                    suffix = f"; {len(source_updates) - len(shown)} more" if len(source_updates) > len(shown) else ""
                    outcome = f"read {len(source_updates)} source page(s): " + "; ".join(shown) + suffix
            if (outcome is None and name in {"web_search", "web_search_preview", "search_web"}
                    and isinstance(payload, dict) and payload.get("success") is False):
                outcome = "returned an error; the agent is deciding how to continue"
            if outcome is None and name in {
                "browser", "browser_navigate", "browser_open", "browser_read", "browser_snapshot",
                "write_file", "patch_file", "apply_patch", "read_file", "search_files",
                "terminal", "execute_code",
            }:
                outcome = "completed successfully" if self._deep_research_requested else f"Completed `{name}`."
        if outcome is not None:
            if self._deep_research_requested:
                activity = self._research_tool_activity(task, name or "tool", outcome)
            elif not self._tool_result_success(name, kwargs):
                activity = f"`{name or 'tool'}` {outcome}."
            else:
                activity = outcome
            self._schedule(self._request_search_progress(activity=activity), "search activity progress scheduling error")
        if source_updates and self._ctx._run_still_current() and not self._agent_interrupted():
            future = self._schedule(
                self._send_deep_research_source_update(source_updates),
                "deep-research source receipt scheduling error",
            )
            if future is not None and callable(getattr(future, "done", None)):
                self._deep_research_source_update_futures.append(future)
        if self._deep_research_requested:
            self._schedule(
                self._checkpoint_deep_research(phase=self._deep_research_phase(name), todos=self._search_progress_todos_snapshot()),
                "deep-research artifact checkpoint scheduling error",
            )

    @staticmethod
    def _deep_research_phase(tool_name: str) -> str:
        if tool_name in {"web_search", "web_search_preview", "search_web"}:
            return "sources"
        if tool_name in {"web_extract", "web_extract_urls", "fetch_url", "browser", "browser_navigate", "browser_open", "browser_read", "browser_snapshot"}:
            return "verification"
        if tool_name in {"write_file", "patch_file", "apply_patch", "terminal", "execute_code"}:
            return "artifacts"
        return "research"

    async def _checkpoint_deep_research(self, *, phase: str, todos=None) -> None:
        if not self._deep_research_requested:
            return
        ctx = self._ctx
        try:
            from gateway.deep_research_sessions import save_checkpoint
            home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
            home = home_resolver(ctx.source) if callable(home_resolver) else None
            if home is None:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
            options = {"research_evidence": self._research_evidence_snapshot(),
                       "tool_history": list(getattr(self, "_deep_research_tool_history", []))}
            if todos is not None:
                options["todos"] = todos
            lock = getattr(self, "_deep_research_checkpoint_lock", None)
            if lock is None:
                lock = self._deep_research_checkpoint_lock = asyncio.Lock()
            async with lock:
                if getattr(self, "_search_progress_terminal", False):
                    return
                await asyncio.to_thread(
                    save_checkpoint, home, ctx.source.user_id, ctx.deep_research["session_id"],
                    phase=phase, status="running", **options,
                )
        except Exception:
            logger.debug("Could not checkpoint DeepResearch work", exc_info=True)

    async def _request_search_progress(
        self, *, activity: Optional[str] = None, state: Optional[str] = None,
        result: Optional[dict] = None, terminal: bool = False,
    ) -> None:
        scheduler = self._search_progress_scheduler
        key = self._search_progress_key
        if scheduler is None or not key or (self._search_progress_terminal and not terminal):
            return
        changed = False
        if activity is not None and activity != self._search_progress_activity:
            self._search_progress_activity = activity
            changed = True
        if state is not None and state != self._search_progress_status:
            self._search_progress_status = state
            changed = True
        if result is not None and result is not self._search_progress_result:
            self._search_progress_result = result
            changed = True
        if self._search_stats_snapshot() != self._search_progress_rendered_stats:
            changed = True
        if self._search_progress_todos_snapshot() != self._search_progress_rendered_todos:
            changed = True
        if not changed and not terminal:
            return
        self._search_progress_requested += 1
        content = self._search_progress_text(status=self._search_progress_status)
        if content == self._search_progress_last_submitted_text and not terminal:
            return
        self._search_progress_last_submitted_text = content
        scheduler.update(key, content, terminal=terminal)

    @staticmethod
    def _deep_research_failure(result: Optional[dict]) -> tuple[str, str]:
        """Describe a terminal turn failure without making another model call."""
        result = result if isinstance(result, dict) else {}
        reason = str(result.get("failure_reason") or "").strip().lower()
        error = str(result.get("error") or "").strip()
        error_lower = error.lower()
        if reason == "strict_free_policy":
            failure_class = "strict_free_policy"
            message = "Strict free-only policy blocked this request before it was sent. Review the request and free-only configuration, then resume this session. No alternate provider, model, or paid fallback was attempted."
        elif reason == "saved_state_mismatch":
            failure_class = "saved_state_mismatch"
            message = str(result.get("error") or "Saved research state disagrees; please review the discrepancy before continuing.")[:500]
        elif reason == "provider_unavailable":
            failure_class = "provider_unavailable"
            message = "The selected AI provider is temporarily unavailable. Choose another configured provider, then resume this session."
        elif result.get("billing_block") or reason in {"billing", "billing_unverified"} or "402" in error:
            failure_class = "provider_billing_or_quota"
            message = "The model provider reported a billing, credits, or entitlement problem. Resolve it or switch models, then resume this session."
        elif reason in {"auth", "auth_permanent"} or any(marker in error_lower for marker in (
            "invalid api key", "unauthorized", "authentication failed", "401",
        )):
            failure_class = "provider_authentication"
            message = "The model provider authentication or configuration failed. Refresh credentials or correct the provider settings, then resume this session."
        elif reason in {"model_configuration", "model_not_found", "format_error"}:
            failure_class = "model_configuration"
            message = "The selected model or provider configuration is not usable. Correct it, then resume this session."
        elif reason in {"rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout", "unknown"}:
            failure_class = "provider_retries_exhausted"
            message = "The model provider remained rate-limited or unavailable after bounded retries. Wait for its retry window or switch models, then resume this session."
        else:
            failure_class = reason or "research_turn_failed"
            detail = f" (reason: {reason})" if reason else ""
            message = f"The research turn ended before completion{detail}. Partial work is saved; resolve the issue and resume this session."
        return failure_class, message

    def _set_research_delivery_response(self, result: Optional[dict], text: str, *, blocked: bool) -> None:
        """Replace model-authored success claims on both delivery result holders."""
        targets = [result]
        holder = getattr(self._ctx, "result_holder", None)
        if holder:
            targets.append(holder[0])
        for target in targets:
            if isinstance(target, dict):
                target["final_response"] = text
                target["research_completion_blocked"] = blocked
                if blocked:
                    target["completed"] = False

    async def finish_search_progress(self, *, success: bool, result: Optional[dict] = None) -> None:
        if not self._search_progress_enabled:
            return
        if not self._deep_research_requested and (self._search_progress_scheduler is None or not self._search_progress_key):
            return
        if self._deep_research_requested:
            await self._drain_deep_research_source_updates()
        self._search_progress_terminal = True

        if self._deep_research_requested:
            try:
                from gateway.deep_research_sessions import (
                    load_checkpoint, load_session, resolve_session_dir, save_checkpoint,
                    update_checkpoint_metadata, update_session, validate_research_artifacts,
                )
                home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                home = home_resolver(self._ctx.source) if callable(home_resolver) else None
                if home is None:
                    from hermes_constants import get_hermes_home
                    home = get_hermes_home()
                session_id = self._ctx.deep_research["session_id"]
                current = load_session(home, self._ctx.source.user_id, session_id)
                interrupted = isinstance(result, dict) and bool(result.get("interrupted"))
                failed = isinstance(result, dict) and bool(result.get("failed"))
                incomplete = isinstance(result, dict) and result.get("completed") is False and not interrupted
                if interrupted:
                    status = "interrupted"
                    last_error = "Research was interrupted before completion. Partial work is saved and the session can be resumed."
                    failure_class = "interrupted"
                elif failed or not success or incomplete:
                    status = "paused"
                    failure_class, last_error = self._deep_research_failure(result)
                else:
                    status = "running"
                    failure_class = None
                    last_error = ""
                update_session(
                    home, self._ctx.source.user_id, session_id, status=status,
                    failure_class=failure_class, last_error=last_error,
                    final_response_status=("interrupted" if interrupted else "failed" if status == "paused" else "pending"),
                )
                todos = self._search_progress_todos_snapshot()
                agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
                store = getattr(agent, "_todo_store", None)
                if callable(getattr(store, "snapshot", None)):
                    snapshot = self._progress_todo_snapshot_from_result(store.snapshot())
                    if snapshot is not None:
                        todos = snapshot[0]
                        lock = getattr(self, "_search_metrics_lock", None)
                        with lock if lock is not None else suppress():
                            self._search_progress_todos = todos
                options = {} if todos is None else {"todos": todos}
                evidence = self._research_evidence_snapshot()
                lock = getattr(self, "_deep_research_checkpoint_lock", None)
                if lock is None:
                    lock = self._deep_research_checkpoint_lock = asyncio.Lock()
                async with lock:
                    await asyncio.to_thread(
                        save_checkpoint, home, self._ctx.source.user_id, session_id,
                        phase=current.get("phase") or "research", status=status,
                        last_error=last_error, latest_reply_message_id=current.get("latest_reply_message_id"),
                        research_evidence=evidence,
                        tool_history=list(getattr(self, "_deep_research_tool_history", [])), **options,
                    )
                # Saved files are not final delivery. Validate artifacts even on
                # a failed model turn so a paused run can truthfully say what survived.
                checkpoint = await asyncio.to_thread(
                    load_checkpoint, home, self._ctx.source.user_id, session_id,
                )
                if todos is None:
                    todos = checkpoint.get("todos") or []
                session_dir = await asyncio.to_thread(
                    resolve_session_dir, home, self._ctx.source.user_id, session_id,
                )
                valid, evidence_issue = await asyncio.to_thread(
                    validate_research_artifacts, session_dir, checkpoint.get("research_evidence"),
                )
                update_session(home, self._ctx.source.user_id, session_id,
                               artifact_status="ready" if valid else "invalid")
                update_checkpoint_metadata(home, self._ctx.source.user_id, session_id)
                if not valid and status == "running" and success:
                    status = "paused"
                    failure_class = "insufficient_research_evidence"
                    last_error = ("The final response did not meet the source-evidence checks: "
                                  f"{evidence_issue} Partial work is saved; resume to continue.")
                    update_session(home, self._ctx.source.user_id, session_id, status=status,
                                   failure_class=failure_class, last_error=last_error,
                                   final_response_status="failed")
                    await asyncio.to_thread(
                        save_checkpoint, home, self._ctx.source.user_id, session_id,
                        phase=current.get("phase") or "research", status=status,
                        last_error=last_error, latest_reply_message_id=current.get("latest_reply_message_id"),
                    )
                elif status == "running" and success and (not todos or any(
                    item.get("status") not in {"completed", "cancelled"} for item in todos
                )):
                    status = "paused"
                    last_error = "The report is saved, but the research task list still has unfinished work. Resume to verify and finish those tasks before final delivery."
                    update_session(home, self._ctx.source.user_id, session_id, status=status,
                                   failure_class="unfinished_research_tasks", last_error=last_error,
                                   final_response_status="failed")
                    await asyncio.to_thread(
                        save_checkpoint, home, self._ctx.source.user_id, session_id,
                        status=status, last_error=last_error,
                    )
                if status != "running":
                    safe_response = f"Deep research is {status}. {last_error} Research is not confirmed complete or delivered."
                else:
                    safe_response = "The report has passed source-evidence checks and its task ledger is finished. It is saved privately; final report delivery in this topic is still pending."
                self._set_research_delivery_response(result, safe_response, blocked=status != "running")
                if status != "running" and isinstance(self._ctx.event_metadata, dict):
                    # The pause is published as this turn's final response; the
                    # post-delivery completion callback must not re-notify it.
                    self._ctx.event_metadata["deep_research_terminal_pause_published"] = True
            except Exception:
                logger.warning("Could not persist DeepResearch terminal state", exc_info=True)
                # A persistence/validation failure must never publish the model's
                # optimistic completion claim or leave a delivery-ready status.
                safe_response = "Deep research is paused because its saved state or completion checks could not be verified. No completion or final delivery is confirmed; review the saved session before resuming."
                self._set_research_delivery_response(result, safe_response, blocked=True)
                if isinstance(self._ctx.event_metadata, dict):
                    self._ctx.event_metadata["deep_research_terminal_pause_published"] = True
                with suppress(Exception):
                    update_session(home, self._ctx.source.user_id, session_id, status="paused",
                                   failure_class="completion_validation_failed", last_error=safe_response,
                                   final_response_status="failed")

        async def _finalize_card() -> None:
            final_success = success
            final_result = result
            state = "complete" if final_success else "failed"
            activity = "The agent returned its final response." if final_success else "The agent run ended before a final answer or report."
            if self._deep_research_requested:
                try:
                    from gateway.deep_research_sessions import load_session
                    home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                    home = home_resolver(self._ctx.source) if callable(home_resolver) else None
                    if home is None:
                        from hermes_constants import get_hermes_home
                        home = get_hermes_home()
                    session = load_session(home, self._ctx.source.user_id, self._ctx.deep_research["session_id"])
                    if session.get("status") == "paused":
                        final_success = False
                        state = "paused"
                        activity = str(session.get("last_error") or "Partial work is saved; resolve the issue and resume this session.")
                    elif session.get("status") == "interrupted":
                        final_success = False
                        state = "cancelled"
                        activity = str(session.get("last_error") or "The agent run was interrupted; partial work is saved.")
                    elif session.get("status") in {"failed", "cancelled"}:
                        final_success = False
                        state = "failed" if session.get("status") == "failed" else "cancelled"
                        activity = str(session.get("last_error") or "The research run did not complete.")
                    elif session.get("status") != "completed" or session.get("final_response_status", "delivered") != "delivered":
                        final_success = False
                        state = "paused"
                        activity = "Final report delivery has not been confirmed. Saved task work is not a completed session."
                    else:
                        activity = "The verified report was delivered in the research topic."
                    artifact = session.get("artifact_status", "not_checked")
                    artifact_label = {"ready": "Verified report saved privately", "invalid": "Report missing or not yet verified"}.get(artifact, "Report not yet checked")
                    delivery = session.get("final_response_status", "delivered" if session.get("status") == "completed" else "pending")
                    activity += f" Artifacts: {artifact_label}. Final response: {delivery}."
                except Exception:
                    final_success = False
                    state = "paused"
                    activity = "Final delivery could not be verified; this session is not confirmed complete."
                    logger.debug("Could not read DeepResearch delivery status for progress card", exc_info=True)
            interrupted = isinstance(final_result, dict) and bool(final_result.get("interrupted"))
            if interrupted and state == "failed":
                state, activity = "cancelled", "The agent run was interrupted; partial work is saved."
            await self._request_search_progress(
                activity=activity, state=state, result=final_result, terminal=True,
            )

        adapter = self._runner._delivery_adapter_for(self._ctx.source)
        register = getattr(adapter, "register_post_delivery_callback", None)
        session_key = self._ctx.session_key
        if session_key and callable(register):
            register(session_key, _finalize_card, generation=self._ctx.run_generation)
            return
        await _finalize_card()

    def _progress_subagent_notice(self, preview, kwargs: dict) -> None:
        """Only terminal failure statuses render (same notice rail as credit warnings)."""
        ctx = self._ctx
        from gateway.warning_notifications import render_notification
        status = kwargs.get("status")
        try:
            from tools.delegate_tool import SUBAGENT_FAILURE_STATUSES, format_subagent_failure_line
            if status in SUBAGENT_FAILURE_STATUSES and ctx._run_still_current():
                line = format_subagent_failure_line(
                    kwargs.get("goal"), status, error=kwargs.get("summary") or preview,
                    duration_seconds=kwargs.get("duration_seconds"), failure_reason=kwargs.get("failure_reason"),
                )
                render_notification(
                    lambda: self._schedule(self._runner._deliver_platform_notice(ctx.source, line), "subagent failure notice scheduling error"),
                    platform=ctx.source.platform, user_config=ctx.user_config)
        except Exception:
            logger.debug("subagent failure notice failed", exc_info=True)

    def _progress_live_status(self, event_type: str, tool_name, args) -> None:
        """Live status line (Slack assistant status): stash the tool phrase on the adapter; the
        _keep_typing refresh renders it. Plain dict write, safe from the sync worker thread."""
        ctx = self._ctx
        adapter = ctx._live_status_adapter
        if adapter is None or ctx._live_status_mode == "off" or tool_name == "_thinking":
            return
        try:
            if event_type == "tool.started" and tool_name and ctx._run_still_current():
                from agent.display import build_status_phrase
                adapter.set_status_text(ctx.source.chat_id, build_status_phrase(tool_name, args if ctx._live_status_mode == "full" else None))
            elif event_type == "tool.completed":
                # Between tools the model is genuinely "thinking" again — revert to the static default.
                adapter.set_status_text(ctx.source.chat_id, None)
        except Exception as err:
            logger.debug("live status update failed: %s", err)

    def _progress_onboarding_hint(self, kwargs: dict) -> None:
        """First-touch onboarding: the first time a tool exceeds _LONG_TOOL_THRESHOLD_S while
        streaming every tool (progress_mode == "all"), append a one-time /verbose hint."""
        from gateway.run import _hermes_home, _load_gateway_config
        ctx = self._ctx
        try:
            if (kwargs.get("duration") or 0) >= ctx._LONG_TOOL_THRESHOLD_S and ctx.progress_mode == "all":
                from agent.onboarding import TOOL_PROGRESS_FLAG, is_seen, mark_seen, tool_progress_hint_gateway
                cfg = _load_gateway_config()
                gate_on = is_truthy_value(cfg_get(cfg, "display", "tool_progress_command"), default=False)
                if gate_on and not is_seen(cfg, TOOL_PROGRESS_FLAG):
                    ctx.long_tool_hint_fired[0] = True
                    ctx.progress_queue.put(tool_progress_hint_gateway())
                    mark_seen(_hermes_home / "config.yaml", TOOL_PROGRESS_FLAG)
        except Exception as err:
            logger.debug("tool-progress onboarding hint failed: %s", err)

    @staticmethod
    def _preview_cap() -> int:
        """tool_preview_length (default 40): the one-line preview budget for "all"/"new" modes."""
        from agent.display import get_tool_preview_max_len
        pl = get_tool_preview_max_len()
        return pl if pl > 0 else 40

    def _progress_terminal_blocks(self, adapter, tool_name, args, emoji):
        """(full, short) fenced blocks for a terminal command on markdown platforms, else (None, None).

        No language tag: Slack mrkdwn renders it as a literal first code line. Verbose shows the FULL
        command; "all"/"new" truncate to one line capped at ``tool_preview_length``. Consecutive
        terminal calls drop the repeated header so back-to-back commands render as adjacent blocks.
        """
        if not (
            getattr(adapter, "supports_code_blocks", False) and tool_name == "terminal" and isinstance(args, dict)
            and isinstance(args.get("command"), str) and args["command"].strip()
        ):
            return None, None
        cmd_full = args["command"].rstrip()
        header = "" if self._ctx.last_was_terminal_block[0] else f"{emoji} {tool_name}\n"
        cap = self._preview_cap()
        lines = cmd_full.splitlines()
        cmd_short = lines[0] if lines else cmd_full
        if len(cmd_short) > cap:
            cmd_short = cmd_short[:cap - 3] + "..."
        elif len(lines) > 1:
            cmd_short += " ..."
        return f"{header}```\n{cmd_full}\n```", f"{header}```\n{cmd_short}\n```"

    def _progress_emit(self, msg: str) -> None:
        """Dedup consecutive identical lines (execute_code boilerplate), then route to the native
        stream bubble when the consumer accepts tool progress, else the progress queue."""
        ctx = self._ctx
        sc = self._stream_consumer()
        native = sc is not None and getattr(sc, "accepts_tool_progress", False)
        if msg == ctx.last_progress_msg[0]:
            ctx.repeat_count[0] += 1
            if native:
                sc.on_tool_progress(f"{msg} (×{ctx.repeat_count[0] + 1})")
            else:
                ctx.progress_queue.put(("__dedup__", msg, ctx.repeat_count[0]))
            return
        ctx.last_progress_msg[0], ctx.repeat_count[0] = msg, 0
        if native:
            sc.on_tool_progress(msg)
        else:
            ctx.progress_queue.put(msg)

    # ── Slack-native task cards (progress-queue drain) ──────────────────────────────────────

    @dataclasses.dataclass
    class _TaskCardState:
        """Task-card rail state for ``_send_native_task_card_progress``."""
        adapter: Any
        tasks: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
        task_order: list[str] = dataclasses.field(default_factory=list)
        fallback_msg_id: Optional[str] = None
        native_failed: bool = False
        # TERMINAL for the turn, distinct from native_failed: no later publication
        # in this turn may deliver task text through the native lane OR the text
        # fallback. Two causes, both properties of the destination rather than of
        # one attempt: the connector's egress guard refused the chat, or the chat
        # cannot host a card (no thread anchor). Declared rather than set
        # dynamically so the state is visible where it lives.
        publication_suppressed: bool = False
        anonymous_seq: int = 0

        @staticmethod
        def _compact(value: Any, limit: int = 120) -> str:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

        def visible_tasks(self) -> list[dict[str, str]]:
            return [self.tasks[task_id] for task_id in self.task_order[-8:]]

        def fallback_text(self) -> str:
            labels = {"in_progress": "running", "complete": "complete", "error": "error"}
            lines = [f"- {t['title']} - {labels.get(t['status'], t['status'])}" for t in self.visible_tasks()]
            return "Hermes is working\n" + "\n".join(lines)

        def _upsert(self, call_id: str, title: str) -> dict[str, str]:
            if call_id not in self.tasks:
                self.task_order.append(call_id)
            self.tasks[call_id] = {"id": call_id, "title": self._compact(title), "status": "in_progress"}
            return self.tasks[call_id]

        def apply_event(self, raw: Any) -> bool:
            event_type = raw.get("type") if isinstance(raw, dict) else None
            if event_type not in {"tool.started", "tool.completed"}:
                return False
            call_id = str(raw.get("tool_call_id") or "")
            if not call_id:
                self.anonymous_seq += 1
                call_id = f"anonymous_{self.anonymous_seq}"
            tool_name = str(raw.get("tool_name") or "tool")
            if event_type == "tool.started":
                preview = self._compact(raw.get("preview"), 64)
                self._upsert(call_id, f"{tool_name} - {preview}" if preview else tool_name)
                return True
            # Completion-only events are rare but valid on some runtimes; keep their real ID instead
            # of guessing a same-name pending call.
            task = self.tasks.get(call_id) or self._upsert(call_id, tool_name)
            task["status"] = "error" if raw.get("is_error") else "complete"
            return True

    async def _task_card_send_or_edit_fallback(self, st) -> None:
        ctx = self._ctx
        text = st.fallback_text()
        from gateway.relay.egress import declined_send

        if st.publication_suppressed:
            return
        if st.fallback_msg_id:
            result = await st.adapter.edit_message(
                chat_id=ctx.source.chat_id, message_id=st.fallback_msg_id, content=text, metadata=ctx._progress_metadata,
            )
            if getattr(result, "success", False):
                return
            # P5(b): R5-4 made a declined native CARD terminal but left this
            # editable-text fallback: a declined edit fell through to
            # _send_progress_text and re-sent the same task text to the refused
            # chat. The decline must set the terminal state here too.
            if declined_send(result):
                logger.warning(
                    "Task-card fallback edit DECLINED by the connector's egress "
                    "guard; suppressing progress delivery for the rest of this "
                    "turn (the destination is not approved)"
                )
                st.publication_suppressed = True
                return
        result = await self._send_progress_text(st, text)
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            st.fallback_msg_id = str(result.message_id)

    async def _task_card_publish(self, st) -> None:
        ctx = self._ctx
        if not st.tasks:
            return
        if st.publication_suppressed:
            # Publication was suppressed earlier in the turn (egress refusal or a chat
            # that cannot host a card); every later publication would re-deliver the
            # same task text there.
            return
        # Resolve eligibility in the owning adapter BEFORE transport I/O: a first
        # timeout/disconnect must not turn an un-cardable chat into text fallback.
        # Optional for older adapters; only an explicit False refuses publication.
        destination_supported = getattr(st.adapter, "native_task_card_destination_supported", None)
        if callable(destination_supported) and destination_supported(
            ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        ) is False:
            self._task_card_uncardable_destination(st)
            if st.publication_suppressed:
                return
        if not st.native_failed:
            result = await st.adapter.send_native_task_card_progress(
                chat_id=ctx.source.chat_id, tasks=st.visible_tasks(), title="Hermes is working",
                reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata, fallback_text=st.fallback_text(),
            )
            if getattr(result, "success", False):
                return
            # P5(b): an AUTHORIZATION decline is not a broken card lane. The
            # fallback below sends the same task text to the same chat, which
            # turns a refused card into delivered plain text. Stop the lane
            # without re-delivering; the refusal is already logged.
            from gateway.relay.egress import declined_send

            if declined_send(result):
                # TERMINAL, and stored SEPARATELY from native_failed. Reusing
                # native_failed suppressed exactly ONE update: the next progress
                # event skipped this branch (the lane is already "failed") and
                # went straight to the text fallback. A refusal does not expire
                # after one tick.
                st.publication_suppressed = True
                st.native_failed = True
                logger.warning(
                    "Slack native task-card progress DECLINED by the connector's "
                    "egress guard — suppressing the text fallback for the rest "
                    "of this turn (the destination is not approved)"
                )
                return
            st.native_failed = True
            if getattr(result, "error", None) in _CARD_DESTINATION_REFUSALS:
                self._task_card_uncardable_destination(st, getattr(result, "error", "unknown error"))
                if st.publication_suppressed:
                    return
            else:
                logger.warning(
                    "Slack native task-card progress failed; falling back "
                    "to an editable text update: %s", getattr(result, "error", "unknown error"),
                )
        # Once the native rail fails, every later lifecycle event edits the same fallback message.
        await self._task_card_send_or_edit_fallback(st)

    def _task_card_uncardable_destination(self, st, reason: str = "no thread anchor") -> None:
        """The chat cannot host a card (flat DM: no thread anchor) — a property of the destination,
        not a transient outage. The text fallback exists to keep a WORKING card lane live through a
        transient failure, not to invent text bubbles the operator never asked for: with Slack's tier
        default (``tool_progress: off``) the lane goes silent for the turn. An operator who WROTE
        ``new``/``all`` asked for text progress, so the editable fallback carries it instead."""
        if self._ctx.tool_progress_enabled:
            st.native_failed = True
            logger.info(
                "Slack native task cards are unsupported for this destination (%s); "
                "tool progress continues as an editable text update", reason,
            )
            return
        st.publication_suppressed = True
        logger.info(
            "Slack native task cards are unsupported for this destination (%s); "
            "tool progress stays off for this turn", reason,
        )

    def _task_card_drain(self, st) -> bool:
        changed = False
        try:
            while True:
                changed = st.apply_event(self._ctx.progress_queue.get_nowait()) or changed
        except queue.Empty:
            pass
        except Exception:
            logger.debug("Slack native progress queue drain failed", exc_info=True)
        return changed

    async def _send_native_task_card_progress(self, adapter) -> None:
        """Drain progress into native cards; supported destinations retain editable fallback.
        Unsupported destinations and egress refusals suppress publication, never finalization.

        See #29483.
        """
        ctx = self._ctx
        st = self._TaskCardState(adapter)
        try:
            while ctx._run_still_current():
                try:
                    raw = ctx.progress_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue
                if not self._agent_interrupted() and st.apply_event(raw):
                    await self._task_card_publish(st)
        except asyncio.CancelledError:
            if self._task_card_drain(st) and ctx._run_still_current() and not self._agent_interrupted():
                await self._task_card_publish(st)
        finally:
            if hasattr(adapter, "stop_native_task_card_progress"):
                # Best-effort on the turn-cleanup path: an escaping transport exception would skip
                # final-delivery logic (cleanup awaits catch only CancelledError).
                try:
                    await adapter.stop_native_task_card_progress(
                        ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("task-card stop failed during turn cleanup", exc_info=True)

    # ── editable progress bubbles (progress-queue drain) ────────────────────────────────────

    @dataclasses.dataclass
    class _ProgressEditState:
        """Mutable editable-bubble state shared by ``send_progress_messages`` and its helpers."""
        adapter: Any
        progress_lines: list
        progress_msg_id: Any
        can_edit: bool
        _progress_len_fn: Any
        _PROGRESS_TEXT_LIMIT: int
        _edit_accepts_metadata: bool

    def _progress_edit_state(self, adapter) -> TurnRunner._ProgressEditState:
        ctx = self._ctx
        len_fn = adapter.message_len_fn if isinstance(adapter, BasePlatformAdapter) else len
        try:
            raw_limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000) or 4000)
        except Exception:
            raw_limit = 4000
        # Per-chat resolution (relay adapter fronting N platforms): cap and length unit follow the
        # chat's underlying platform; native adapters return their scalar/property unchanged.
        if isinstance(adapter, BasePlatformAdapter):
            with suppress(Exception):
                raw_limit = int(adapter.max_message_length_for_chat(ctx.source.chat_id) or 4000)
                len_fn = adapter.message_len_fn_for_chat(ctx.source.chat_id)
        return self._ProgressEditState(
            adapter=adapter, progress_lines=[], progress_msg_id=None,
            # "separate" = one msg per tool (pre-v0.9 behavior)
            can_edit=ctx.progress_grouping != "separate",
            _progress_len_fn=len_fn,
            # Leave room for platform quirks / formatting; tiny test adapters keep a usable limit.
            _PROGRESS_TEXT_LIMIT=max(1, raw_limit - (64 if raw_limit > 128 else 0)),
            # Overflow edits pass metadata (Telegram topic/thread routing) only when edit_message takes it.
            _edit_accepts_metadata=bool(ctx._progress_metadata) and _accepts_keyword(adapter.edit_message, "metadata"),
        )

    async def _edit_progress_message(self, st, message_id: str, content: str):
        ctx = self._ctx
        kwargs = {"chat_id": ctx.source.chat_id, "message_id": message_id, "content": content}
        if getattr(st.adapter, "REQUIRES_EDIT_FINALIZE", False):
            kwargs["finalize"] = True
        if st._edit_accepts_metadata:
            kwargs["metadata"] = ctx._progress_metadata
        return await st.adapter.edit_message(**kwargs)

    @staticmethod
    def _progress_text(lines: list) -> str:
        return "\n".join(str(line) for line in lines)

    def _split_progress_groups(self, st, lines: list) -> list[list]:
        """Partition progress lines into platform-sized editable bubbles."""
        groups: list[list] = []
        current: list = []
        for line in lines:
            candidate = current + [line]
            if current and st._progress_len_fn(self._progress_text(candidate)) > st._PROGRESS_TEXT_LIMIT:
                groups.append(current)
                candidate = [line]
            current = candidate
        return groups + ([current] if current else [])

    async def _send_progress_text(self, st, text: str):
        ctx = self._ctx
        result = await st.adapter.send(
            chat_id=ctx.source.chat_id, content=text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        )
        self._track_progress_result(result)
        return result

    async def _roll_progress_overflow_if_needed(self, st) -> bool:
        """Start fresh editable progress bubbles before a bubble exceeds limit.

        Returns True when it delivered/split the buffer or a transient edit failure left it
        intact for retry — either way the caller skips the normal send/edit path this tick.
        """
        if not st.progress_lines or not st.can_edit:
            return False
        groups = self._split_progress_groups(st, st.progress_lines)
        if len(groups) <= 1:
            return False
        if st.progress_msg_id is not None:
            result = await self._edit_progress_message(st, st.progress_msg_id, self._progress_text(groups[0]))
            if not result.success:
                if getattr(result, "retryable", False):
                    logger.debug("[%s] Transient overflow edit failure — keeping can_edit=True", st.adapter.name)
                    return True
                st.can_edit = False
                # Fall back to the existing non-edit behavior.
                return False
            groups = groups[1:]
        for group in groups:
            result = await self._send_progress_text(st, self._progress_text(group))
            if result.success and result.message_id:
                st.progress_msg_id = result.message_id
        # The newest continuation is the only mutable bubble: keep just its lines so later
        # edits update it instead of replaying the full transcript into new messages.
        st.progress_lines = groups[-1]
        return True

    @staticmethod
    def _is_reset_marker(raw) -> bool:
        return isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__"

    def _reset_progress_bubble(self, st) -> None:
        """Content bubble landed — close the tool-progress bubble so the next tool starts fresh
        below it; else tool edits hit the ORIGINAL message above (out of order)."""
        st.progress_msg_id, st.progress_lines = None, []
        self._ctx.last_progress_msg[0], self._ctx.repeat_count[0] = None, 0

    def _progress_absorb(self, st, raw) -> Any:
        """Fold a queue item into the bubble buffer; returns the line to render this tick."""
        if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
            _, base_msg, count = raw
            if not st.progress_lines:
                return base_msg
            st.progress_lines[-1] = f"{base_msg} (×{count + 1})"
            return st.progress_lines[-1]
        st.progress_lines.append(raw)
        return raw

    async def _flush_progress_edit(self, st) -> None:
        if st.can_edit and st.progress_lines and st.progress_msg_id:
            with suppress(Exception):
                await self._edit_progress_message(st, st.progress_msg_id, self._progress_text(st.progress_lines))

    async def _drain_progress_on_cancel(self, st) -> None:
        ctx = self._ctx
        with suppress(Exception):
            while not ctx.progress_queue.empty():
                raw = ctx.progress_queue.get_nowait()
                if self._is_reset_marker(raw):
                    # Content-bubble marker during drain: close the current progress bubble
                    # and start a fresh one for tool lines that arrived after.
                    await self._roll_progress_overflow_if_needed(st)
                    await self._flush_progress_edit(st)
                    self._reset_progress_bubble(st)
                else:
                    self._progress_absorb(st, raw)
                    await self._roll_progress_overflow_if_needed(st)
        # Final edit with all remaining tools (only if editing works)
        if st.can_edit and st.progress_lines and st.progress_msg_id:
            await self._roll_progress_overflow_if_needed(st)
        await self._flush_progress_edit(st)

    async def _progress_restore_typing(self, st) -> None:
        ctx = self._ctx
        await asyncio.sleep(0.3)
        if ctx._run_still_current():
            await st.adapter.send_typing(ctx.source.chat_id, metadata=ctx._progress_metadata)

    async def _progress_send_or_edit(self, st, msg) -> bool:
        """Deliver this tick's bubble. Returns False on a transient edit failure (retry next tick).

        Transient network errors (ConnectError, timeouts) must not disable editing; only permanent
        failures (not found, permissions) set can_edit=False. Flood control backs off but keeps editing.
        """
        if st.can_edit and st.progress_msg_id is not None:
            result = await self._edit_progress_message(st, st.progress_msg_id, "\n".join(st.progress_lines))
            if result.success:
                return True
            if getattr(result, "retryable", False):
                logger.debug("[%s] Transient edit failure — keeping can_edit=True", st.adapter.name)
                return False
            if any(w in (getattr(result, "error", "") or "").lower() for w in ("flood", "retry after")):
                logger.info("[%s] Progress edit flood control, backing off", st.adapter.name)
            else:
                st.can_edit = False
            await self._send_progress_text(st, msg)
            return True
        # First tool: send all accumulated text as a new message; editing unsupported: just this line.
        result = await self._send_progress_text(st, "\n".join(st.progress_lines) if st.can_edit else msg)
        if result.success and result.message_id:
            st.progress_msg_id = result.message_id
        return True

    async def _delete_telegram_activity_message(self, adapter, message_id: str) -> bool:
        delete = getattr(adapter, "delete_message", None)
        if not callable(delete):
            return False
        try:
            return bool(await delete(self._ctx.source.chat_id, message_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Telegram activity message cleanup failed (%s)", type(exc).__name__)
            return False

    async def _cleanup_telegram_activity_messages(self, adapter) -> None:
        """Best-effort final sweep for activity bubbles whose tool callback did not finish."""
        message_ids = list(self._telegram_activity_message_ids.values())
        for message_id in message_ids:
            with suppress(Exception):
                await self._delete_telegram_activity_message(adapter, message_id)
        self._telegram_activity_message_ids.clear()

    async def _consume_telegram_activity_queue(self, adapter) -> None:
        """Send one standalone MTProto RichMessage per tool start and delete it on completion."""
        ctx = self._ctx
        from gateway.telegram_activity import tool_activity_message

        try:
            while True:
                if not ctx._run_still_current() or self._agent_interrupted():
                    self._drain_progress_queue()
                    await self._cleanup_telegram_activity_messages(adapter)
                    return
                try:
                    event = ctx.progress_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.2)
                    continue
                if not isinstance(event, dict) or event.get("kind") != "telegram_activity":
                    continue
                if event.get("event") == "turn":
                    self._drain_progress_queue()
                    await self._cleanup_telegram_activity_messages(adapter)
                    return
                if event.get("event") != "tool":
                    continue
                call_id = str(event.get("call_id") or "")
                if event.get("status") == "running":
                    content = tool_activity_message(event)
                    if not content:
                        continue
                    metadata = dict(ctx._progress_metadata or {})
                    metadata["rich_format"] = "html"
                    try:
                        result = await adapter.send(
                            chat_id=ctx.source.chat_id,
                            content=content,
                            reply_to=ctx._progress_reply_to,
                            metadata=metadata,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug("Telegram activity send failed (%s)", type(exc).__name__)
                        continue
                    message_id = getattr(result, "message_id", None)
                    if getattr(result, "success", False) and message_id and call_id:
                        self._telegram_activity_message_ids[call_id] = str(message_id)
                elif event.get("status") in {"succeeded", "failed"} and call_id:
                    message_id = self._telegram_activity_message_ids.get(call_id)
                    if message_id and await self._delete_telegram_activity_message(adapter, message_id):
                        self._telegram_activity_message_ids.pop(call_id, None)
        except asyncio.CancelledError:
            await self._cleanup_telegram_activity_messages(adapter)
            raise

    async def send_progress_messages(self):
        ctx = self._ctx
        adapter = self._runner._delivery_adapter_for(ctx.source) if ctx.progress_queue else None
        if not adapter:
            return
        if ctx._native_slack_task_cards and hasattr(adapter, "send_native_task_card_progress"):
            await self._send_native_task_card_progress(adapter)
            return
        if getattr(ctx, "telegram_activity_trail_enabled", False):
            await self._consume_telegram_activity_queue(adapter)
            return
        # Skip tool progress for platforms that can't edit messages (e.g. iMessage/BlueBubbles):
        # each update would be a separate bubble. getattr, not attribute access: duck-typed
        # adapters (test fakes, minimal plugins) may lack edit_message — treated as "can't edit".
        adapter_edit = getattr(type(adapter), "edit_message", None)
        if adapter_edit is None or adapter_edit is BasePlatformAdapter.edit_message:
            self._drain_progress_queue()
            return
        st = self._progress_edit_state(adapter)
        last_edit_ts = 0.0
        EDIT_INTERVAL = 1.5  # Minimum seconds between edits (Telegram flood control)
        while True:
            try:
                if not ctx._run_still_current():
                    self._drain_progress_queue()
                    return
                raw = ctx.progress_queue.get_nowait()
                # Drain silently when interrupted: events queued in the window between tool parse
                # and interrupt processing should not render as bubbles.
                if self._agent_interrupted():
                    await asyncio.sleep(0)
                    continue
                if self._is_reset_marker(raw):
                    self._reset_progress_bubble(st)
                    continue
                msg = self._progress_absorb(st, raw)
                if not await self._roll_progress_overflow_if_needed(st):
                    # Throttle edits: batch rapid tool updates into fewer API calls (grammY pattern:
                    # proactively rate-limit rather than react to 429s). Loop back to drain further
                    # queued messages before sending a single batched edit.
                    remaining = EDIT_INTERVAL - (time.monotonic() - last_edit_ts)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        continue
                    if not ctx._run_still_current():
                        return
                    if not await self._progress_send_or_edit(st, msg):
                        continue
                last_edit_ts = time.monotonic()
                await self._progress_restore_typing(st)
            except queue.Empty:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                await self._drain_progress_on_cancel(st)
                return
            except Exception as e:
                logger.error("Progress message error: %s", e)
                await asyncio.sleep(1)

    # ── ID-bearing lifecycle callbacks (agent thread) ───────────────────────────────────────

    def voice_ack_callback(self, call_id, tool_name, args):
        """tool_start_callback: speak a one-time ack in the voice channel."""
        ctx = self._ctx
        if ctx._voice_ack_fired[0] or ctx._voice_ack_guild[0] is None or not ctx._run_still_current():
            return
        ctx._voice_ack_fired[0] = True
        adapter = self._runner.adapters.get(Platform.DISCORD)
        if adapter is None or not hasattr(adapter, "play_ack_in_voice"):
            return
        try:
            self._schedule(
                adapter.play_ack_in_voice(ctx._voice_ack_guild[0]), "voice ack scheduling error", loop=ctx._voice_ack_loop,
            )
        except Exception as err:
            logger.debug("voice ack schedule failed: %s", err)

    # Slack-native task cards ride agent.tool_start_callback / tool_complete_callback so start and
    # completion correlate by the REAL tool-call id; name-correlated progress_callback text events
    # would duplicate cards and mispair concurrent calls.

    def _native_card_gate(self) -> bool:
        ctx = self._ctx
        return bool(ctx.progress_queue) and ctx._run_still_current() and not self._agent_interrupted()

    # ── Slack-native task cards: ID-bearing lifecycle callbacks (#29483) ── These ride
    # agent.tool_start_callback / agent.tool_complete_callback so start/completion events correlate by the
    # REAL tool-call id — the name-correlated text events in progress_callback would duplicate cards and
    # mispair concurrent calls to the same tool.
    def native_tool_start_callback(self, call_id, tool_name, args):
        """Queue an ID-correlated native progress start from the agent thread."""
        if not self._native_card_gate():
            return
        from agent.display import build_tool_preview
        name = str(tool_name or "tool")
        self._ctx.progress_queue.put({
            "type": "tool.started", "tool_call_id": str(call_id or ""), "tool_name": name,
            "preview": build_tool_preview(name, args or {}, max_len=64) or "",
        })

    def native_tool_complete_callback(self, call_id, tool_name, args, result):
        """Queue the matching native completion using the real tool-call ID."""
        if getattr(self._ctx, "telegram_activity_trail_enabled", False):
            self.telegram_activity_tool_complete_callback(call_id, tool_name, args, result)
        if not self._ctx._native_slack_task_cards:
            return
        if not self._native_card_gate():
            return
        from agent.display import _detect_tool_failure
        name = str(tool_name or "tool")
        is_error, _ = _detect_tool_failure(name, result)
        self._ctx.progress_queue.put({
            "type": "tool.completed", "tool_call_id": str(call_id or ""), "tool_name": name, "is_error": bool(is_error),
        })

    def combined_tool_start_callback(self, call_id, tool_name, args):
        """Compose voice ack, Slack task-card, and personal Telegram activity consumers."""
        if local_viewer_turn(self):
            from gateway import local_viewer_events
            local_viewer_events.note_tool_start(self, call_id, tool_name, args)
        self._publish_execution("tool.start", _tool_lifecycle_payload(call_id, tool_name, args))
        self._publish_api_tool("tool.start", call_id, tool_name, args)
        if getattr(self._ctx, "telegram_activity_trail_enabled", False):
            self.telegram_activity_tool_start_callback(call_id, tool_name, args)
        if self._ctx._voice_ack_guild[0] is not None:
            self.voice_ack_callback(call_id, tool_name, args)
        if self._ctx._native_slack_task_cards:
            self.native_tool_start_callback(call_id, tool_name, args)

    def _telegram_activity_gate(self) -> bool:
        ctx = self._ctx
        return bool(
            getattr(ctx, "telegram_activity_trail_enabled", False) and ctx.progress_queue is not None
            and ctx.source.platform == Platform.TELEGRAM
            and getattr(ctx.source, "chat_type", None) == "dm"
            and str(getattr(ctx.source, "user_id", "") or "").isdigit()
            and ctx._run_still_current() and not self._agent_interrupted()
            and not ctx.mute_notification_reply
        )

    def telegram_activity_tool_start_callback(self, call_id, tool_name, args):
        """Queue a safe started event; tool args are deliberately never inspected or retained."""
        if not self._telegram_activity_gate() or tool_name == "clarify":
            return
        from gateway.telegram_activity import tool_activity_event
        event = tool_activity_event(call_id, tool_name, "running", args=args)
        if event is not None:
            self._queue_telegram_activity_event(event)

    def telegram_activity_tool_complete_callback(self, call_id, tool_name, args, result):
        """Queue a safe completion state using the existing tool-result failure classifier."""
        if not self._telegram_activity_gate() or tool_name == "clarify":
            return
        from agent.display import _detect_tool_failure
        from gateway.telegram_activity import tool_activity_event
        try:
            is_error, _reason = _detect_tool_failure(str(tool_name or "tool"), result)
        except Exception:
            is_error = True  # if outcome classification fails, do not claim success
        event = tool_activity_event(call_id, tool_name, "failed" if is_error else "succeeded")
        if event is not None:
            self._queue_telegram_activity_event(event)

    def _queue_telegram_activity_event(self, event) -> bool:
        ctx = self._ctx
        with self._telegram_activity_lock:
            if self._telegram_activity_terminal_queued:
                return False
            self._telegram_activity_event_count += 1
            ctx.progress_queue.put(event)
            return True

    def finish_activity_trail(self, response=None) -> None:
        """Append the turn's real terminal state; does not report transport delivery success."""
        ctx = self._ctx
        if not getattr(ctx, "telegram_activity_trail_enabled", False) or ctx.progress_queue is None:
            return
        if isinstance(response, dict) and response.get("interrupted"):
            status = "turn-interrupted"
        elif not isinstance(response, dict) or response.get("failed") or response.get("completed") is False:
            status = "turn-failed"
        else:
            status = "turn-complete"
        from gateway.telegram_activity import turn_activity_event
        event = turn_activity_event(status)
        if event is not None:
            with self._telegram_activity_lock:
                if self._telegram_activity_terminal_queued or not self._telegram_activity_event_count:
                    return
                self._telegram_activity_terminal_queued = True
                ctx.progress_queue.put(event)

    # ── hook / status bridges (agent thread → gateway loop) ────────────────────────────────

    def _event_callback_sync(self, event_type: str, context: dict) -> None:
        ctx = self._ctx
        try:
            asyncio.run_coroutine_threadsafe(ctx._hooks_ref.emit(event_type, context), ctx._loop_for_step)
        except Exception as e:
            logger.debug("event_callback hook error: %s", e)

    def _status_live(self) -> bool:
        """Status adapter present and this run is still the current generation."""
        return bool(self._ctx._status_adapter) and self._ctx._run_still_current()

    def _send_status_text(self, text: str, metadata, log_message: str) -> None:
        ctx = self._ctx
        self._schedule(ctx._status_adapter.send(ctx._status_chat_id, text, metadata=metadata), log_message)

    def _publish_execution(self, event_type, payload):
        if self._approval_owner is not None:
            authority, session_id, generation = self._approval_owner
            return authority.publish_execution(
                session_id, generation, event_type, self._stamp_segment(generation, event_type, payload))
        return False

    def _stamp_segment(self, generation, event_type, payload):
        """The turn's wire identity on its reply frames (tui_gateway/stream_segments.py), so a viewer
        upserts a re-sent delta or seal instead of painting a second copy (#144609). The same
        stamping rule as tui_gateway's ``_emit``, with one cursor per execution generation."""
        from tui_gateway.stream_segments import TurnSegments, stamp_reply_frame
        segments = getattr(self, "_segments", None)
        if segments is None or segments[0] != generation:
            segments = self._segments = (generation, TurnSegments())
        return stamp_reply_frame(segments[1], event_type, payload)

    def _publish_api_tool(self, event_type, call_id, tool_name, args, result=None):
        """The retained tool payload reaches the API observers of this exact admission only;
        the shared viewer stream keeps its ID-correlated frames."""
        if self._approval_owner is not None:
            from gateway.session_api_turn import publish_api_tool_event
            authority, session_id, generation = self._approval_owner
            publish_api_tool_event(authority, session_id, generation, event_type,
                                   str(call_id or ""), str(tool_name or "tool"), args, result)


    # ── stream consumer / interim commentary wiring ─────────────────────────────────────────

    def _setup_stream_consumer(self, platform_key):
        ctx = self._ctx
        if ctx.mute_notification_reply:
            return None, None, None, False
        stream_consumer = None
        if getattr(self, "_deep_research_requested", False):
            # Only measured progress is published during research. Model narration
            # can claim completion before the ledger/artifact/delivery gates run.
            return None, None, None, False
        # The streaming-TTS consumer is created on the outer loop thread before run_sync launches;
        # run_sync only reads it via the holder for delta-callback wiring.
        stts = ctx.streaming_tts_consumer_holder[0]
        scfg = getattr(getattr(self._runner, 'config', None), 'streaming', None)
        if scfg is None:
            from gateway.config import StreamingConfig
            scfg = StreamingConfig()
        # display.platforms.<plat>.streaming may disable streaming per platform; None = follow global.
        plat_streaming = ctx.resolve_display_setting(ctx.user_config, platform_key, "streaming")
        # A loaded ``hold_stream`` transform rewrites this session's reply before the user may see it:
        # no drafts, edits, native stream or commentary bubbles; the rewrite goes out as one plain
        # final while the typing indicator runs (the agent also withholds every delta).
        held = stream_held()
        want_stream_deltas = not held and not ctx.scheduled_heartbeat and scfg.enabled_for(plat_streaming)
        # A finite (`-z`, `chat -q`) viewer prints only the terminal reply, like the classic quiet
        # CLI that never wired commentary. Commentary it cannot see must not be recorded as a
        # delivery, or a final that arrives as commentary (codex app-server) suppresses the reply.
        want_interim_messages = (bool(ctx.interim_assistant_messages_enabled) and not ctx.scheduled_heartbeat
                                 and not held and not _finite_viewer_turn())
        if want_stream_deltas or want_interim_messages:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                adapter = self._runner._delivery_adapter_for(ctx.source)
                if adapter:
                    supports_incremental_stream = (
                        getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
                        or bool(getattr(adapter, "SUPPORTS_NATIVE_STREAMING", False))
                    )
                    consumer_stream_deltas = want_stream_deltas and supports_incremental_stream
                    consumer_cfg, pause_typing_before_finalize = self._runner._build_stream_consumer_config(
                        ctx.source, scfg, adapter,
                        # A complete commentary message needs no edit cursor.  Keeping it on the
                        # consumer records what reached non-editable platforms, so an interim
                        # callback carrying the final answer participates in final-send dedup.
                        on_missing_cursor="fallback" if want_interim_messages else "raise",
                    )
                    stream_consumer = GatewayStreamConsumer(
                        adapter=adapter, chat_id=ctx.source.chat_id, config=consumer_cfg,
                        metadata=ctx._status_thread_metadata,
                        on_new_message=(
                            (lambda: ctx.progress_queue.put(("__reset__",)))
                            if ctx.progress_queue is not None and not getattr(ctx, "telegram_activity_trail_enabled", False) else None
                        ),
                        on_before_finalize=pause_typing_before_finalize,
                        initial_reply_to_id=ctx.event_message_id, run_still_current=ctx._run_still_current,
                    )
                    ctx.stream_consumer_holder[0] = stream_consumer
                    # #105341: a consumer created only for interim commentary (text streaming off)
                    # is never fed the final reply's deltas — mark it so the duplicate-risk
                    # diagnostic in ``_run_agent_mark_streamed_delivery`` stays silent.
                    stream_consumer.stream_deltas_enabled = consumer_stream_deltas
            except Exception as err:
                logger.debug("Could not set up stream consumer: %s", err)
        # Deltas tee to the stream consumer (when text streaming is on) and to streaming TTS.
        delta_sinks = [
            sc for sc in (
                stream_consumer if stream_consumer and stream_consumer.stream_deltas_enabled else None,
                stts,
            ) if sc is not None
        ]
        stream_delta_cb = None
        local_viewer_route = getattr(ctx.source, "platform", None) == Platform.LOCAL
        if delta_sinks or self._approval_owner is not None:
            def stream_delta_cb(text: Optional[str]) -> None:
                if not ctx._run_still_current():
                    return
                delivered = bool(delta_sinks)
                if text is not None:
                    delivered = self._publish_execution("message.delta", {"text": text}) or delivered
                elif local_viewer_route:
                    # The segment boundary (main's ``_stream_delta(None)`` closes the reply box
                    # before tool lines); messaging routes only close their native stream.
                    self._publish_execution("message.delta", {"text": None})
                for sink in delta_sinks:
                    sink.on_delta(text)
                if text is not None and (not delivered or _finite_viewer_turn()):
                    # Nobody the user is looking at saw this text: no live viewer/observer/adapter
                    # sink, or a finite (`-z`, `chat -q`) viewer, which prints the terminal reply
                    # only. The agent must treat it as undelivered, else a stream that dies here is
                    # stitched as a "partial delivery" nobody received (classic -q parity).
                    raise _NoStreamConsumer()

        def interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
            if not ctx._run_still_current():
                return
            unseen = False
            if local_viewer_route and not already_streamed and str(text or "").strip():
                # LocalSessionAdapter.send publishes nothing, so unstreamed commentary handed to the
                # consumer would be recorded as delivered while no viewer saw it. A codex app-server
                # final that arrives with no deltas then suppresses the reply (message.complete "").
                # Publish the in-process TUI's message.interim contract instead, and count the text
                # as delivered only when a viewer or observer actually took it.
                payload = {"text": text, "already_streamed": False}
                unseen = not self._publish_execution("message.interim", payload)
            if stts is not None:
                # Flush accepted deltas; completed commentary is a separate speech segment.
                stts.on_delta(None)
                if not already_streamed:
                    stts.on_delta(text)
                    stts.on_delta(None)
            if unseen:
                return
            if stream_consumer is not None:
                stream_consumer.on_segment_break() if already_streamed else stream_consumer.on_commentary(text)
            elif not already_streamed and ctx._status_adapter and str(text or "").strip():
                self._send_status_text(text, ctx._status_thread_metadata, "interim_assistant_callback scheduling error")

        return stream_consumer, stream_delta_cb, interim_assistant_cb, want_interim_messages

    # ── agent resolution (cache reuse vs fresh build) ───────────────────────────────────────

    @dataclasses.dataclass
    class _CachedAgentLookup:
        agent: Any = None
        reused: bool = False
        evicted: Any = None  # agent evicted under the lock; released off-lock on a daemon thread

    def _skip_context_files(self, platform_key) -> bool:
        """gateway.platforms.<plat>.skip_context_files: messaging platforms may opt out of
        filesystem-heavy context-file discovery (SOUL.md, AGENTS.md, .cursorrules)."""
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(self._runner, self._ctx.source)
        if policy:
            return policy.ignore_rules
        platforms_cfg = (self._ctx.user_config.get("gateway") or {}).get("platforms") or {}
        # ``hermes gateway setup`` writes ``gateway.platforms`` as a LIST of enabled platform names,
        # not a dict; treat any non-dict shape as "no per-platform overrides" rather than crashing.
        if not isinstance(platforms_cfg, dict):
            return False
        return bool((platforms_cfg.get(platform_key) or {}).get("skip_context_files"))

    def _cached_sid_is_dead(self, cache_lock, cache) -> tuple:
        """(peeked cached session_id, is_dead) — checked OUTSIDE the cache lock. "cached sid != current
        sid" normally means an intentional switch (reuse), but the routing-key self-heal yields the same
        shape with an agent bound to a DEAD session; reusing it re-binds the dead sid and loops."""
        ctx = self._ctx
        peek_sid = None
        if cache_lock and cache is not None:
            with cache_lock:
                entry = cache.get(ctx.session_key)
            if entry and len(entry) > 3:
                peek_sid = entry[3]
        dead = False
        if peek_sid is not None and ctx.session_id is not None and peek_sid != ctx.session_id:
            # The cache is keyed by session_key, so the snapshot's row lives in that key's profile
            # store; its id is no longer in the routing index once the self-heal moved the key on.
            with suppress(Exception):
                dead = self._runner.session_store._is_session_ended_in_db(peek_sid, session_key=ctx.session_key)
        return peek_sid, dead

    def _current_message_count(self):
        """Cross-process write guard input: the session's current DB message_count (or None)."""
        ctx = self._ctx
        if self._runner._session_db is None or not ctx.session_id:
            return None
        count = None
        with suppress(Exception):
            # run_sync is off-loop (executor); sync DB is fine.
            row = self._runner._session_db._db.get_session(ctx.session_id)
            if row:
                count = row.get("message_count", 0)
        return count

    def _pop_cached_agent_for_eviction(self):
        """Evict under the lock but DEFER release (release_clients can block on memory-provider /
        socket teardown while the idle sweeper waits on this lock). The turn rebuilds a fresh agent, so
        the caller does a SOFT release that keeps sandbox / browser / bg processes."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        evicted = self._runner._agent_cache.pop(self._ctx.session_key, None)
        agent = evicted[0] if isinstance(evicted, tuple) and evicted else None
        return agent if agent and agent is not _AGENT_PENDING_SENTINEL else None

    def _lookup_cached_agent(self, sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count):
        ctx = self._ctx
        out = self._CachedAgentLookup()
        if not (cache_lock and cache is not None):
            return out
        with cache_lock:
            cached = cache.get(ctx.session_key)
            if not (cached and cached[1] == sig):
                return out
            # cached[2] = message_count at cache time (stale when a second process appended rows);
            # cached[3] = the session_id the snapshot was taken for.
            cached_mc = cached[2] if len(cached) > 2 else None
            cached_sid = cached[3] if len(cached) > 3 else None
            # Same session_key, other conversation: the counts track DIFFERENT DB rows, so the
            # comparison is meaningless — REUSE rather than bust the prompt cache on every switch.
            sid_mismatch = cached_sid is not None and ctx.session_id is not None and cached_sid != ctx.session_id
            # Re-validate the outside-lock dead-session peek against the tuple read under THIS lock:
            # a stale "dead" verdict must never be applied to a different (possibly live) agent.
            if sid_mismatch and dead and cached_sid == peek_sid:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "cached agent's session_id %s is ended in "
                    "state.db (stale self-heal artifact, "
                    "#54878 x #54947) — discarding instead of "
                    "reusing across the routing recovery", ctx.session_key, cached_sid,
                )
            elif not sid_mismatch and cached_mc is not None and msg_count is not None and msg_count != cached_mc:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "message_count changed (%s -> %s), "
                    "possible cross-process write", ctx.session_key, cached_mc, msg_count,
                )
            else:
                out.agent = cached[0]
                # Refresh LRU order so cap enforcement evicts truly-oldest entries.
                if hasattr(cache, "move_to_end"):
                    with suppress(KeyError):
                        cache.move_to_end(ctx.session_key)
                self._runner._init_cached_agent_for_turn(out.agent, ctx._interrupt_depth)
                # Cached agent may have been created with old config.
                out.agent.max_iterations = max_iterations
                logger.debug("Reusing cached agent for session %s", ctx.session_key)
                out.reused = True
                return out
            out.evicted = self._pop_cached_agent_for_eviction()
        return out

    def _release_evicted_agent(self, agent) -> None:
        """Off-lock soft release on a daemon thread so teardown never blocks the gateway loop."""
        self._runner._spawn_release_thread(
            self._runner._release_evicted_agent_soft, (agent,), f"agent-xproc-evict-{str(self._ctx.session_key)[:24]}",
            inline_fallback=True,
        )

    def _build_fresh_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations,
                           reasoning_config, pr, skip_context_files):
        from gateway.run import _checkpoint_agent_kwargs
        ctx = self._ctx
        runner = self._runner
        src = ctx.source
        from gateway.session_authorities import active_authority
        from gateway.session_policy import policy_for_source
        from gateway.session_worker_construct import session_prefill
        policy = policy_for_source(runner, src)
        return ctx.AIAgent(
            model=turn_route["model"], **turn_route["runtime"], **_checkpoint_agent_kwargs(ctx.user_config),
            max_iterations=max_iterations, quiet_mode=not bool(policy and policy.verbose), verbose_logging=False,
            enabled_toolsets=ctx.enabled_toolsets, disabled_toolsets=ctx.disabled_toolsets,
            ephemeral_system_prompt=combined_ephemeral or None,
            prefill_messages=(session_prefill(active_authority(runner), policy, src.chat_id) if policy
                              else runner._prefill_messages) or None,
            reasoning_config=reasoning_config, service_tier=runner._service_tier,
            request_overrides=turn_route.get("request_overrides"),
            providers_allowed=pr.get("only"), providers_ignored=pr.get("ignore"), providers_order=pr.get("order"),
            provider_sort=pr.get("sort"), provider_require_parameters=pr.get("require_parameters", False),
            provider_data_collection=pr.get("data_collection"),
            session_id=ctx.session_id, platform=platform_key,
            user_id=src.user_id, user_id_alt=src.user_id_alt, user_name=src.user_name,
            chat_id=src.chat_id, chat_name=src.chat_name, chat_type=src.chat_type, thread_id=src.thread_id,
            gateway_session_key=ctx.session_key,
            session_db=getattr(runner._session_db, "_db", runner._session_db),
            # Reload from disk — do not reuse the startup snapshot.
            # See #60955.
            fallback_model=None if api_runtime_locked() else self._runner._refresh_fallback_model(),
            skip_context_files=skip_context_files,
            # Keep the persona even with minimal context: soul identity is one small file.
            load_soul_identity=not bool(policy and policy.ignore_rules),
            skip_memory=bool(policy and policy.ignore_rules),
            pass_session_id=bool(policy and policy.pass_session_id),
            # A local session's frozen budget (`--run-budget` / its profile's agent.run_budget_seconds);
            # None leaves the agent reading the owner's config, as before.
            run_budget_seconds=policy.run_budget_seconds if policy else None,
        )

    def _resolve_turn_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr):
        """Reuse this session's cached AIAgent (frozen system prompt + tool schemas → prompt cache
        hits) or build a fresh one. Returns (agent, reused_cached_agent)."""
        ctx = self._ctx
        runner = self._runner
        skip_context_files = self._skip_context_files(platform_key)
        sig = runner._agent_config_signature(
            turn_route["model"], turn_route["runtime"], ctx.enabled_toolsets, combined_ephemeral,
            cache_keys=runner._extract_cache_busting_config(ctx.user_config),
            user_id=getattr(ctx.source, "user_id", None),
            user_id_alt=getattr(ctx.source, "user_id_alt", None),
            skip_context_files=skip_context_files,
        )
        cache_lock = getattr(runner, "_agent_cache_lock", None)
        cache = getattr(runner, "_agent_cache", None)
        peek_sid, dead = self._cached_sid_is_dead(cache_lock, cache)
        msg_count = self._current_message_count()
        found = self._lookup_cached_agent(sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count)
        agent = found.agent
        # Lock released — refresh the reused agent's fallback chain from disk OUTSIDE the cache lock
        # (disk I/O under the lock stalls the idle-sweep watcher and Discord heartbeats). A chain
        # configured after caching must reach the next turn; per-session serialization keeps it safe.
        locked = api_runtime_locked() is not None
        if found.reused and agent is not None and locked and getattr(agent, "_fallback_activated", False):
            # A cached agent already moved onto a fallback provider cannot serve a confirmed lock.
            found.evicted, found.reused, agent = agent, False, None
            with cache_lock:
                cache.pop(ctx.session_key, None)
        if found.reused and agent is not None:
            self._runner._apply_fallback_chain_to_agent(agent, None if locked else runner._refresh_fallback_model())
        if found.evicted is not None:
            self._release_evicted_agent(found.evicted)
        if agent is None:
            from tools.delegate_tool_config import gateway_delegation_inline, gateway_delegation_inline_scope
            # delegation.gateway_mode from this turn's own (profile-scoped) config and the chat's resolved interim
            # setting (never muted for a heartbeat turn, which shares the user's session and its cached agent);
            # read once, by the agent's first tool build, then carried by its delegate_task schema and pin.
            inline = gateway_delegation_inline(
                ctx.user_config, platform_key, interim_enabled=ctx.interim_assistant_messages_enabled)
            with gateway_delegation_inline_scope(inline):
                agent = self._build_fresh_agent(
                    turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr,
                    skip_context_files,
                )
            if cache_lock and cache is not None:
                with cache_lock:
                    # Record the snapshot's session_id with message_count so the cross-process guard
                    # can skip the meaningless count comparison if the active session_id switches.
                    cache[ctx.session_key] = (agent, sig, msg_count, ctx.session_id)
                    runner._enforce_agent_cache_cap()
            logger.debug("Created new agent for session %s (sig=%s)", ctx.session_key, sig)
        store = getattr(agent, "_todo_store", None)
        if store is not None:
            store.strict_lifecycle = self._deep_research_requested
            if self._deep_research_requested and (ctx.deep_research.get("resumed") or not store.has_items()):
                checkpoint = ctx.deep_research.get("checkpoint") or {}
                todos = checkpoint.get("todos") if isinstance(checkpoint, dict) else None
                if isinstance(todos, list):
                    store.restore(todos, revision=max(1, int(checkpoint.get("todo_revision", 1) or 1)))
        return agent, found.reused

    # ── per-turn agent wiring ───────────────────────────────────────────────────────────────

    def _notice_callback_sync(self, notice) -> None:
        """Credits / out-of-band notices (usage bands, depletion, restored) fire from the agent's
        sync worker thread; hop onto the gateway loop. Fired-once latch lives on the cached agent."""
        from gateway.run import render_notice_line
        from gateway.warning_notifications import is_diagnostic_notice, render_notification
        if self._ctx.mute_notification_reply or not self._status_live():
            return
        # A usage-window limit line repeats the turn's own reply, which carries the sentence and the
        # switch button (a chat message cannot be cleared, so it would only pile up). The 90% heads-up
        # is a desktop / terminal nudge; in a chat it is noise (/usage has the numbers).
        key = str(getattr(notice, "key", "") or "")
        if key == "credits.window_limit" or key.startswith("credits.window."):
            return
        diagnostic = is_diagnostic_notice(notice)
        def present():
            try:
                line = render_notice_line(notice)
                if key.startswith("credits."):
                    # Window plans (e.g. a paused plan's line): a chat's /topup has no credits settings.
                    from agent.plan_limits import portal_wording
                    line = portal_wording(line)
            except Exception:
                logger.debug("render_notice_line failed", exc_info=True)
                return
            if line:
                self._schedule(self._runner._deliver_platform_notice(self._ctx.source, line), "notice_callback delivery scheduling error")
        render_notification(present, platform=self._ctx.source.platform,
                            user_config=self._ctx.user_config, diagnostic=diagnostic)

    def _make_bg_review_callbacks(self):
        """(send, release): background-review messages ("💾 Memory updated") are held until the
        adapter's post-delivery hook releases them after the main response lands."""
        from gateway.run import _interim_metadata, _non_conversational_metadata
        ctx = self._ctx
        release_evt = threading.Event()
        pending: list[str] = []
        pending_lock = threading.Lock()

        owner = self._approval_owner
        local_viewer = owner is not None and getattr(ctx.source, "platform", None) == Platform.LOCAL

        def deliver(message: str) -> None:
            if local_viewer and owner is not None:
                # LocalSessionAdapter.send publishes nothing, so a local viewer only sees the
                # summary as the in-process TUI's review.summary frame. Not generation-fenced:
                # the review fork finishes after this turn's running claim has settled.
                authority, session_id, _generation = owner
                live = authority.sessions.get(session_id)
                if live is not None:
                    live.event_stream.publish(session_id, {"text": str(message)}, event_type="review.summary")
                return
            if self._status_live():
                self._send_status_text(
                    message,
                    _interim_metadata(_non_conversational_metadata(ctx._status_thread_metadata, platform=ctx.source.platform)),
                    "background_review_callback scheduling error",
                )

        def release() -> None:
            release_evt.set()
            with pending_lock:
                queued = list(pending)
                pending.clear()
            for message in queued:
                deliver(message)

        def send(message: str) -> None:
            if local_viewer and owner is not None:
                # The review fork reports from its own thread while the drain may still be
                # settling this turn. Hold the row until the authority published the turn's
                # message.complete (it clears the execution stamp only after that), the order
                # the in-process TUI produced. Never fenced: a late summary still reaches viewers.
                authority, session_id, generation = owner
                live = authority.sessions.get(session_id)
                deadline = time.monotonic() + 120
                while (live is not None and time.monotonic() < deadline
                       and live.event_stream.execution.get("execution_generation") == generation):
                    time.sleep(0.05)
                deliver(message)
                return
            if not self._status_live():
                return
            if not release_evt.is_set():
                with pending_lock:
                    if not release_evt.is_set():
                        pending.append(message)
                        return
            deliver(message)

        return send, release

    @staticmethod
    def _merge_turn_request_overrides(agent, turn_route) -> None:
        """Merge, never overwrite: init-time request overrides (e.g. a custom provider's extra_body)
        must survive every reused-agent turn. Drop only the PREVIOUS turn's routing overrides before
        layering this turn's, so stale per-turn values never linger."""
        overrides = dict(getattr(agent, "request_overrides", {}) or {})
        for key, value in (getattr(agent, "_gateway_turn_request_overrides", {}) or {}).items():
            if overrides.get(key) == value:
                overrides.pop(key, None)
        turn_overrides = dict(turn_route.get("request_overrides") or {})
        overrides.update(turn_overrides)
        agent.request_overrides = overrides
        agent._gateway_turn_request_overrides = turn_overrides

    def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config,
                                   stream_delta_cb, interim_assistant_cb, want_interim_messages):
        """Per-message state (callbacks, reasoning, voice route) — never baked into the cached agent."""
        ctx = self._ctx
        runner = self._runner
        agent._notification_config = ctx.user_config
        agent._notification_platform = ctx.source.platform
        # ALWAYS attached (never gated to None): its body gates each event class, and subagent-
        # failure notices must fire even with tool_progress/thinking off.
        agent.tool_progress_callback = ctx.progress_callback
        # Discord's one-time voice ack and Slack's task cards both ride the authoritative start
        # callback, so neither infers identity from tool names.
        agent.tool_start_callback = (
            (ctx.native_tool_start_callback or ctx.voice_ack_callback)
            if (ctx._voice_ack_guild[0] is not None or ctx._native_slack_task_cards
                or getattr(ctx, "telegram_activity_trail_enabled", False)) else None
        )
        agent.tool_complete_callback = (
            ctx.native_tool_complete_callback
            if (ctx._native_slack_task_cards or getattr(ctx, "telegram_activity_trail_enabled", False)) else None
        )
        if self._approval_owner is not None:
            agent.tool_start_callback = self.combined_tool_start_callback
            agent.tool_complete_callback = self.combined_tool_complete_callback
        agent.step_callback = ctx._step_callback_sync if (ctx._hooks_ref.loaded_hooks or self._approval_owner is not None) else None
        agent.stream_delta_callback = stream_delta_cb
        agent.interim_assistant_callback = interim_assistant_cb if want_interim_messages else None
        from gateway.run_turn_progress import local_viewer_turn
        agent.status_callback, agent.notice_callback = ctx._status_callback_sync, (
            self._local_notice_callback_sync if local_viewer_turn(self) else self._notice_callback_sync)
        agent.reasoning_callback = None  # per turn: a reused agent never keeps an earlier API turn's sink
        if self._approval_owner is not None:
            from gateway.session_api_turn import api_execution, wire_api_observers
            if api_execution.get() is not None:
                # The API request's SSE observes reasoning/status/commentary through its admission.
                wire_api_observers(agent, self._approval_owner, want_interim_messages)
        # A platform send can't be retracted; a local viewer withdraws the keyed notice.
        agent.notice_clear_callback = self._local_notice_clear_callback_sync if local_viewer_turn(self) else None
        agent.event_callback = ctx._event_callback_sync
        agent.reasoning_config, agent.service_tier = reasoning_config, runner._service_tier
        self._merge_turn_request_overrides(agent, turn_route)
        from gateway.session_surface import arm_surface_turn
        # auxiliary.voice_chat route (a voice note, or an admission committed as a voice turn) and
        # this turn's must-deliver notes; a managed worker arms its agent through the same helper.
        arm_surface_turn(agent, runner._consume_pending_turn_sidecar_notes(ctx.session_key), voice_turn=ctx.voice_turn)
        agent.background_review_callback, bg_release = self._make_bg_review_callbacks()
        # Register the release hook on the adapter so base.py's finally block fires it after the
        # main response is delivered.
        if ctx._status_adapter and ctx.session_key:
            if getattr(type(ctx._status_adapter), "register_post_delivery_callback", None) is not None:
                ctx._status_adapter.register_post_delivery_callback(ctx.session_key, bg_release, generation=ctx.run_generation)
            else:
                pdc = getattr(ctx._status_adapter, "_post_delivery_callbacks", None)
                if pdc is not None:
                    pdc[ctx.session_key] = bg_release
        # display.memory_notifications: off | on (generic "💾 Memory updated", default) | verbose.
        # `display:` present-but-null yields None, not the {} default (same `or {}` guard as
        # display_config.py / runtime_footer.py).
        mem_notif = (ctx.user_config.get("display") or {}).get("memory_notifications")
        if isinstance(mem_notif, bool):
            mem_notif = "on" if mem_notif else "off"
        agent.memory_notifications = str(mem_notif).lower() if mem_notif else "on"
        agent.clarify_callback = self._clarify_callback_sync
        # Thinking between tool calls is independent of tool_progress mode (Mattermost opts in
        # per platform so global scratch-text doesn't leak into threads).
        agent.thinking_progress = ctx._thinking_enabled
        if ctx.mute_notification_reply:
            # Controls and operational event/step callbacks remain wired. These
            # presentation callbacks are rebound on every next turn.
            agent.tool_progress_callback = None
            agent.tool_start_callback = None
            agent.tool_complete_callback = None
            # Keep diagnostic observers installed; concrete sinks veto display.
            agent.stream_delta_callback = None
            agent.interim_assistant_callback = None
            agent.thinking_progress = False
        ctx.agent_holder[0] = agent  # interrupt support
        if self._approval_owner is not None:
            authority, owner_id, generation = self._approval_owner
            authority.adopt_agent(owner_id, generation, agent)
        # The titler fires from the turn prologue, so attach the rename lane before the run.
        self._attach_session_title_callback(agent, ctx)
        # Publish turn ownership for /stop, /new, disconnect and shutdown interrupts; older session
        # processes are outside this baseline and remain alive.
        agent._gateway_turn_process_task_id, agent._gateway_turn_process_baseline = ctx.process_task_id, ctx.process_baseline
        ctx.tools_holder[0] = getattr(agent, "tools", None)  # transcript logging

    # ── blocking prompts from the agent thread (approval / clarify) ─────────────────────────

    def _close_native_stream_boundary(self, reason: str, placeholder: str | None = None, reopen: bool = False) -> bool:
        """Native-streaming platforms (e.g. WeCom): an interrupting interaction (approval or clarify
        prompt) must finalize the current stream first, or post-interaction output keeps updating the
        OLD bubble above the prompt. Runs on the agent thread; the consumer serializes via its queue."""
        sc = self._stream_consumer()
        if not (sc and getattr(sc, "_use_native_streaming", False)):
            return True
        cancelled_flag = None
        try:
            boundary = sc.close_for_approval_prompt(placeholder, reason=reason, reopen=reopen)
            # Returns (future, cancelled_flag) or just a future.
            if isinstance(boundary, tuple):
                boundary, cancelled_flag = boundary
            if not hasattr(boundary, "result"):
                return True
            ok = boundary.result(timeout=10)
            if not ok:
                logger.warning(
                    "%s boundary failed to close stream properly — "
                    "prompt may still appear in typing bubble", reason,
                )
            return bool(ok)
        except (TimeoutError, Exception) as err:
            if cancelled_flag is not None:
                cancelled_flag["cancelled"] = True
            logger.warning("%s boundary timed out or failed: %s", reason, err)
            return False

    async def _send_shared_clarify(self, entry, **kwargs):
        result = await self._ctx._status_adapter.send_clarify(**kwargs)
        if self._approval_owner is not None and result.success:
            authority, session_id, generation = self._approval_owner
            from gateway import local_viewer_events
            authority.register_clarify(session_id, generation, entry,
                                       timeout_s=local_viewer_events.prompt_timeout_s(self, "clarify"))
        return result

    def _clarify_callback_sync(self, questions) -> dict:
        """Answer the clarify tool's questions (clarify_tool's synchronous contract): one card per
        question, stop at the first the user never answers. The stream/typing re-arm waits for the
        last question — between two cards it only opens a bubble the next boundary closes."""
        from gateway.run_turn_runner_clarify_delivery import UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE
        from tools.clarify_gateway import CANCELLED, SKIPPED
        if _finite_viewer_turn():
            # `chat -q` / `-z`: the viewer detaches on the first prompt, so nobody can answer.
            # Answer headless at once, as main's _single_query_clarify_callback did in process.
            from gateway.session_finite import finite_clarify_reply
            return finite_clarify_reply()
        from gateway.session_cli_turn import clarify_batch_turn
        if clarify_batch_turn(self):
            # Main's batch panel (classic CLI, Ink TUI) shows every question at once: one request.
            from gateway.session_clarify_batch import ask_clarify_batch
            return ask_clarify_batch(self, questions)
        answers: dict[str, Any] = {}
        reply: dict[str, Any] = {"answers": answers, "outcome": "submitted"}
        last = len(questions) - 1
        # The typed-"skip" hint is for messaging adapters, where a chat reply answers the card. A local
        # viewer (TUI / Desktop / CLI) renders main's modal with its own skip control, unhinted.
        platform = getattr(getattr(self._ctx, "source", None), "platform", None)
        hint = "" if platform == Platform.LOCAL else f"\n{t('gateway.clarify.skip_hint')}"
        for index, entry in enumerate(questions):
            question = f"{entry['question']}{hint}"
            raw, answered = self._ask_clarify_question(
                question, entry["choices"], bool(entry["multi_select"]), rearm=index == last)
            if raw == CANCELLED:
                reply["outcome"] = "cancelled"
                break
            if not answered:
                # The surface's own no-answer text ("could not be delivered", "did not respond
                # within Nm") rides along as ``notice``: blank answers alone read as user
                # inactivity, which is the misreport #112684 describes for an undelivered card.
                undelivered = raw in (UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE)
                reply.update(outcome="undelivered" if undelivered else "timed_out", notice=raw)
                break
            answers[entry["qid"]] = None if raw == SKIPPED else raw
        return reply

    def _ask_clarify_question(self, question, choices, multi_select, rearm: bool = True) -> tuple[str, bool]:
        """One card: register, send, wait, then retire it (no answer) or re-arm (answer).
        Returns ``(response, answered)``; the caller decides what "no answer" means."""
        from gateway.run_turn_runner_clarify_delivery import (
            UNDELIVERED_NO_SURFACE, _clarify_send_then_wait, text_fallback_coro)
        from tools import clarify_gateway as clarify_mod
        import uuid
        ctx = self._ctx
        if not ctx._status_adapter:
            # Nothing can render the question: say so, or the batch's blank answers read as
            # user inactivity (#112684).
            return UNDELIVERED_NO_SURFACE, False
        session_key = ctx.session_key or ""
        clarify_id = uuid.uuid4().hex[:10]
        choices = list(choices) if choices else None
        send_kwargs = dict(
            chat_id=ctx._status_chat_id, question=question, choices=choices, clarify_id=clarify_id,
            session_key=session_key, metadata=ctx._status_thread_metadata,
        )

        def _text_fallback():
            """Schedule the plain-text prompt when the native card cannot render; None = no such path."""
            coro = text_fallback_coro(ctx._status_adapter, **send_kwargs)
            return None if coro is None else self._schedule(coro, "Clarify text fallback failed to schedule")
        entry = clarify_mod.register(
            clarify_id=clarify_id, session_key=session_key, question=question, choices=choices,
            multi_select=bool(multi_select),
        )
        # Unlike approval, clarify passes reopen=True so the continuation re-opens a native stream
        # below the question; if the re-seed fails the consumer degrades to send() automatically.
        self._close_native_stream_boundary("Clarify", t("gateway.clarify.native_stream_placeholder"), reopen=True)
        # Pause typing: a "thinking..." status must not obscure the prompt or block an "Other" reply
        # on platforms that disable input while typing (Slack Assistant).
        with suppress(Exception):
            ctx._status_adapter.pause_typing_for_chat(ctx._status_chat_id)
        # Ordering barrier: flush buffered assistant prose BEFORE the poll, which goes out on a
        # separate agent-thread-blocking path and would otherwise render ABOVE its own explanation.
        # Best-effort + short timeout so the agent thread never hangs if the consumer isn't running.
        flush = getattr(self._stream_consumer(), "flush_pending_sync", None)
        try:
            if callable(flush):
                flush(timeout=3.0)
        except Exception:
            logger.debug("Stream-consumer flush before clarify prompt failed", exc_info=True)
        fut = self._schedule(
            self._send_shared_clarify(entry, **send_kwargs),
            "Clarify send failed to schedule",
        )
        # Boundary rule (see _approval_send_outcome): a send timeout is AMBIGUOUS — the card may
        # have posted with a late ack. Only a definitive failure tears down the registration;
        # ambiguous falls through to the bounded wait so a late reply resolves. A definitive
        # failure — immediate or late — retries once as plain text before giving up.
        response, answered = _clarify_send_then_wait(
            fut, clarify_id=clarify_id, session_key=session_key, clarify_mod=clarify_mod,
            fallback=_text_fallback)
        if self._approval_owner is not None:
            authority, session_id, generation = self._approval_owner
            authority.sessions[session_id].controls.snapshot(session_id, generation)
        # Branch on the explicit flag, never on the text: a real answer can start with '[' (a
        # "[A] staging" label, "[urgent] ..." free text) and must not be mistaken for a sentinel.
        if not answered:
            # No answer arrived (timeout, /new, run end): retire the native card so it stops
            # looking answerable. Adapters without a persistent card have no such method.
            retire = getattr(type(ctx._status_adapter), "retire_clarify_card", None)
            if callable(retire):
                self._schedule(
                    retire(ctx._status_adapter, clarify_id, _clarify_expired_notice()),
                    "Clarify card retire failed to schedule")
        elif rearm:
            # Reopen typing IMMEDIATELY, not on the LLM's first post-answer token (native streaming
            # otherwise re-seeds lazily on the first delta: ~48s of dead air). request_reopen_seed is
            # a no-op outside the reopen-pending native state.
            sc = self._stream_consumer()
            if sc is not None:
                try:
                    sc.request_reopen_seed()
                except Exception:
                    logger.debug("request_reopen_seed after clarify answer failed", exc_info=True)
            try:
                ctx._status_adapter.resume_typing_for_chat(ctx._status_chat_id)
            except Exception:
                logger.debug("resume_typing_for_chat after clarify answer failed", exc_info=True)
        return response, answered

    def _approval_notify_sync(self, approval_data: dict) -> None:
        if self._approval_owner is not None:
            authority, session_id, generation = self._approval_owner
            authority.check_approval_generation(session_id, generation)
        # A native decline must finish before any observer can authorize work.
        self._render_approval_sync(approval_data)
        if self._approval_owner is not None:
            from gateway import local_viewer_events
            authority.register_approval(session_id, generation, self._ctx.session_key, approval_data,
                                        timeout_s=local_viewer_events.prompt_timeout_s(self, "approval"))

    def _render_approval_sync(self, approval_data: dict) -> None:
        """Send the approval request from the agent thread: the adapter's interactive button
        approvals (``send_exec_approval``) when available, else plain text with ``/approve`` steps."""
        from gateway.run import _approval_send_outcome, _format_exec_approval_fallback, _interim_metadata, _redact_approval_command
        from gateway.run_turn_runner_approval_settle import register_timeout_notice
        ctx = self._ctx
        adapter = ctx._status_adapter
        # Slack's assistant_threads_setStatus disables the compose box, so the user can't type
        # /approve while "is thinking..." shows. Pausing stops _keep_typing re-setting it; resumed
        # in approve/deny.
        adapter.pause_typing_for_chat(ctx._status_chat_id)
        self._close_native_stream_boundary("Approval")
        # Redact credentials before display: the raw command string can carry secrets. Both the button and plain-text paths use this value.
        cmd = _redact_approval_command(approval_data.get("command", ""))
        desc = approval_data.get("description") or ea_default_reason_text()
        flags = {k: approval_data.get(k, d) for k, d in (("allow_permanent", True), ("allow_session", True), ("smart_denied", False))}
        # Check the *class*, not the instance — MagicMock auto-creates attributes in tests.
        if _renders_exec_approval_buttons(type(adapter)):
            # The card is rendered on the gateway loop, outside the turn's profile
            # scope, so the adapter cannot resolve the session's owner/home from the
            # ambient HERMES_HOME. Stamp the turn's initiator, profile home and owner
            # into the metadata here, where the source is known: without them the
            # adapter stores an empty owner and the owner themself is denied at tap
            # time ("Only the owner ... can approve it"). Best-effort: a failure
            # leaves the metadata as-is and the adapter falls back as before.
            approval_metadata = dict(ctx._status_thread_metadata or {})
            approval_metadata["user_id"] = str(getattr(getattr(ctx, "source", None), "user_id", "") or "")
            try:
                home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                if callable(home_resolver):
                    from pathlib import Path
                    home = Path(home_resolver(ctx.source)).expanduser().resolve()
                    approval_metadata["profile_home"] = str(home)
                    from gateway.mongo_multiuser import owner_user_id
                    approval_metadata["owner_user_id"] = str(owner_user_id(home))
            except Exception:
                logger.debug("Approval metadata owner/profile resolution failed", exc_info=True)
            try:
                fut = self._schedule(
                    adapter.send_exec_approval(
                        chat_id=ctx._status_chat_id, command=cmd, session_key=ctx.session_key or "",
                        description=desc, metadata=approval_metadata,
                        request_id=approval_data.get("request_id"), **flags,
                    ),
                    "send_exec_approval scheduling error",
                )
                if fut is None:
                    raise RuntimeError("send_exec_approval: loop unavailable")
                outcome = _approval_send_outcome(fut, timeout=15)
                if outcome == "sent":
                    # Without this, a card whose timer runs out keeps live buttons and nobody
                    # learns the command did NOT run (only the TUI registered a settle hook).
                    register_timeout_notice(
                        self, approval_data, command=cmd,
                        card_message_id=getattr(fut.result(timeout=0), "message_id", None))
                    return
                if outcome == "ambiguous":
                    # Timeout ≠ failure: the card may have posted with a late ack. The prompt
                    # registration stays alive so a tap still resolves; re-sending made duplicate
                    # cards + orphaned "/approve: nothing pending".
                    logger.warning(
                        "Button-based approval send timed out — treating "
                        "as possibly-delivered (no re-send; the prompt "
                        "stays armed for a late tap)"
                    )
                    return
                if outcome == "declined":
                    # P5(b): the connector AUTHORIZED this destination and
                    # refused it. The text fallback below re-sends the same
                    # content to the same chat, which would turn a refused
                    # button card into a delivered plain-text one — the exact
                    # leak the egress guard exists to stop. A decline is
                    # definitive, so unlike `ambiguous` the registration is
                    # torn down; unlike `failed`, nothing is re-sent.
                    logger.warning(
                        "Button-based approval DECLINED by the connector's "
                        "egress guard — not falling back to text (the "
                        "destination is not approved for this connection)"
                    )
                    # RAISE, do not return. This function is the notify_cb for
                    # `_await_gateway_decision`, which already has a correct
                    # undeliverable path: a raising notify drops the queue entry
                    # and returns `notify_failed`, unblocking the tool. Returning
                    # quietly suppressed the text fallback (right) but left the
                    # CENTRAL approval entry pending (wrong) — the dangerous
                    # command then blocked until the approval timeout. My earlier
                    # comment claimed the registration was torn down; only the
                    # adapter's private prompt map was.
                    raise _ExecApprovalDeclined(
                        "exec approval undeliverable: connector egress declined "
                        "this destination"
                    )
                logger.warning("Button-based approval failed (send returned error), falling back to text")
            except _ExecApprovalDeclined:
                # Must escape this handler: the fallback below is a text send to
                # the destination the connector just refused.
                raise
            except Exception as e:
                logger.warning("Button-based approval failed, falling back to text: %s", e)
        # Plain-text prompt with the adapter's typed prefix (e.g. `!approve`): typed "/" is blocked
        # in Slack threads and reserved by Matrix clients.
        msg = _format_exec_approval_fallback(cmd, desc, getattr(adapter, "typed_command_prefix", "/"), **flags)
        try:
            # Mark as approval prompt: WeCom routes it through the control lane and Telegram pushes it
            # in "important" mode (#132516). Never ``notify`` — A2A reads that as the turn-final reply.
            metadata = {**(ctx._status_thread_metadata or {}), "is_approval_prompt": True}
            fut = self._schedule(
                adapter.send(ctx._status_chat_id, msg, metadata=_interim_metadata(metadata)), "Approval text-send scheduling error",
            )
            if fut is not None:
                fut.result(timeout=15)
                # No card to edit on the text path: the prompt has no buttons to drop and carries
                # the /approve instructions, so the timeout notice is posted as a new message.
                register_timeout_notice(self, approval_data, command=cmd, card_message_id=None)
        except Exception as e:
            logger.error("Failed to send approval request: %s", e)

    # ── run_sync phases ─────────────────────────────────────────────────────────────────────

    def _load_turn_history(self, agent, reused_cached_agent):
        from gateway.run import (
            _build_gateway_agent_history, _collect_history_media_paths, _message_timestamps_enabled,
            _select_cached_agent_history,
        )
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None and api['history'] is not None:
            from gateway.run import _collect_history_media_paths
            history = api['history']
            tail = history[-1] if history else None
            if api.get('resume_unanswered') and isinstance(tail, dict) and tail.get('role') == 'user':
                # The door's re-run continues the row its failed attempt persisted (#115325). Only a
                # bare user tail: a tool suffix is completed work the re-run must still see.
                from agent.session_persistence import adopt_unanswered_turn
                history = list(history)
                adopt_unanswered_turn(history, api['content'], agent)
            return history, None, _collect_history_media_paths(history)
        # Transcript rows ({role, content, timestamp}) lose timestamps; interrupt-path agent messages
        # (tool_calls/tool_call_id/reasoning) pass through intact so the API sees valid assistant→tool
        # sequences. Telegram observed=True rows are withheld from replayable history and attached to
        # the current addressed message as API-only context.
        agent_history, observed_group_context = _build_gateway_agent_history(
            ctx.history, channel_prompt=ctx.channel_prompt, inject_timestamps=_message_timestamps_enabled(ctx.user_config),
        )
        # FTS write-corruption guard: if persistence failed silently the reloaded transcript is stale
        # while the SAME cached agent still holds the live conversation (same-session amnesia). Only
        # for a reused agent bound to this exact session_id.
        # Replacing the live transcript with that shorter copy causes immediate same-session amnesia. See
        # #50502.
        if reused_cached_agent and getattr(agent, "session_id", None) == ctx.session_id:
            selected = _select_cached_agent_history(agent_history, getattr(agent, "_session_messages", None))
            # Consecutive lagging turns per session (cleared on /new): one lag is a blip, a streak
            # is the #114266 write outage and must escalate past a repeating WARNING.
            streaks = getattr(self._runner, "_transcript_lag_streaks", None)
            if streaks is None:  # tests may build bare runners
                streaks = self._runner._transcript_lag_streaks = {}
            if selected is not agent_history:
                streak = streaks[ctx.session_key] = streaks.get(ctx.session_key, 0) + 1
                log = logger.error if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else logger.warning
                log(
                    "Persisted transcript lagged live cached history for "
                    "session %s (disk=%d, memory=%d, consecutive_turns=%d); preserving live "
                    "conversation context (possible FTS write corruption)%s",
                    ctx.session_key, len(agent_history), len(selected), streak,
                    "; state.db is not receiving this session's writes and needs operator attention"
                    if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else "",
                )
                # The live history bypassed _build_gateway_agent_history's cleanup — re-apply
                # the full canonicalization so no replay transform can slip through.
                agent_history = canonicalize_replay_history(selected)
            else:
                streaks.pop(ctx.session_key, None)
        # MEDIA paths already in history are excluded from this turn's extraction (compression-safe).
        return agent_history, observed_group_context, _collect_history_media_paths(agent_history)

    def _prepend_pending_note(self, attr: str) -> None:
        """Consume a one-shot per-session note (model switch, /reload-skills) into the NEXT user
        message. Nothing hits the transcript out-of-band, so alternation stays intact."""
        ctx = self._ctx
        notes = getattr(self._runner, attr, None)
        note = notes.pop(ctx.session_key, None) if notes and ctx.session_key and ctx.session_key in notes else None
        if note:
            ctx.message = note + "\n\n" + ctx.message

    def _resume_note_interactive(self) -> bool:
        """Interactive platforms report the restore and ask what next; event platforms (webhook,
        API server) continue the work — nobody is present to answer."""
        return bool(getattr(self._runner._delivery_adapter_for(self._ctx.source), "interactive_resume", True))

    def _prepare_turn_message(self, agent_history):
        """Prepend recovery/notice guidance to ``ctx.message``.

        Returns (persist_user_message_override, persist_user_timestamp_override): real user text is
        kept separate from API-only recovery guidance so stale guidance never replays as user text.
        """
        from gateway.run import (
            _auto_continue_freshness_window, _is_fresh_gateway_interruption,
            _last_transcript_timestamp, _prepare_resume_pending_message, build_resume_recovery_note,
        )
        ctx = self._ctx
        persist_override: Optional[Any] = ctx.persist_user_message
        from gateway.session_cli_turn import cli_turn
        api_text = cli_turn(ctx.inbound_message_id).get('api_text')
        if api_text is not None:
            # The classic CLI's model-facing message (voice prefix, its own model-switch and
            # skills-reload notes); the admitted text stays the persisted user row.
            clean_text = persist_override if persist_override is not None else ctx.message
            persist_override = clean_text
            ctx.message = api_text
        self._prepend_pending_note("_pending_model_notes")
        # Auto-continue: history ending with a tool result means the previous turn was cut off
        # (restart, crash, SIGTERM). Session-level resume_pending (drain-timeout shutdown) uses
        # stronger reason-aware wording that subsumes this case. Both gate on the age of
        # ``history[-1]`` (not agent_history, which stripped tool-row timestamps); no stamp = fresh.
        window = _auto_continue_freshness_window()
        interruption_is_fresh = _is_fresh_gateway_interruption(_last_transcript_timestamp(ctx.history), window_secs=window)
        entry = None
        if ctx.session_key:
            with suppress(Exception):
                entry = self._runner.session_store._entries.get(ctx.session_key)
        resume_pending = entry is not None and getattr(entry, "resume_pending", False)
        resume_reason = (getattr(entry, "resume_reason", None) or "restart_timeout") if resume_pending else None
        # resume_pending freshness ALSO uses the restart watchdog's ``last_resume_marked_at`` (the true
        # interruption stamp): the transcript clock can be hours older for an active thread, and the
        # startup auto-resume turn has empty text, so gating on it alone yields a blank user message.
        mark_is_fresh = resume_pending and _is_fresh_gateway_interruption(
            getattr(entry, "last_resume_marked_at", None), window_secs=window,
        )
        if resume_pending and (interruption_is_fresh or mark_is_fresh):
            # Empty message = the startup auto-resume turn; there is no NEW user message.
            ctx.message, persist_override = _prepare_resume_pending_message(
                resume_reason, ctx.message, interactive=self._resume_note_interactive(),
            )
        elif agent_history and agent_history[-1].get("role") == "tool" and interruption_is_fresh:
            persist_override = ctx.message
            ctx.message = (
                "[System note: A new message has arrived. The conversation "
                "history contains pending tool outputs from an interrupted turn. "
                "IGNORE those pending results. Address the user's NEW message "
                "below FIRST. Do NOT re-execute old tool calls from the history.]\n\n"
                + ctx.message
            )
        self._prepend_pending_note("_pending_skills_reload_notes")
        if api_text is not None:
            persist_override = clean_text  # recovery wording is API-only too, never the stored row
        # Safety net: a startup auto-resume event carries empty text; if the resume_pending branch
        # did not fire (freshness signals disagreed, marker cleared) we must NOT hand the model a blank
        # user turn. Restricted to resume_pending sessions so caption-less image turns are untouched.
        if isinstance(ctx.message, str) and not ctx.message.strip() and resume_pending:
            ctx.message = build_resume_recovery_note(resume_reason, "", interactive=self._resume_note_interactive())
        return persist_override, ctx.persist_user_timestamp

    def _native_image_run_message(self):
        """Wrap the user turn as an OpenAI-style multimodal content list when
        _prepare_inbound_message_text buffered image paths; consume-and-clear so later turns on the
        same runner never re-attach stale images. Falls back to plain text when nothing is readable."""
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None and isinstance(api.get('content'), list):
            return api['content']
        native_imgs = self._runner._consume_pending_native_image_paths(ctx.session_key)
        if not native_imgs:
            return ctx.message
        try:
            from agent.image_routing import build_native_content_parts
            parts, skipped = build_native_content_parts(ctx.message, native_imgs)
            if skipped:
                logger.warning("Native image attachment: skipped %d unreadable path(s): %s", len(skipped), skipped)
            if any(p.get("type") == "image_url" for p in parts):
                return parts
        except Exception as exc:
            logger.warning("Native image attachment failed, falling back to text: %s", exc)
        return ctx.message

    def _prepare_research_todo_resume(self, agent, history) -> Optional[str]:
        """Compare the durable task ledger with paired transcript state before hydration."""
        research = self._ctx.deep_research or {}
        if not self._deep_research_requested:
            return None
        store = getattr(agent, "_todo_store", None)
        if not research.get("resumed"):
            # Fresh session: seed the canonical plan into the agent's own todo
            # store when it has no tasks yet, so a ledger exists even if the
            # agent would otherwise never call todo_list. Never overwrite an
            # existing plan (for example one restored from transcript history).
            if store is not None:
                try:
                    current = store.snapshot().get("todos") or []
                except Exception:
                    current = None
                if current == []:
                    try:
                        from gateway.deep_research_sessions import default_research_plan
                        store.restore(default_research_plan(), revision=1)
                    except Exception:
                        logger.debug("Could not seed DeepResearch task plan", exc_info=True)
            return None
        checkpoint = research.get("checkpoint") or {}
        todos = checkpoint.get("todos")
        if not isinstance(todos, list) or store is None:
            return "The saved task ledger is unavailable; owner review is required before research can continue."
        latest = getattr(agent, "_latest_todo_response", None)
        found = latest(history) if callable(latest) else None
        revision = max(1, int(checkpoint.get("todo_revision", 1) or 1))
        if found is not None:
            prior_todos, prior_revision = found
            core = lambda items: [{key: item.get(key) for key in ("id", "content", "status", "parent")}
                                  for item in items]
            if core(prior_todos) != core(todos):
                return "The task ledger in checkpoint.json differs from the saved conversation's latest paired todo result. Please review and tell me which durable state to retain before continuing; no research was restarted."
            revision = max(revision, int(prior_revision or 0))
        # Same task state, but retain checkpoint timing and prevent history
        # hydration from discarding it solely because an older revision is higher.
        store.restore(todos, revision=revision)
        return None

    def _run_conversation_with_approval(self, agent, agent_history, observed_group_context,
                                        persist_user_message_override, persist_user_timestamp_override):
        """Run the turn with the per-session gateway approval callback registered: dangerous-command
        approval blocks the agent thread (mirrors CLI input()); the callback bridges sync→async."""
        from gateway.run import _wrap_current_message_with_observed_context
        from tools.approval import register_gateway_notify, unregister_gateway_notify
        from tools.approval_context import reset_current_session_key, set_current_session_key
        ctx = self._ctx
        session_key = ctx.session_key or ""
        token = set_current_session_key(session_key)
        register_gateway_notify(session_key, self._approval_notify_sync)
        try:
            resume_issue = self._prepare_research_todo_resume(agent, agent_history)
            if resume_issue:
                return {"final_response": resume_issue, "failed": True, "completed": False,
                        "failure_reason": "saved_state_mismatch", "error": resume_issue, "messages": agent_history}
            api_message = _wrap_current_message_with_observed_context(self._native_image_run_message(), observed_group_context)
            kwargs = {"conversation_history": agent_history, "task_id": ctx.session_id}
            from gateway.session_api_turn import api_execution
            api = api_execution.get()
            if api is not None and api.get('relay_metadata') and _accepts_keyword(agent.run_conversation, "relay_metadata"):
                kwargs["relay_metadata"] = api['relay_metadata']  # main: api_server forwards body.metadata
            if _accepts_keyword(agent.run_conversation, "turn_author"):
                # Sent on every transport: a provider gating durable writes needs the bot flag in a DM too.
                from gateway.session_ingress import admission_author
                kwargs["turn_author"] = (api.get('turn_author') if api is not None else
                    admission_author.get() or {"id": ctx.source.user_id or None, "name": ctx.source.user_name or None,
                                               "is_bot": bool(getattr(ctx.source, "is_bot", False))})
            if ctx.title_user_message is not None:
                kwargs["title_user_message"] = ctx.title_user_message
            if persist_user_message_override is not None:
                kwargs["persist_user_message"] = persist_user_message_override
            elif observed_group_context:
                kwargs["persist_user_message"] = ctx.message
            if ctx.persist_user_display_kind:
                # Internal self-injected turn: type the persisted user row so UIs render it as a
                # timeline notice, not a user bubble (stripped from provider payloads downstream).
                kwargs["persist_user_display_kind"] = ctx.persist_user_display_kind
            if ctx.persist_user_display_metadata:
                kwargs["persist_user_display_metadata"] = ctx.persist_user_display_metadata
            if ctx.moa_config is not None:
                kwargs["moa_config"] = ctx.moa_config
            from gateway.session_onboarding import arm_onboarding_turn
            prelude = arm_onboarding_turn(agent, self._approval_owner, agent_history)
            if prelude is not None and _accepts_keyword(agent.run_conversation, "prelude"):
                kwargs["prelude"] = prelude
            if persist_user_timestamp_override is not None:
                kwargs["persist_user_timestamp"] = persist_user_timestamp_override
            # The RAW inbound id (not event_message_id, the reply anchor) rides the persisted user
            # turn so a restart-interrupted turn is recorded WITH its id for drain-window dedup.
            if ctx.inbound_message_id is not None:
                kwargs["persist_user_platform_id"] = str(ctx.inbound_message_id)
            from agent.notification_presentation import notification_turn
            from gateway.session_results import execution_result
            captured = execution_result.get()
            before = (getattr(agent, 'session_prompt_tokens', 0) or 0,
                      getattr(agent, 'session_completion_tokens', 0) or 0)
            with notification_turn(agent, muted=ctx.mute_notification_reply, session_id=ctx.session_id or ""):
                from gateway.response_filters import silence_allowed
                # Where a bare marker would become the "needed a reply" warning, the agent ends a
                # marker that follows a written reply on that reply, before transform/persistence.
                agent._silence_marker_needs_reply = not silence_allowed(
                    ctx.persist_user_display_kind, ctx.reply_expected)
                try:
                    result = agent.run_conversation(api_message, **kwargs)
                finally:
                    agent._silence_marker_needs_reply = False
            if captured is not None:
                incoming = max(0, (getattr(agent, 'session_prompt_tokens', 0) or 0) - before[0])
                outgoing = max(0, (getattr(agent, 'session_completion_tokens', 0) or 0) - before[1])
                captured['usage'] = {'input_tokens': incoming, 'output_tokens': outgoing,
                                     'total_tokens': incoming + outgoing}
                _settle_api_runtime_lock(agent, result, captured)
            return result
        finally:
            unregister_gateway_notify(session_key)
            # Cancel pending clarify entries so blocked agent threads don't hang past the end of the
            # run (interrupt, completion, gateway shutdown). Idempotent.
            with suppress(Exception):
                from tools.clarify_gateway import clear_session
                clear_session(session_key)
            reset_current_session_key(token)

    def _finish_stream_consumer(self, result, agent_history, stream_consumer):
        ctx = self._ctx
        # Canonicalize a model-emitted computer-use screenshot path at the common result boundary so
        # the streaming finalizer and the non-streaming delivery path see the same response.
        if isinstance(result, dict) and isinstance(result.get("final_response"), str):
            result["final_response"] = repair_explicit_computer_use_media_paths(
                result["final_response"], result.get("messages", []), history_offset=len(agent_history),
            )
        ctx.result_holder[0] = result
        if stream_consumer is None:
            return
        # Pass final_response as the authoritative finalize payload: it includes post-stream
        # augmentation (verifier footer, explainer) the accumulator never saw. Adopt ONLY a genuinely
        # completed final: interrupt paths return {interrupted: True, completed: False} with a
        # DIAGNOSTIC final_response — adopting it would seal the partial answer over with the
        # diagnostic AND suppress the gateway's own error delivery.
        _final_for_stream = None
        if (
            isinstance(result, dict) and not result.get("failed") and not result.get("interrupted")
            and result.get("completed") is not False
        ):
            fr = result.get("final_response")
            if isinstance(fr, str) and fr.strip() and fr != "(empty)":
                _final_for_stream = fr
        if _final_for_stream is None:
            stream_consumer.finish()
            return
        # Duck-type safe: test doubles / older consumers may expose a zero-arg finish().
        try:
            stream_consumer.finish(_final_for_stream)
        except TypeError:
            stream_consumer.finish()

    def _restore_telegram_thread_id_after_split(self, agent_session_id) -> None:
        """Telegram DM whose source.thread_id was lost in the session split (synthetic/recovered
        event): restore it from the binding so _thread_metadata_for_source yields the right
        message_thread_id instead of the General thread (non-fatal)."""
        ctx = self._ctx
        try:
            # run_sync is off-loop (executor); sync DB is fine.
            binding = self._runner._session_db._db.get_telegram_topic_binding_by_session(session_id=agent_session_id)
            if binding and binding.get("thread_id"):
                ctx.source.thread_id = str(binding["thread_id"])
                logger.debug(
                    "Restored source.thread_id=%s from binding after session split %s → %s",
                    ctx.source.thread_id, ctx.session_id, agent_session_id,
                )
        except Exception:
            logger.debug("Failed to restore thread_id from binding after session split", exc_info=True)

    def _sync_session_after_run(self, agent_history):
        """Sync session_id right after run_conversation(): compression can rotate before a follow-up
        model call fails, and the failure return must still point at the compressed child.
        Returns (compacted_in_place, effective_session_id, effective_history_offset)."""
        ctx = self._ctx
        runner = self._runner
        agent = ctx.agent_holder[0]
        # In-place compaction compacts the transcript WITHOUT rotating the id, so the id-change diff
        # can't see it; compress_context() sets this flag and the gateway re-baselines as for a split.
        compacted_in_place = bool(getattr(agent, "_last_compaction_in_place", False)) if agent else False
        agent_session_id = getattr(agent, 'session_id', ctx.session_id) if agent else ctx.session_id
        session_was_split = bool(agent and ctx.session_key and agent_session_id != ctx.session_id)
        if session_was_split:
            logger.info("Session split detected: %s → %s (compression)", ctx.session_id, agent_session_id)
            entry = runner.session_store._entries.get(ctx.session_key)
            persisted = False
            if entry:
                entry_session_id = getattr(entry, "session_id", None)
                if not ctx._run_still_current():
                    logger.info(
                        "Skipping session split sync for stale run %s — "
                        "generation %s is no longer current",
                        ctx.session_key or "?", ctx.run_generation,
                    )
                elif entry_session_id == agent_session_id:
                    persisted = True
                elif entry_session_id != ctx.session_id:
                    logger.info(
                        "Skipping session split sync for %s because the "
                        "session binding moved from %s to %s before "
                        "compression finished",
                        ctx.session_key or "?", ctx.session_id, entry_session_id,
                    )
                else:
                    entry.session_id = agent_session_id
                    runner.session_store._save()
                    runner.session_store._record_gateway_session_peer(agent_session_id, ctx.session_key, ctx.source)
                    persisted = True
            # Only after this run published its split — a stale /stop→/new predecessor must not
            # mutate routing state.
            if persisted:
                src = ctx.source
                if (
                    getattr(src, "platform", None) == Platform.TELEGRAM and getattr(src, "chat_type", None) == "dm"
                    and getattr(src, "thread_id", None) is None and runner._session_db is not None
                ):
                    self._restore_telegram_thread_id_after_split(agent_session_id)
                runner._sync_telegram_topic_binding(src, entry, reason="agent-run-compression")
        from gateway.session_cli_turn import cli_turn
        if not cli_turn(ctx.inbound_message_id).get("model_override"):
            # A `/model --once` / `/moa` turn ran another route for this admission only: the row keeps
            # the session's model, or a /resume would come back on the one-turn model.
            runner._sync_session_model_from_agent(agent_session_id, agent)
        # history_offset=0 whenever the agent's message list lost the original history prefix
        # (split OR in-place compaction): the returned `messages` is the compacted set, persist all
        # of it; slicing past the pre-compaction length would drop everything.
        offset = 0 if (session_was_split or compacted_in_place) else len(agent_history)
        return compacted_in_place, agent_session_id, offset

    def _load_personal_telegram_settings(self) -> None:
        """Snapshot private Telegram-DM settings and overlay only this turn's config."""
        ctx = self._ctx
        source = ctx.source
        if (getattr(source, "platform", None) != Platform.TELEGRAM
                or getattr(source, "chat_type", None) != "dm"
                or not str(getattr(source, "user_id", "") or "").isdigit()):
            return
        try:
            from gateway.telegram_personal_settings import (
                apply_personal_settings_to_user_config, load_personal_settings,
            )
            home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
            home = home_resolver(source) if callable(home_resolver) else None
            if home is None:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
            settings = ctx.personal_settings
            if not isinstance(settings, dict):
                settings = load_personal_settings(home, source.user_id)
            ctx.personal_settings = settings
            ctx.user_config = apply_personal_settings_to_user_config(ctx.user_config, settings)
        except Exception:
            logger.warning("Could not load personal Telegram settings for this turn", exc_info=True)

    def _combined_ephemeral_prompt(self) -> str:
        """Platform context + YAML channel_prompts hint + channel_overrides system_prompt (or global
        ephemeral) + the gateway ephemeral prompt."""
        ctx = self._ctx
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None:
            return api['settings'].get('ephemeral_system_prompt') or ''
        combined = ctx.context_prompt or ""
        for extra in (
            (ctx.channel_prompt or "").strip(),
            self._runner._get_system_prompt_for_channel(
                ctx.source.platform, ctx.source.chat_id or "", thread_id=getattr(ctx.source, "thread_id", None),
                parent_id=getattr(ctx.source, "parent_chat_id", None),
            ),
        ):
            if extra:
                combined = (combined + "\n\n" + extra).strip()
        if self._search_task_requested:
            combined = (combined + "\n\n" + _SEARCH_TASK_PROMPT).strip()
        if self._deep_research_requested:
            try:
                from gateway.deep_research_sessions import resolve_session_dir
                home_resolver = getattr(self._runner, "_resolve_profile_home_for_source", None)
                home = home_resolver(ctx.source) if callable(home_resolver) else None
                if home is None:
                    from hermes_constants import get_hermes_home
                    home = get_hermes_home()
                session_dir = resolve_session_dir(home, ctx.source.user_id, ctx.deep_research["session_id"])
                research = ctx.deep_research or {}
                depth_value = str(research.get("depth") or "deep").strip().lower()
                depth_guidance = {
                    "standard": "Standard depth: answer the question thoroughly with the core evidence — one full search, read, and verify pass, plus a targeted second pass on the main uncertainties.",
                    "deep": "Deep depth: several search rounds, read widely across independent sources, cross-check every material claim, investigate disagreements, and write a comprehensive report.",
                    "exhaustive": "Exhaustive depth: leave no major subquestion unsearched. Keep running further rounds until new searches stop surfacing new material evidence, then document the saturation point and the remaining gaps.",
                }.get(depth_value, "Deep depth: several search rounds, read widely across independent sources, cross-check every material claim, investigate disagreements, and write a comprehensive report.")
                try:
                    configured_turns = int(research.get("max_turns") or 150)
                except (TypeError, ValueError):
                    configured_turns = 150
                custom_text = str(research.get("custom_instructions") or "").strip()
                custom_block = (
                    "Owner's extra instructions for this session (follow them; they do not override the evidence, file-boundary, or no-consequential-action rules above):\n" + custom_text
                    if custom_text else "No extra instructions were given for this session."
                )
                deep_prompt = _DEEP_RESEARCH_TASK_PROMPT.format(
                    session_dir=session_dir,
                    max_turns=configured_turns,
                    report_format=str(research.get("report_format") or "md").strip().lower(),
                    depth_guidance=depth_guidance,
                    custom_instructions=custom_block,
                )
                deep_prompt += (
                    "\n\nPath mapping: file tools resolve relative names from the verified private session root above; "
                    "pass only relative filenames to them, never `/workspace/...` or an absolute host path. "
                    "The isolated shell alone sees this session directory mounted at `/workspace`; use `/workspace/...` "
                    "only in shell commands. Any other path is blocked by the session file boundary; do not attempt to work around it. "
                    "If a file tool refuses a write, read the file first with read_file using the same relative name and retry; "
                    "never copy an absolute host path out of an error message into a shell command, that path does not exist inside the isolated shell."
                )
                combined = (combined + "\n\n" + deep_prompt).strip()
            except Exception:
                combined = (combined + "\n\nDeep research session storage could not be validated. Do not use shell or file tools; continue only with safe read-only research tools and report the session-storage issue.").strip()
        personal = ctx.personal_settings or {}
        general = str(personal.get("general_instructions") or "").strip()
        if general:
            combined = (combined + "\n\nPrivate Telegram user instructions (apply only to this user's turns; they do not override system or safety rules):\n" + general).strip()
        if self._search_task_requested:
            search_instructions = str(personal.get("search_instructions") or "").strip()
            if search_instructions:
                combined = (combined + "\n\nPrivate Telegram search preferences:\n" + search_instructions).strip()
        if personal:
            if not personal.get("memory_enabled", True):
                combined = (combined + "\n\nPersonal Memory is OFF for this Telegram account. Do not access or write personal memory for this user.").strip()
            if personal.get("self_learning_enabled", True):
                combined = (combined + "\n\nSelf-learning is enabled only for this Telegram account. Any durable personal facts belong only in this account's isolated private-chat memory; never put them in profile-wide memory or another user's history, and follow all memory-write approval rules.").strip()
            else:
                combined = (combined + "\n\nSelf-learning is OFF for this Telegram account. Do not persist or stage autonomous personal-learning changes.").strip()
        return combined

    def _append_auto_media_tags(self, final_response: str, result, agent_history, history_media_paths) -> str:
        """Append MEDIA:<path> tags from tool results (e.g. TTS) that the model's final text omits, so
        extract_media() delivers each file once. Scoped to THIS turn (slice at len(agent_history)) so
        a stale MEDIA: path from an earlier turn never rides a later reply; the history-path dedup is
        the secondary guard — and the sole one when mid-run compression shrank the list."""
        from gateway.run import _collect_auto_append_media_tags
        if "MEDIA:" in final_response:
            return final_response
        # Scan tool results for MEDIA:<path> tags that need to be delivered as native audio/file
        # attachments. The TTS tool embeds MEDIA: tags in its JSON response, but the model's final text
        # reply usually doesn't include them. We collect unique tags from tool results and append any that
        # aren't already present in the final response, so the adapter's extract_media() can find and
        # deliver the files exactly once. Scope the scan to THIS turn's tool results only. ``agent_history``
        # was passed into run_conversation as ``conversation_history``, so the agent's returned ``messages``
        # list is ``agent_history`` followed by the messages produced this turn. Slicing at
        # ``len(agent_history)`` isolates the current turn precisely, so a stale MEDIA: path emitted by a
        # tool several turns earlier (still present in the full message list) can never leak onto a later
        # text-only reply. (Fixes #34608) Path-based deduplication against _history_media_paths (collected
        # before run_conversation) is retained as a secondary guard. It is also the sole guard on the
        # fallback branch taken when mid-run context compression shrinks the message list below the original
        # history length, preserving the compression-safe behaviour of #160.
        media_tags, has_voice_directive = _collect_auto_append_media_tags(
            result.get("messages", []), history_offset=len(agent_history), history_media_paths=history_media_paths,
        )
        if not media_tags:
            return final_response
        unique_tags = (["[[audio_as_voice]]"] if has_voice_directive else []) + list(dict.fromkeys(media_tags))
        return final_response + "\n" + "\n".join(unique_tags)

    def run_sync(self):
        from contextlib import ExitStack
        from gateway.session_policy import bind_turn_workspace, policy_for_source, policy_scope, refused_turn_result
        from gateway.session_api_turn import api_policy_scope
        from gateway.session_authorities import active_authority
        from hermes_state_runtime import RuntimeStoreError
        policy = policy_for_source(self._runner, self._ctx.source)
        with ExitStack() as scope:
            try:
                scope.enter_context(policy_scope(policy, authority=active_authority(self._runner)))
            except RuntimeStoreError as exc:
                result = refused_turn_result(policy, exc)
                if result is None:
                    raise
            else:
                scope.enter_context(api_policy_scope())
                bind_turn_workspace(policy, self._ctx.session_id)
                result = self._run_sync_scoped()
            from gateway.session_results import execution_result
            captured = execution_result.get()
            if captured is not None:
                cli_fields = self.__dict__.pop("_cli_result_fields", None)
                # A classic CLI viewer reads the run_conversation-shaped dict from the receipt; the
                # delivered reply (message.complete text) keeps the gateway's own copy.
                captured['result'] = (
                    {**result, **cli_fields, **({'response_previewed': True} if result.get('response_previewed') else {})}
                    if cli_fields else result
                )
            return result

    def _run_sync_scoped(self):
        """Executor-thread body of the turn; returns the gateway result dict.

        The turn message lives on the shared TurnContext (``ctx.message``) so ``_run_agent_inner`` sees
        every rebind. session_key propagates via contextvars (_set_session_env / set_current_session_key)
        — never os.environ["HERMES_SESSION_KEY"], which would misroute approvals across sessions.
        """
        from gateway.run import (
            _current_max_iterations, _normalize_empty_agent_response,
            _recover_missing_final_response, _sanitize_gateway_final_response,
        )
        ctx = self._ctx
        runner = self._runner
        # Platform.LOCAL ("local") maps to the "cli" hint key the agent understands.
        # session_key is propagated via contextvars in _set_session_env() (_SESSION_KEY) and via
        # set_current_session_key() (_approval_session_key) below — both concurrency-safe and inherited by
        # tool worker threads. We deliberately do NOT write os.environ["HERMES_SESSION_KEY"] here:
        # os.environ is process-global, so concurrent gateway sessions (e.g. two Discord threads) would
        # clobber each other's value, and a tool thread whose contextvar is unset would fall back to
        # os.environ and read the wrong session key — misrouting command-approval prompts to the wrong
        # thread (#24100). The non-gateway surfaces don't depend on this write: CLI and cron bind the
        # session via contextvars (set_current_session_key / session context), and only the TUI slash-worker
        # *subprocess* exports HERMES_SESSION_KEY (from its own --session-key argv, a separate process) — so
        # removing this in-process gateway write does not affect any of them.
        from gateway.session_policy import policy_for_source
        policy = policy_for_source(runner, ctx.source)
        if policy and policy.yolo and ctx.session_key:
            from tools.approval_yolo import apply_launch_yolo
            apply_launch_yolo(ctx.session_key)
        platform_key = policy.platform if policy else ("cli" if ctx.source.platform == Platform.LOCAL else ctx.source.platform.value)
        self._load_personal_telegram_settings()
        combined_ephemeral = self._combined_ephemeral_prompt()
        if policy and policy.skills_prompt:
            combined_ephemeral = "\n\n".join(p for p in (combined_ephemeral, policy.skills_prompt) if p)
        max_iterations = policy.max_turns if policy else _current_max_iterations()
        if self._deep_research_requested:
            try:
                from gateway.deep_research_sessions import DEFAULT_MAX_TURNS, MAX_TURNS_CAP
                max_iterations = min(MAX_TURNS_CAP, max(1, int((ctx.deep_research or {}).get("max_turns", DEFAULT_MAX_TURNS))))
            except (TypeError, ValueError):
                max_iterations = DEFAULT_MAX_TURNS
        from gateway.hosted_room_execution_policy import current_room_execution_policy
        room = current_room_execution_policy()
        if room is not None:
            max_iterations = room.max_iterations
            ctx.enabled_toolsets = list(room.enabled_toolsets)
        try:
            from gateway.provider_sharing import ProviderNotSharedError

            model, runtime_kwargs = runner._resolve_session_agent_runtime(
                source=ctx.source, session_key=ctx.session_key, user_config=ctx.user_config,
                enforce_visibility=True,
            )
            # Stashed by _resolve_session_agent_runtime when the primary's credentials failed and a
            # fallback was resolved before any agent exists (#74349); one-shot per turn.
            pending_fallback_notice = getattr(runner, "_pre_agent_fallback_notice", None)
            runner._pre_agent_fallback_notice = None
            from gateway.session_api_turn import prepare_api_runtime
            model, runtime_kwargs = prepare_api_runtime(model, runtime_kwargs)
            if policy and policy.model and not pending_fallback_notice:
                # The frozen route's model, unless resolution just fell back: the fallback entry's
                # model is the one this agent must send (#112600).
                from gateway.session_authorities import active_authority
                model = policy.current_model(active_authority(runner))
            # The classic CLI's `/model --once` / `/moa`: this admission only, nothing persisted.
            from gateway.session_cli_turn import one_turn_route
            model, runtime_kwargs = one_turn_route(self, policy, model, runtime_kwargs)
            logger.debug(
                "run_agent resolved: model=%s provider=%s session=%s",
                model, runtime_kwargs.get("provider"), ctx.session_key or "",
            )
        except ProviderNotSharedError as exc:
            # A provider added by another user and not shared with this one:
            # the turn never starts, so their credential is never spent.
            logger.warning(
                "Turn blocked: provider %s is not shared with this user (session %s)",
                exc.provider, ctx.session_key or "",
            )
            return {
                "final_response": (
                    f"⚠️ This provider ({exc.provider}) was added by another user and isn't shared with you. "
                    "Ask them to share it from /providers, or switch to one of your own with /model."
                ),
                "messages": [], "api_calls": 0, "tools": [],
            }
        except Exception as exc:
            # Model/credential resolution failed before the turn began; the raw text (URLs, status
            # codes) belongs in the log, and the chat gets the commands that fix it. The result is
            # a FAILED turn: a one-shot client exits non-zero, transcript persistence closes the
            # turn, and an API receipt reports failure, never a completed turn with an apology.
            logger.warning("Model resolution failed for session %s: %s", ctx.session_key or "", exc, exc_info=True)
            from agent.turn_failure_copy import stamp_failure
            text, reason, retryable = _resolution_failure_reply(exc, ctx.source.platform)
            return stamp_failure({"final_response": text, "messages": [], "api_calls": 0, "tools": [],
                                  "failed": True, "completed": False, "error": str(exc)}, reason, retryable)
        from gateway.session_authorities import active_authority
        from gateway.session_worker_construct import session_routing
        pr = session_routing(active_authority(runner), policy) if policy else runner._provider_routing
        reasoning_config = (policy.current_reasoning_config(runner, ctx.session_key) if policy else
            runner._resolve_session_reasoning_config(source=ctx.source, session_key=ctx.session_key, model=model))
        from gateway.session_cli_turn import one_turn_reasoning
        reasoning_config = one_turn_reasoning(self, reasoning_config)  # `/model --once --reasoning X`
        runner._service_tier = (policy.current_service_tier(runner, ctx.session_key) if policy else
            runner._resolve_session_service_tier(source=ctx.source, session_key=ctx.session_key))
        from gateway.session_api_turn import api_execution
        api = api_execution.get()
        if api is not None:
            from gateway.platforms.api_server import _request_reasoning_config, _request_service_tier, _REQUEST_OPTION_MISSING
            requested_reasoning = _request_reasoning_config(api['settings'].get('model_options'))
            if requested_reasoning is not None:
                reasoning_config = requested_reasoning
            tier = _request_service_tier(api['settings'].get('model_options'))
            if tier is not _REQUEST_OPTION_MISSING:
                runner._service_tier = tier
        runner._reasoning_config = reasoning_config
        stream_consumer, stream_delta_cb, interim_cb, want_interim = self._setup_stream_consumer(platform_key)
        turn_route = runner._resolve_turn_agent_config(ctx.message, model, runtime_kwargs)
        agent, reused_cached_agent = self._resolve_turn_agent(
            turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr,
        )
        if pending_fallback_notice:
            # Reuse the in-agent one-shot notice so the pre-agent provider switch is user-visible too.
            agent._pending_fallback_notice = pending_fallback_notice
        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)
        from gateway.run_turn_progress import local_viewer_turn
        local_sinks = None
        if local_viewer_turn(self) and not ctx.mute_notification_reply:
            # Per turn on the shared cached agent: installed here, restored when the run returns.
            from gateway import local_viewer_events
            local_sinks = local_viewer_events.wire(self, agent)
            from gateway.session_usage import TURN_BASE_ATTR
            setattr(agent, TURN_BASE_ATTR, int(getattr(agent, "session_output_tokens", 0) or 0))
        try:
            agent_history, observed_group_context, history_media_paths = self._load_turn_history(agent, reused_cached_agent)
            persist_msg, persist_ts = self._prepare_turn_message(agent_history)
            compressions = getattr(getattr(agent, "context_compressor", None), "compression_count", None)
            from gateway.session_cli_turn import run_classic_cli_turn
            from gateway.session_secret_prompt import secret_prompt_scope
            from gateway.session_sudo_prompt import sudo_prompt_scope
            from gateway.session_vault_prompt import vault_prompt_scope
            with sudo_prompt_scope(self, agent), secret_prompt_scope(self, agent), vault_prompt_scope(self, agent):
                result = run_classic_cli_turn(self, lambda: self._run_conversation_with_approval(
                    agent, agent_history, observed_group_context, persist_msg, persist_ts))
        finally:
            if local_sinks is not None:
                from gateway import local_viewer_events
                local_viewer_events.restore(agent, local_sinks)
                from gateway.session_usage import publish_usage
                publish_usage(self, agent, record=True)  # turn end: the final API call's counters
                from gateway.session_local_inventory import publish_settled_info
                publish_settled_info(self, agent)
        persisted_turn = _persisted_turn(agent, agent_history, result, compressions)
        self._finish_stream_consumer(result, agent_history, stream_consumer)
        if _finite_viewer_turn() and getattr(agent, "_codex_session", None) is not None:
            # The classic `chat -q` process exit bounded the codex app-server child; a finite turn
            # hosted here must too, or every one-shot leaves one running until the idle-TTL sweep.
            # Like a fresh process, the next turn respawns it and resumes the stored thread.
            agent._close_codex_session()
            agent._codex_session_prompt = None
        # The streaming-TTS consumer's finish() runs on the outer loop thread after the executor
        # returns, so early run_sync returns are also finalised.
        # See the outer finally/completion section below. See #60671.
        final_response = result.get("final_response")
        # Actual token counts from the agent instance used for this run.
        agent = ctx.agent_holder[0]
        has_comp = bool(agent) and hasattr(agent, "context_compressor")
        comp = agent.context_compressor if has_comp else None
        usage = {
            "last_prompt_tokens": getattr(comp, "last_prompt_tokens", 0) if has_comp else 0,
            "input_tokens": getattr(agent, "session_prompt_tokens", 0) if has_comp else 0,
            "output_tokens": getattr(agent, "session_completion_tokens", 0) if has_comp else 0,
            "model": getattr(agent, "model", None) if agent else None,
            # Most failed results name no provider; the error card's sign-in / update-key action needs it.
            "provider": getattr(agent, "provider", None) if agent else None,
            "context_length": (getattr(comp, "context_length", 0) or 0) if has_comp else 0,
            # The rest of the `-z --usage-file` ledger (hermes_cli/oneshot._USAGE_KEYS): a finite
            # viewer reads the committed result over `prompt.receipt`, so the projection must keep
            # what the in-process one-shot took straight from `run_conversation`.
            **{key: result.get(key) for key in _LEDGER_PASSTHROUGH_KEYS if key in result},
        }
        compacted_in_place, effective_session_id, history_offset = self._sync_session_after_run(agent_history)
        # failure_reason must survive the empty-response path too (TUI billing, transient-failure
        # persistence). compression_deferred (soft lock-contention defer) is distinct from
        # compression_exhausted so the gateway never auto-resets a session a concurrent compressor is
        # about to shrink.
        common = {
            "messages": result.get("messages", []), "api_calls": result.get("api_calls", 0),
            "failed": result.get("failed", False), "failure_reason": result.get("failure_reason"),
            # Usage-window limit (agent/plan_limits.py): the gateway offers the plan's model as a button.
            "usage_limit": result.get("usage_limit"),
            "partial": result.get("partial", False), "completed": result.get("completed"),
            "interrupted": result.get("interrupted", False), "interrupt_message": result.get("interrupt_message"),
            "error": result.get("error"),
            **{key: result[key] for key in _FAILURE_DESCRIPTOR_KEYS if key in result},
            "compression_exhausted": result.get("compression_exhausted", False),
            "compression_deferred": result.get("compression_deferred", False),
            "tools": ctx.tools_holder[0] or [],
            "history_offset": history_offset, "compacted_in_place": compacted_in_place, "session_id": effective_session_id,
            # Same rule as the direct API path (``_finish_turn_result``): the returned `messages` replaced the
            # transcript, so a stored receipt must keep it whole instead of cutting it to this turn's suffix.
            **({"_compressed": True} if compacted_in_place or effective_session_id != ctx.session_id else {}),
            # The loop's proven current-turn row (``export_current_turn_boundary``) for the same `messages`: the
            # stored receipt cuts there instead of re-guessing from text (multimodal or rewritten input + a nudge).
            **({"current_turn_user_idx": result["current_turn_user_idx"]} if "current_turn_user_idx" in result else {}),
            **usage,
            **({"persisted_turn": persisted_turn} if persisted_turn else {}),
        }
        recovered_response = _recover_missing_final_response(
            result, final_response, history_len=len(agent_history),
        )
        if recovered_response != final_response and recovered_response:
            logger.warning(
                "Agent result had no final_response; recovered %d characters from its final assistant message",
                len(recovered_response),
            )
            final_response = recovered_response
        previewed = result.get("response_previewed", False)
        viewer_meta = {}
        if local_sinks is not None:
            was_delivered = getattr(agent, "_interim_text_was_delivered", None)
            previewed = bool(previewed or (callable(was_delivered) and bool(final_response) and was_delivered(final_response) is True))
            viewer_meta = {
                "local_viewer": True,
                **({"billing_block": result["billing_block"]} if isinstance(result.get("billing_block"), dict) else {}),
                **({"warning": result["warning"]} if isinstance(result.get("warning"), str) and result["warning"] else {}),
            }
        if not final_response:
            final_response = _normalize_empty_agent_response(result, final_response or "", history_len=len(agent_history))
            final_response = _sanitize_gateway_final_response(ctx.source.platform, final_response)
            if not final_response:
                final_response = t("gateway.shared.warn_passthrough", error=result["error"]) if result.get("error") else ""
            # NOTE: deliberately omits agent_persisted/response_reused (and last_reasoning outside a local
            # viewer's turn); the caller defaults agent_persisted differently when the key is absent.
            if local_sinks is not None:
                return {
                    "final_response": final_response, **common, **viewer_meta,
                    "last_reasoning": result.get("last_reasoning"),
                    "response_previewed": previewed,
                    "response_transformed": bool(result.get("response_transformed")),
                }
            return {"final_response": final_response, **common}
        final_response = self._append_auto_media_tags(final_response, result, agent_history, history_media_paths)
        # Auto-titling runs at TURN START (agent/turn_context.py) from the user's message alone, so a
        # failed/interrupted turn is still titled.
        return {
            "final_response": final_response, "last_reasoning": result.get("last_reasoning"), **common, **viewer_meta,
            "response_previewed": previewed,
            "response_transformed": result.get("response_transformed", False),
            "response_reused": result.get("response_reused", False),
            # Lets the persistence block tell whether the codex app-server path self-persisted (it
            # didn't — see codex_runtime.py); default True keeps skip-db for the standard runtime.
            "agent_persisted": result.get("agent_persisted", True),
        }
