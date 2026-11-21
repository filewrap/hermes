"""System-prompt assembly for :class:`AIAgent`.

Built once per session and reused across turns (only context compression
triggers a rebuild) so the upstream prefix cache stays warm.  Three tiers are
joined with ``\\n\\n``: ``stable`` (identity only: SOUL.md / default identity,
user-correction guidance, model identity), ``context`` (caller ``system_message``,
project context files, home workspace bootstrap files, workspace snapshot,
coding brief, platform hints, pinned skills, and the skills/tools
availability listing) and ``volatile`` (memory, USER.md, external memory
provider, timestamp line, Runtime line).  See
``references/system-prompt-invariant.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.delegation_context import owned_kanban_task
from agent.prompt_cache_scope import conversation_id
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY, HERMES_AGENT_HELP_GUIDANCE, HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS,
    KANBAN_GUIDANCE, PLATFORM_HINTS, USER_CORRECTION_GUIDANCE, drain_truncation_warnings,
)
from agent import prompt_builder as _pb
from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
from hermes_constants import get_default_hermes_root, get_hermes_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)
_PLUGIN_SECTION_FRAME_RE = re.compile(
    r"^## Plugin Context: (?P<id>[a-z0-9][a-z0-9._-]{0,127})\n<!-- hermes-plugin-section-chars:(?P<chars>[0-9]{1,4}) -->\n\n",
    re.MULTILINE,
)
def _resolve_platform_hint(agent: Any, platform_key: str, default_hint: str) -> str:
    """Apply the ``platform_hints.<platform>`` config override: ``replace``
    substitutes the default, ``append`` adds text (a bare string is shorthand
    for append). Malformed entries fall back to the unmodified default so bad
    config can never break prompt assembly or leak across platforms."""
    overrides = getattr(agent, "_platform_hint_overrides", None)
    spec = overrides.get(platform_key) if platform_key and isinstance(overrides, dict) else None
    if isinstance(spec, str):
        spec = {"append": spec}
    if not isinstance(spec, dict):
        return default_hint
    replace_text, append_text = (v.strip() if isinstance(v, str) else "" for v in (spec.get("replace"), spec.get("append")))
    base = replace_text or default_hint
    return f"{base}\n\n{append_text}".strip() if append_text else base


_TUI_EMBEDDED_PANE_CLARIFIER = (
    " You're in its embedded terminal pane, beside the GUI chat — the user can "
    "select your output (Option-drag on macOS, Shift-drag elsewhere) and press "
    "Cmd/Ctrl+L to send it to the chat composer."
)


def _tui_embedded_pane_clarifier(hint: str) -> str:
    """Append the desktop embedded-terminal clarifier when ``HERMES_DESKTOP_TERMINAL``
    is set (only the desktop's TUI PTY, never the chat backend). Idempotent."""
    if not hint or _TUI_EMBEDDED_PANE_CLARIFIER in hint or not is_truthy_value(os.getenv("HERMES_DESKTOP_TERMINAL")):
        return hint
    return hint + _TUI_EMBEDDED_PANE_CLARIFIER


def _plugin_session_info(agent: Any) -> dict[str, str]:
    """Return immutable-at-render-time metadata exposed to prompt sections."""
    try:
        cwd = str(resolve_context_cwd() or "")
    except Exception:
        cwd = ""
    info = {k: str(getattr(agent, k, None) or "") for k in ("session_id", "model", "provider", "platform")}
    info.update(profile_name=_active_profile_name(agent, _ambient_plugin_profile_name), cwd=cwd,
                conversation_id=conversation_id(agent))
    # The creating client's self-declared label ("" when none): plugin data only, never prompt text.
    from hermes_cli.plugin_session import session_client
    client = session_client() or {}
    info.update(client=client.get("name", ""), client_version=client.get("version", ""))
    return info


def _ambient_plugin_profile_name() -> str:
    from hermes_cli.profiles import get_active_profile_name
    return str(get_active_profile_name() or "default")


def _active_profile_name(agent: Any, ambient) -> str:
    """Profile name from the agent's OWN home, else *ambient()*; "default" on any
    failure. Ambient resolution misreports on threads that lost the HERMES_HOME
    ContextVar, which is why the agent's home is preferred."""
    try:
        home = _agent_home(agent)
        return _profile_name_for_home(home) if home is not None else ambient()
    except Exception:
        return "default"


def _frozen_plugin_prompt_sections(agent: Any) -> tuple:
    """Render plugin sections once per session and freeze them on the agent.
    A restored ``_cached_system_prompt`` is parsed instead of re-running plugin
    code; a render that raises at a rebuild boundary keeps the previous bytes
    (stashed by ``invalidate_system_prompt``) instead of silently vanishing."""
    if hasattr(agent, "_plugin_system_prompt_sections_snapshot"):
        return agent._plugin_system_prompt_sections_snapshot
    stored_prompt = getattr(agent, "_cached_system_prompt", None)
    if isinstance(stored_prompt, str) and stored_prompt:
        rendered = _restore_plugin_prompt_sections(stored_prompt)
    else:
        try:
            from hermes_cli.plugins import render_system_prompt_sections
            rendered = tuple(render_system_prompt_sections(_plugin_session_info(agent)))
        except Exception as exc:
            rendered = getattr(agent, "_plugin_system_prompt_sections_previous", None)
            if rendered:
                logger.warning("Plugin system prompt sections failed to re-render (%s); keeping the previous frozen sections", exc)
            else:
                logger.warning("Plugin system prompt sections could not be rendered: %s", exc)
                rendered = ()
    agent._plugin_system_prompt_sections_snapshot = rendered
    return rendered


def _restore_plugin_prompt_sections(prompt: str) -> tuple:
    """Recover frozen section bytes from the persisted full prompt.  Only the
    exact canonical container emitted by core is accepted — user/project text
    may resemble a frame."""
    from hermes_cli.plugins import (
        MAX_SYSTEM_PROMPT_SECTION_CHARS, PLUGIN_SECTIONS_END, PLUGIN_SECTIONS_START,
        RenderedPluginSystemPromptSection, format_system_prompt_sections,
    )
    start = prompt.rfind(PLUGIN_SECTIONS_START)
    end = prompt.find(PLUGIN_SECTIONS_END, start + len(PLUGIN_SECTIONS_START)) if start >= 0 else -1
    if end < 0:
        return ()
    after_end = end + len(PLUGIN_SECTIONS_END)
    if not prompt[after_end:].startswith("\n\nConversation started:"):
        return ()
    framed = prompt[start:after_end]
    restored = []
    for match in _PLUGIN_SECTION_FRAME_RE.finditer(framed):
        content_len = int(match.group("chars"))
        content = framed[match.end() : match.end() + content_len]
        if content_len > MAX_SYSTEM_PROMPT_SECTION_CHARS or len(content) != content_len:
            continue
        restored.append(RenderedPluginSystemPromptSection(id=match.group("id"), content=content,
                                                          position="after_memory", plugin="persisted-prompt"))
    return tuple(restored) if format_system_prompt_sections(restored) == framed else ()


def restore_plugin_prompt_sections(agent: Any, prompt: str) -> None:
    """Seed a resumed agent's frozen snapshot from persisted prompt bytes."""
    agent._plugin_system_prompt_sections_snapshot = _restore_plugin_prompt_sections(prompt)


def _plugin_section_blocks(sections: tuple, position: str) -> list[str]:
    from hermes_cli.plugins import format_system_prompt_sections
    block = format_system_prompt_sections([s for s in sections if s.position == position])
    return [block] if block else []


def _session_start_like(agent: Any, now: Any) -> Any:
    """Best-known conversation start time, or ``now`` as a fallback.
    ``Conversation started:`` must be byte-stable across rebuilds (compression,
    resume, fresh gateway turns), so prefer immutable sources in order: the
    lineage-root session id's embedded stamp (compaction rotates ids, each with
    its own mint time), the current session id's stamp, ``agent.session_start``,
    then ``now``.  Stamps are box-local wall-clock: attach that zone first, then
    convert to ``now``'s zone so the date matches the per-turn clock.

    0. the LINEAGE-ROOT session id's embedded timestamp — compaction can rotate the session id, and each
    rotated id embeds its OWN mint time, so after months of compactions rung 1 alone would quietly re-birth
    the conversation at its latest rotation. Walking to the lineage root (same walk as
    ``_conversation_root_id``) recovers the ORIGINAL birth stamp — a Bot Mode forever-chat keeps knowing
    when it was first born, across every compaction (maintainer-directed, #98426); 1. the timestamp embedded
    in ``session_id`` (``YYYYMMDD_HHMMSS_...``) — immutable for the life of the session, so the line is
    byte-stable across every rebuild boundary (preserving prefix-cache KV); 2. 3. ``now`` (initial/legacy
    build without either).
    """
    from datetime import datetime
    def _to_display_tz(dt: Any) -> Any:
        if dt.tzinfo is None:
            try:
                # The offset in force at dt, not today's: a stamp from the other DST half differs by an hour.
                dt = dt.astimezone()
            except (ValueError, OSError):
                pass
        if getattr(now, "tzinfo", None) is not None and dt.tzinfo is not None:
            try:
                dt = dt.astimezone(now.tzinfo)
            except (ValueError, OSError):
                pass
        return dt
    session_id = getattr(agent, "session_id", None)
    db = getattr(agent, "_session_db", None)
    try:
        root_id = db.get_conversation_root(session_id) if db is not None and isinstance(session_id, str) and session_id else None
    except Exception:
        root_id = None
    for candidate in (root_id, session_id):
        m = re.match(r"^(\d{8})_(\d{6})", candidate) if isinstance(candidate, str) else None
        if m:
            try:
                return _to_display_tz(datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S"))
            except ValueError:
                pass
    session_start = getattr(agent, "session_start", None)
    return _to_display_tz(session_start) if hasattr(session_start, "astimezone") else now


def _agent_home(agent: Any) -> Optional[Path]:
    """The agent's OWN profile home, or None to use ambient resolution.
    A bound HERMES_HOME ContextVar override wins (the gateway multiplexes
    profiles over one shared session DB and binds the home per turn); else the
    parent of ``_session_db.db_path`` — ground truth on threads that lost the
    ContextVar, where ambient resolution would leak the launch profile.

    1. Surfaces that multiplex several profiles over ONE shared session DB (the messaging gateway:
    ``gateway/run.py`` hands every agent the launch-home ``state.db`` and binds the profile home per turn
    via ``_profile_runtime_scope`` + ``copy_context``) would otherwise have the db-derived launch home STOMP
    the correctly-bound profile — inverting the leak this helper exists to fix (found by @kshitijk4poor's
    post-merge probe on #86313). 2. Fallback: the home containing the agent's ``_session_db.db_path``
    (``<home>/state.db``) — ground truth on threads that lost the ContextVar (ContextVars don't propagate
    into ``threading.Thread``), where the unbound build previously fell back to the launch home and leaked
    the default profile's skills/identity into a bot prompt.
    """
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:
        pass
    try:
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        return Path(db_path).parent if db_path else None
    except Exception:
        return None


def _agent_skills_dir(agent: Any) -> Optional[Path]:
    """The agent's own ``<home>/skills`` dir, or None to use ambient home."""
    home = _agent_home(agent)
    return home / "skills" if home is not None else None


def _profile_name_for_home(home: Path) -> str:
    """``<root>/profiles/X`` -> ``"X"``; anything else -> ``"default"``.
    Uses ``get_default_hermes_root()`` (NOT ``get_hermes_home()``): on a bound
    profile session the ambient home IS the profile dir, so every profile
    would misreport as "default"."""
    try:
        from hermes_constants import get_default_hermes_root
        rel = home.resolve().relative_to((get_default_hermes_root() / "profiles").resolve())
        return rel.parts[0] if rel.parts else "default"
    except (ValueError, OSError):
        return "default"


def _kanban_part(agent: Any) -> Optional[str]:
    """Kanban worker protocol for a dispatcher-owned task, if any. This is task
    context, not identity: it rides the context tier, never the stable tier.
    Gating matches the old _tool_guidance_block kanban branch (agent_init.py
    resolves ``_kanban_worker_guidance`` once at __init__); the fallback covers
    paths that never ran that init step."""
    _kanban_guidance = getattr(agent, "_kanban_worker_guidance", None)
    if _kanban_guidance is None and "kanban_show" in (agent.valid_tool_names or set()) and owned_kanban_task():
        _kanban_guidance = KANBAN_GUIDANCE
    return _kanban_guidance or None


def _skills_prompt(agent: Any, context_token_budget: Optional[int] = None) -> str:
    """Skills index (empty without skills tools).  Focus mode demotes non-coding
    categories to names-only — never hidden, every name stays visible. The catalog
    compacts to ``min(SKILLS_CATALOG_MAX_CHARS, floor(context_token_budget / 5))`` chars
    when a budget is given; None still applies the fixed budget."""
    if not any(name in agent.valid_tool_names for name in ['skills_list', 'skill_view', 'skill_manage']):
        return ""
    import model_tools
    avail_toolsets = {model_tools.get_toolset_for_tool(tool_name) for tool_name in agent.valid_tool_names} - {None, ""}
    try:
        from agent.coding_context import coding_compact_skill_categories
        _compact_cats = coding_compact_skill_categories(platform=agent.platform, cwd=resolve_context_cwd())
    except Exception:
        _compact_cats = frozenset()
    return _pb.build_skills_system_prompt(available_tools=agent.valid_tool_names, available_toolsets=avail_toolsets,
                                         compact_categories=_compact_cats or None, skills_dir_override=_agent_skills_dir(agent),
                                         context_token_budget=context_token_budget)


def _auto_load_parts(agent: Any) -> list[str]:
    """``skills.auto_load`` blocks, resolved once per agent lifecycle (config, skill files and
    HERMES_IGNORE_RULES are read on the first build only) so the prompt stays byte-stable
    across model switches, compression and static-prefix restoration.

    Same gate as ``_skills_prompt``: nothing without the skills toolset, and nothing for agents that skip
    context files (delegate children, curator/review forks, gateway hygiene agents) — pinned skills are
    operator guidance for the user's session, not payload for every internal fork."""
    if getattr(agent, "skip_context_files", False) or not any(
            name in agent.valid_tool_names for name in ("skills_list", "skill_view", "skill_manage")):
        return []
    if not getattr(agent, "_auto_load_skills_resolved", False):
        result: tuple[str, list[str], list[str]] = ("", [], [])
        try:
            if not is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")):
                from agent.skill_commands import build_auto_load_prompt
                result = build_auto_load_prompt(task_id=getattr(agent, "session_id", None), home_override=_agent_home(agent))
            if result[2]:
                logger.warning("skills.auto_load: skill(s) not found or disabled, skipped: %s", ", ".join(result[2]))
        except Exception:
            logger.debug("skills.auto_load: injection skipped", exc_info=True)  # config errors never block session start
        agent._auto_load_skills_result = result
        agent._auto_load_skills_resolved = True
    prompt = agent._auto_load_skills_result[0]
    return [prompt] if prompt else []


def _bot_mode_parts(agent: Any) -> list[str]:
    """Bot Mode teammate protocol — only in a bot's canonical "Bot Chat" session.
    Marks the prompt timeless (the volatile date line is dropped) since a birth
    date pinned in a months-long session is misinformation."""
    parts: list[str] = []
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE, epoch_line, get_bot_mode_protocol_section
        _title = str(getattr(agent, "_session_title_hint", "") or "").strip()
        if not _title:
            _sdb = getattr(agent, "_session_db", None)
            _sid = getattr(agent, "session_id", None)
            _title = str((_sdb.get_session_title(_sid) if (_sdb and _sid) else None) or "").strip()
        _bot_section = get_bot_mode_protocol_section(_agent_home(agent)) if _title == BOT_CHAT_TITLE else None
        if _bot_section:
            parts.append(_bot_section)
            # Capability epoch lets the restore path rebuild ONCE per
            # user-initiated capability change in an eternal session.
            parts.append(epoch_line(_agent_home(agent)))
            agent._bot_chat_timeless_prompt = True
    except Exception:
        pass
    return parts


def _ambient_file_safety_profile_name() -> str:
    from agent.file_safety import _resolve_active_profile_name
    return _resolve_active_profile_name()


def _active_profile_line(agent: Any) -> str:
    """Name the running profile so the agent doesn't conflate ``~/.hermes/skills``
    (default) with ``~/.hermes/profiles/<active>/skills``.  Resolved from the
    agent's OWN home first (a build thread that lost the ContextVar would
    otherwise print "default" for a bot profile)."""
    _agent_home_path = _agent_home(agent)
    active_profile = _active_profile_name(agent, _ambient_file_safety_profile_name)
    if active_profile == "default":
        # With an explicit agent home, the default profile's data lives at the
        # ROOT (get_hermes_home() on a bound profile session is the PROFILE dir).
        # Without one, keep the ambient (patchable) resolution byte-identical.
        _root_str = str(get_default_hermes_root() if _agent_home_path is not None else get_hermes_home())
        return (
            "Active Hermes profile: default. Other profiles (if any) live "
            "under " + _root_str + "/profiles/<name>/. Each profile has its own "
            "skills/, plugins/, cron/, and memories/ that affect a different "
            "session than this one. Do not modify another profile's "
            "skills/plugins/cron/memories unless the user explicitly directs "
            "you to."
        )
    # A non-default name is only returned when the resolved home is ALREADY
    # <root>/profiles/<name>, so the profile home is the session home itself.
    profile_home = str(_agent_home_path) if _agent_home_path is not None else str(get_hermes_home())
    # A non-default name is only ever returned when the resolved home is ALREADY <root>/profiles/<name> —
    # that is exactly how both _profile_name_for_home() and _resolve_active_profile_name() derive it. So the
    # profile home is the session home itself; appending /profiles/<name> again doubled it (#72894). The
    # default profile's data sits at the ROOT (get_default_hermes_root()), which in ambient profile mode is
    # NOT get_hermes_home().
    default_root = get_default_hermes_root()
    return (
        f"Active Hermes profile: {active_profile}. This session reads "
        f"and writes {profile_home}/. The default "
        f"profile's data lives at {default_root}/skills/, {default_root}/plugins/, "
        f"{default_root}/cron/, {default_root}/memories/ — those belong to a "
        f"different session run from a different shell. Do NOT modify "
        f"another profile's skills/plugins/cron/memories unless the user "
        f"explicitly directs you to."
    )


def _default_platform_hint(platform_key: str, tool_names: Any = ()) -> str:
    """Built-in hint, else the plugin adapter's ``platform_hint`` plus its ``platform_hint_tool_notes`` for
    tools in *tool_names* (the session's toolset), else ``""``."""
    hint = PLATFORM_HINTS.get(platform_key, "")
    if not hint and platform_key:
        try:
            from gateway.platform_registry import platform_registry
            _entry = platform_registry.get(platform_key)
            if _entry is None:
                # A prompt can be built before anything ran plugin discovery (direct builders,
                # one-shot callers); the hint lives on the plugin's entry, so discover (idempotent).
                from hermes_cli.plugins import discover_plugins
                discover_plugins()
                _entry = platform_registry.get(platform_key)
            hint = (_entry and _entry.platform_hint) or ""
            _notes = [note for tool, note in ((_entry and _entry.platform_hint_tool_notes) or {}).items()
                      if tool in (tool_names or ())]
            if hint and _notes:
                hint = " ".join([hint.rstrip(), *_notes])
        except Exception:
            pass
    return hint


def _cron_delivery_hint(agent: Any) -> str:
    """The destination channel's hint (default + its ``platform_hints`` override) for a cron agent.

    A cron agent runs as platform ``cron`` but its final response lands on the job's ``deliver``
    channel, so without this the model never learns that MEDIA: tags become Slack/Telegram
    attachments or that tables do not render there — and a user's ``platform_hints.slack.append``
    never reached scheduled jobs at all. The scheduler publishes the primary auto-deliver target
    into the session ContextVar before the agent runs (same seam ``send_message`` routes by).
    """
    from gateway.session_context import get_session_env
    deliver_key = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").lower().strip()
    if not deliver_key or deliver_key == "cron":
        return ""
    hint = _resolve_platform_hint(
        agent, deliver_key, _default_platform_hint(deliver_key, getattr(agent, "valid_tool_names", ())))
    return f"Delivery destination ({deliver_key}): {hint}" if hint else ""


def platform_hint(agent: Any) -> str:
    """Built-in/plugin platform hint + config
    override + desktop TUI clarifier; cron agents also carry their delivery channel's hint."""
    platform_key = (agent.platform or "").lower().strip()
    _effective_hint = _resolve_platform_hint(
        agent, platform_key, _default_platform_hint(platform_key, getattr(agent, "valid_tool_names", ())))
    if platform_key == "tui" and _effective_hint:
        _effective_hint = _tui_embedded_pane_clarifier(_effective_hint)
    if platform_key == "cron":
        _delivery = _cron_delivery_hint(agent)
        if _delivery:
            _effective_hint = f"{_effective_hint}\n\n{_delivery}".strip()
    return _effective_hint


def _zone_bits(now: Any, tz: Any) -> list[str]:
    """IANA key, abbreviation (if different) and UTC offset — all constant for
    the day, so the byte-stable date line stays cacheable."""
    _iana = getattr(tz, "key", None)
    from hermes_time import safe_strftime
    _abbrev = safe_strftime(now, "%Z")
    _offset = safe_strftime(now, "%z")  # '-0400' -> 'UTC-04:00'
    bits = [_iana] if _iana else []
    if _abbrev and _abbrev != _iana:
        bits.append(_abbrev)
    if _offset:
        bits.append(f"UTC{_offset[:3]}:{_offset[3:]}")
    return bits


def _timestamp_line(agent: Any) -> str:
    """Date-only so the prompt is byte-stable for the day; zone + offset so
    tools needn't guess EST vs EDT. Long-lived sessions get an "as of" line on
    rebuild days (the cache prefix is already invalidated at that boundary)."""
    from hermes_time import get_timezone as _hermes_tz, now as _hermes_now, safe_strftime
    now = _hermes_now()
    _bits = _zone_bits(now, _hermes_tz())
    _zone_suffix = f" ({', '.join(_bits)})" if _bits else ""
    _start = _session_start_like(agent, now)
    timestamp_line = f"Conversation started: {safe_strftime(_start, '%A, %B %d, %Y')}{_zone_suffix}"
    # Second line (maintainer design, salvaging #96224's anchor): long-lived sessions — Bot Mode
    # forever-chats, messenger channels people never close — span many days and many compactions. A lone
    # birth date leads the model to believe it is still living in that old day. The prompt is rebuilt at
    # every compaction boundary, so stamp the rebuild day too: 'started' stays anchored and byte-stable, 'as
    # of' refreshes exactly when the cache prefix is already being invalidated (compaction), so the added
    # line costs no extra cache churn. Same-day sessions skip the second line entirely — nothing to correct,
    # and the single-line shape stays byte-identical for the day (prefix-cache safe).
    if now.strftime("%Y%m%d") != _start.strftime("%Y%m%d"):
        timestamp_line += (f"\nToday's date (as of the last context rebuild): {safe_strftime(now, '%A, %B %d, %Y')} "
                           "— trust this over the start date for what day it is now; query tools for exact time.")
    if getattr(agent, "_bot_chat_timeless_prompt", False):
        timestamp_line = f"Timezone: {', '.join(_bits)}" if _bits else ""
    trailer = (("Session ID", agent.session_id if agent.pass_session_id else None), ("Model", agent.model),
               ("Provider", agent.provider), ("Platform", agent.platform))
    return timestamp_line + "".join(f"\n{label}: {value}" for label, value in trailer if value)


def _memory_parts(agent: Any) -> list[str]:
    """Built-in memory/USER.md blocks plus the external provider block (gated on
    the same check ``inject_memory_provider_tools`` uses, so we never advertise
    tools the toolset config gated off).

    Turn 1 of a session carries memory GUIDANCE only (the prompt
    names the recall tools, it never preloads the files). Retrieval on later turns
    runs through ``memory_search`` / ``memory_get`` and per-turn prefetch, and a
    post-compression rebuild (turn > 1) restores the full blocks from disk."""
    from agent.memory_model import is_first_memory_turn, memory_guidance_block

    parts: list[str] = []
    if agent._memory_store:
        if is_first_memory_turn(agent):
            guidance = memory_guidance_block(agent)
            if guidance:
                parts.append(guidance)
        else:
            for enabled, kind in ((agent._memory_enabled, "memory"), (agent._user_profile_enabled, "user")):
                block = agent._memory_store.format_for_system_prompt(kind) if enabled else None
                if block:
                    parts.append(block)
    # External memory provider system prompt block (additive to built-in). Gated on the same check
    # ``inject_memory_provider_tools`` uses so we never advertise provider tools that the agent's toolset
    # configuration has already gated off (#81014).
    if agent._memory_manager:
        try:
            from agent.memory_manager_tools import memory_provider_tools_exposed as _mem_exposed
        except Exception:
            _mem_exposed = None
        if _mem_exposed is None or _mem_exposed(agent):
            try:
                _ext_mem_block = agent._memory_manager.build_system_prompt()
            except Exception:
                _ext_mem_block = None
            if _ext_mem_block:
                parts.append(_ext_mem_block)
    return parts


def _identity_parts(agent: Any, ctx_len: Optional[int]) -> tuple[list[str], bool]:
    """SOUL.md (primary identity; cron keeps the persona while skipping cwd
    instructions, scoped to the agent's OWN home) or the default identity.
    Returns ``(parts, soul_loaded)``."""
    wants_soul = agent.load_soul_identity or not agent.skip_context_files
    _soul_content = _pb.load_soul_md(ctx_len, home_override=_agent_home(agent)) if wants_soul else None
    return ([_soul_content], True) if _soul_content else ([DEFAULT_AGENT_IDENTITY], False)


def _first_sentence(text: str) -> str:
    """First sentence of *text*, flattened to one line and clipped at a word
    boundary — the one-line purpose for the tool-availability listing. A
    terminator only ends the sentence when its word is longer than one
    character, so ``e.g.`` / ``v1.2`` / initials do not cut the purpose short."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    for match in re.finditer(r"[.!?](?:\s|$)", flat):
        end = match.start()
        start = max(flat.rfind(" ", 0, end), flat.rfind("\n", 0, end)) + 1
        token = flat[start:end].rsplit(".", 1)[-1]
        if match.group(0)[0] in "!?" or len(token) > 1:
            flat = flat[: end + 1]
            break
    else:
        if flat[-1] not in ".!?…":
            flat += "."
    if len(flat) > 240:
        flat = flat[:240].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return flat


def _tools_availability_block(agent: Any) -> str:
    """Plain listing of the tools in this session's schema: name + one-line
    purpose each. Informational only — it mirrors ``agent.tools`` exactly, so
    it can never advertise a tool the model cannot call."""
    rows = []
    for tool in getattr(agent, "tools", None) or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        purpose = _first_sentence(str(fn.get("description") or ""))
        rows.append(f"- {name}: {purpose}" if purpose else f"- {name}")
    if not rows:
        return ""
    return (
        "## Available tools\n"
        "Tools available in this session — each entry names the tool and what it is for.\n"
        + "\n".join(rows)
    )


def _availability_parts(agent: Any, context_token_budget: Optional[int] = None) -> list[tuple[str, str]]:
    """Skills index + tool listing + Hermes-docs pointer + kanban protocol, for
    the context tier. Informational text only: names, short descriptions, and
    what each thing is for — no load-or-else instruction, no limits. The skills
    index keeps its lazy-loading mechanism (skill_view); only the wording and
    the injection tier changed. ``(key, text)`` pairs so the prompt ceiling can
    cut each block by priority (the tools listing, redundant with the schemas,
    goes first)."""
    parts: list[tuple[str, str]] = []
    skills_prompt = _skills_prompt(agent, context_token_budget=context_token_budget)
    if skills_prompt:
        parts.append(("skills_index", skills_prompt))
    # The skill_view() pointer dangles without skill tools OR without the
    # hermes-agent skill installed, so the variant is chosen after the skills
    # index is built (same check the old stable-tier slot used).
    if "skill_view" in (agent.valid_tool_names or set()) and "<name>hermes-agent</name>" in skills_prompt:
        parts.append(("help_guidance", HERMES_AGENT_HELP_GUIDANCE))
    elif agent.valid_tool_names:
        parts.append(("help_guidance", HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS))
    tools_block = _tools_availability_block(agent)
    if tools_block:
        parts.append(("tools_availability", tools_block))
    kanban = _kanban_part(agent)
    if kanban:
        parts.append(("kanban", kanban))
    return parts


def _alibaba_identity_part(agent: Any) -> list[str]:
    """Alibaba Coding Plan always reports "glm-4.7" as the model name; inject
    the real identity so the agent can answer correctly."""
    if agent.provider != "alibaba":
        return []
    _model_short = agent.model.rsplit("/", 1)[-1]
    return [
        f"You are powered by the model named {_model_short}. "
        f"The exact model ID is {agent.model}. "
        f"When asked what model you are, always answer based on this information, "
        f"not on any model name returned by the API."
    ]


def _workspace_pin_key() -> str:
    """The directory the workspace probe inspects, which is also the prompt's ``Current working
    directory``: a build with no cwd bound (launch dir) and a later one binding that same dir
    (TUI ``/compress``) are one workspace, not two."""
    try:
        return str(resolve_context_cwd() or resolve_agent_cwd())
    except OSError:  # deleted cwd
        return ""


def _persisted_workspace_block(prompt: str, key: str) -> Optional[str]:
    """The workspace snapshot inside ``prompt`` taken for ``key`` (its ``- Root:`` is ``key`` or an
    ancestor); "" when the prompt has none; None when it has one for another root."""
    from agent.coding_context import WORKSPACE_BLOCK_HEADER
    head = f"\n\n{WORKSPACE_BLOCK_HEADER}\n- Root: "
    start = prompt.find(head)
    if start < 0:
        return ""
    cwd = Path(key).resolve()
    while start >= 0:
        block = prompt[start + 2:].split("\n\n", 1)[0]
        root = Path(block.split("\n", 2)[1][len("- Root: "):]).resolve()
        if root == cwd or root in cwd.parents:
            return block
        start = prompt.find(head, start + 2)
    return None


def _session_prompt(agent: Any) -> Optional[str]:
    """Prompt bytes this session already sends: the cached copy, else its persisted row."""
    cached = getattr(agent, "_cached_system_prompt", None)
    if isinstance(cached, str) and cached:
        return cached
    db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
    if db is None or not isinstance(session_id, str) or not session_id:
        return None
    try:
        row = db.get_session(session_id)
    except Exception:
        logger.debug("workspace snapshot: session row read failed (session=%s)", session_id, exc_info=True)
        return None
    prompt = row.get("system_prompt") if isinstance(row, dict) else None
    return prompt if isinstance(prompt, str) and prompt else None


def _seed_workspace_pin(agent: Any, key: str) -> None:
    """Pin the snapshot the session's existing prompt already carries.  An agent that did not
    build those bytes (resumed, or a fresh gateway/TUI agent whose first act is ``/compress``)
    would otherwise re-probe git at its first rebuild and rewrite the prompt for any repo that
    moved since session start.  Only a snapshot provably taken in this cwd is adopted."""
    from agent.surface_switch import runtime_host_value
    prompt = _session_prompt(agent)
    if not prompt:
        return
    stored_cwd = runtime_host_value(prompt, "Current working directory")
    if stored_cwd and stored_cwd != key:
        return
    block = _persisted_workspace_block(prompt, key)
    # Only a real snapshot is adopted: a prompt without one (built on a surface without the
    # coding posture, or with tools off) leaves the pin open so this build captures one.
    if block:
        agent._frozen_workspace_snapshot = (key, block)


def _coding_parts(agent: Any) -> tuple[list[str], list[str], list[str]]:
    """``(prefix, workspace, trailing)`` coding-posture blocks; all empty
    without tools or when probing fails (it must never block prompt build).

    The workspace block is a live git probe after project context, ahead of the whole
    volatile band; re-probing at the compaction rebuild re-emits different bytes for any
    repo that moved and defeats the keep-prompt fast path.  So the bytes are pinned per
    session on the agent, keyed by the probed cwd (a gateway serves many cwds), seeded from
    the session's existing prompt when this agent did not build it, and replayed on
    rebuilds; ``reset_session_state`` drops the pin at a session boundary.
    """
    try:
        from agent.coding_context import coding_system_prompt_parts
        if not agent.valid_tool_names:
            return [], [], []
        cwd = resolve_context_cwd()
        cwd_key = _workspace_pin_key()
        if getattr(agent, "_frozen_workspace_snapshot", None) is None:
            _seed_workspace_pin(agent, cwd_key)
        pinned = getattr(agent, "_frozen_workspace_snapshot", None)
        # "" is a real pinned value (no workspace here) — only a cwd mismatch re-probes.
        replay = pinned[1] if pinned is not None and pinned[0] == cwd_key else None
        parts = coding_system_prompt_parts(platform=agent.platform, cwd=cwd, model=agent.model,
                                           valid_tool_names=agent.valid_tool_names, workspace_block=replay)
        if replay is None:
            agent._frozen_workspace_snapshot = (cwd_key, parts[1][0] if parts[1] else "")
        return parts
    except Exception:
        pass
    return [], [], []


def _post_workspace_parts(agent: Any) -> list[tuple[str, str]]:
    """Blocks that follow the worktree-specific context: environment probe
    (config.yaml agent.environment_probe; one line, nothing when clean, skipped
    for remote backends), bot-mode protocol, platform hint. ``(key, text)``
    pairs so the prompt ceiling can cut each block by priority."""
    parts: list[tuple[str, str]] = []
    if getattr(agent, "_environment_probe", True):
        try:
            from tools.env_probe import get_environment_probe_line
            if (line := get_environment_probe_line()):
                parts.append(("environment_probe", line))
        except Exception:
            pass  # Probe failure must never block prompt build.
    if getattr(agent, "_bot_mode_protocol", True):
        parts.extend(("bot_mode", p) for p in _bot_mode_parts(agent))
    if (hint := platform_hint(agent)):
        parts.append(("platform_hint", hint))
    return parts


def _context_files_part(agent: Any, ctx_len: Optional[int], soul_loaded: bool) -> list[str]:
    """Project context files (AGENTS.md etc.) for the context tier. TERMINAL_CWD
    when set (gateway); None lets discovery fall back to the launch dir.  The
    install-tree fallback is only legitimate for cli/tui where the launch dir
    IS the user's shell cwd. Desktop launch artifacts skip the session cwd but
    still honor the profile-scoped TERMINAL_CWD; without one, the fallback guard
    can reject Hermes's bundled AGENTS.md."""
    if agent.skip_context_files:
        return []
    launch_artifact = getattr(agent, "_context_cwd_is_launch_artifact", False)
    cwd = resolve_context_cwd(include_session_override=not launch_artifact)
    return [_pb.build_context_files_prompt(
        cwd=cwd, skip_soul=soul_loaded, context_length=ctx_len,
        allow_install_tree_fallback=agent.platform in ("cli", "tui"), home_override=_agent_home(agent))]


def _bootstrap_files_part(agent: Any) -> list[str]:
    """Home workspace bootstrap files (AGENTS.md / IDENTITY.md / USER.md /
    BOOTSTRAP.md) for the context tier, in canonical order with ``[MISSING]``
    markers and the 20k/60k caps (``hermes_cli.workspace_bootstrap``). SOUL.md
    is not repeated here — it already rides the stable tier as the identity
    slot — and MEMORY.md is owned by the memory system, not this section.
    Same gates as the cwd context files: skipped for agents that skip context
    files and for safe workers."""
    if agent.skip_context_files:
        return []
    from agent.safe_worker_policy import safe_worker_enabled

    if safe_worker_enabled():
        return []
    from hermes_cli.workspace_bootstrap import build_workspace_bootstrap_prompt

    home = _agent_home(agent) or get_hermes_home()
    section = build_workspace_bootstrap_prompt(home)
    return [section] if section else []


def _runtime_line(agent: Any) -> str:
    """``Runtime: name=... | session=... | model=... | provider=... | platform=...``
    — the only place the IDENTITY.md identity surfaces. The name comes from
    IDENTITY.md's ``Name`` field (``resolve_runtime_agent_name``: whitespace-
    collapsed, capped at 128 chars, no default agent name anywhere — absent or
    blank means no ``name=`` pair at all); the remaining pairs are the session's
    own runtime facts. Truthy fields only."""
    from hermes_cli.workspace_bootstrap import load_agent_identity, resolve_runtime_agent_name

    home = _agent_home(agent) or get_hermes_home()
    name = resolve_runtime_agent_name(load_agent_identity(home))
    fields = (
        ("name", name),
        ("session", agent.session_id if agent.pass_session_id else None),
        ("model", agent.model),
        ("provider", agent.provider),
        ("platform", agent.platform),
    )
    rendered = " | ".join(
        f"{key}={str(value).strip()}" for key, value in fields if value and str(value).strip()
    )
    return f"Runtime: {rendered}" if rendered else ""


# Total ceiling for the built system prompt (chars, ~12K tokens). Per-file and
# per-section caps bound the parts, but nothing bounded the SUM: a home with a
# big skills catalog, big context files, auto-loaded skills and a long channel
# prompt built a ~466K-char prompt (~116K tokens) before the first model call.
# The ceiling is a budget on prompt construction only: when the assembled
# prompt exceeds it, the lowest-priority sections shrink first — never refuse
# or fail the turn — and the cut is a pure function of the parts, so the prompt
# stays byte-stable within the conversation. Everything cut stays reachable:
# context files via read_file, skills via skills_list / skill_view, deferred
# tools via tool_search. Identity (stable tier) and the small volatile lines
# are never cut.
SYSTEM_PROMPT_MAX_CHARS = 48_000


class _PromptSection:
    """One ceiling-managed block of the assembled prompt. ``priority`` None
    means never cut (identity, timestamp, runtime line); otherwise lower is
    cut first. ``mode``: ``drop`` (remove whole), ``head_tail`` (keep head +
    tail with a marker), ``skills`` (whole ``<skill>`` blocks). ``floor`` is
    the chars kept when the section is the last thing standing between the
    prompt and the ceiling."""

    __slots__ = ("floor", "key", "mode", "priority", "text", "tier")

    def __init__(self, tier: str, key: str, text: str, priority: Optional[int] = None,
                 mode: str = "drop", floor: int = 0):
        self.tier, self.key, self.text = tier, key, text
        self.priority, self.mode, self.floor = priority, mode, floor


# Cut order (lower first) — what the ceiling sheds, and in what order.
_CEILING_PRIORITY: dict[str, tuple[int, str, int]] = {
    # (priority, mode, floor chars)
    "tools_availability": (1, "drop", 0),       # redundant with the tool schemas
    "auto_load": (2, "drop", 0),                # full skill bodies; skill_view reloads them
    "help_guidance": (3, "drop", 0),            # docs pointer; the hermes-agent skill says it
    "environment_probe": (4, "drop", 0),        # one probe line
    "workspace_snapshot": (5, "drop", 0),       # git snapshot; re-probeable via terminal
    "plugin_sections": (6, "drop", 0),          # plugin guidance, individually capped
    "context_files": (7, "head_tail", 2_000),   # project instructions; read_file recovers
    "bootstrap_files": (8, "head_tail", 2_000),  # home bootstrap files; read_file recovers
    "platform_hint": (9, "head_tail", 1_000),   # platform rendering rules
    "coding_prefix": (10, "head_tail", 1_000),
    "coding_trailing": (10, "head_tail", 500),
    "bot_mode": (10, "head_tail", 500),
    "kanban": (10, "head_tail", 500),
    "skills_index": (11, "skills", 1_000),      # names shrink last: discovery is load-bearing
    "memory": (12, "head_tail", 2_000),         # full memory blocks; memory_search recovers
    "system_message": (13, "head_tail", 2_000),  # this turn's direct instructions; cut last
}

_CEILING_RECOVERY: dict[str, str] = {
    "context_files": " Read the complete file with the read_file tool for the full instructions.",
    "bootstrap_files": " Read the complete file with the read_file tool for the full instructions.",
    "memory": " Search memory with memory_search for the full entries.",
}


def _ceiling_truncate(label: str, text: str, keep_chars: int) -> str:
    """Head/tail truncation with a marker naming the budget cut (70% head,
    30% tail — the opening holds the most load-bearing lines)."""
    if len(text) <= keep_chars:
        return text
    head_chars = int(keep_chars * 0.7)
    tail_chars = keep_chars - head_chars
    marker = (
        f"\n\n[...truncated {label}: kept {keep_chars} of {len(text)} chars to fit the "
        f"{SYSTEM_PROMPT_MAX_CHARS}-char system-prompt budget."
        f"{_CEILING_RECOVERY.get(label, '')}]\n\n"
    )
    return text[:head_chars] + marker + (text[-tail_chars:] if tail_chars else "")


def _shrink_section(section: _PromptSection, needed: int) -> Optional[str]:
    """Shrink *section* by up to *needed* chars; the new text, or None when it
    cannot shrink further. Never raises: a section that cannot shrink is
    simply exhausted and the ceiling moves on to the next."""
    if not section.text:
        return None
    if section.mode == "drop":
        return ""
    if section.mode == "skills":
        target = max(section.floor, len(section.text) - needed)
        shrunk = _pb.truncate_skills_catalog(section.text, target)
        if shrunk is not None and len(shrunk) < len(section.text):
            return shrunk
        # Header alone already exceeds the target: dropping the block is the
        # only shrink left; the index stays reachable via skills_list.
        if len(section.text) > section.floor:
            logger.info("System-prompt ceiling dropped the skills index wholesale "
                        "(%d chars, header exceeds the %d-char floor)",
                        len(section.text), section.floor)
            return ""
        return None
    if len(section.text) <= section.floor:
        return None
    keep = max(section.floor, len(section.text) - needed - 300)  # marker headroom
    shrunk = _ceiling_truncate(section.key, section.text, keep)
    return shrunk if len(shrunk) < len(section.text) else None


def _apply_prompt_ceiling(sections: list[_PromptSection],
                          ceiling: int = SYSTEM_PROMPT_MAX_CHARS) -> list[_PromptSection]:
    """Enforce the total ceiling across *sections* (same list, texts possibly
    shrunk or emptied). Deterministic: same parts in, same prompt out, so the
    byte-stable-prefix invariant is untouched. Sections with ``priority`` None
    are never touched; when nothing left can shrink, the prompt ships over
    budget rather than refusing the turn (only reachable if identity itself
    exceeds the ceiling)."""

    def _joined_len() -> int:
        by_tier: dict[str, list[str]] = {}
        for sec in sections:
            if sec.text and sec.text.strip():
                by_tier.setdefault(sec.tier, []).append(sec.text)
        tiers = [_join_tier(by_tier.get(t, [])) for t in ("stable", "context", "volatile")]
        return len("\n\n".join(t for t in tiers if t))

    if _joined_len() <= ceiling:
        return sections
    cuttable = sorted((s for s in sections if s.priority is not None), key=lambda s: s.priority)
    while _joined_len() > ceiling:
        progressed = False
        for sec in cuttable:
            if _joined_len() <= ceiling:
                break
            shrunk = _shrink_section(sec, _joined_len() - ceiling)
            if shrunk is not None and len(shrunk) < len(sec.text):
                sec.text = shrunk
                progressed = True
        if not progressed:
            logger.info("System prompt still over the %d-char ceiling after cutting every "
                        "cuttable section; shipping %d chars rather than refusing the turn",
                        ceiling, _joined_len())
            break
    return sections


def _join_tier(parts: list[Optional[str]]) -> str:
    """Join non-empty parts; None/blank entries are dropped."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def build_system_prompt_parts(agent: Any, system_message: Optional[str] = None) -> dict[str, str]:
    """Assemble the system prompt as three ordered cache tiers: ``stable`` (identity
    only), ``context`` (caller ``system_message``, project context files, home
    workspace bootstrap files, workspace snapshot, coding brief, platform hints,
    pinned skills, and the skills/tools availability listing) and ``volatile``
    (memory, user profile, external memory block, timestamp line, Runtime line,
    runtime environment hints).  Worktree-dependent blocks follow project context so a
    shared context file can remain in the longest common prefix across worktrees.
    Never re-rendered mid-session.

    The assembled prompt is capped at ``SYSTEM_PROMPT_MAX_CHARS``: when the
    parts exceed it, the lowest-priority sections shrink first (see
    ``_CEILING_PRIORITY``) — never refused, never failed. The cut is a pure
    function of the parts, so the prompt stays byte-stable within the session.
    """
    # Model context window scales the context-file caps; stable per conversation.
    _cc_len = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    _ctx_len = _cc_len if isinstance(_cc_len, int) and _cc_len > 0 else None

    def _sec(tier: str, key: str, text: str) -> _PromptSection:
        priority, mode, floor = _CEILING_PRIORITY[key]
        return _PromptSection(tier, key, text, priority, mode, floor)

    def _keep(tier: str, key: str, text: str) -> _PromptSection:
        return _PromptSection(tier, key, text)  # priority None: never cut

    sections: list[_PromptSection] = []
    # ── Stable tier: identity only ───────────────────────────────
    stable_parts, _soul_loaded = _identity_parts(agent, _ctx_len)
    sections.extend(_keep("stable", "identity", p) for p in stable_parts)
    sections.append(_keep("stable", "identity_guidance", USER_CORRECTION_GUIDANCE))
    sections.extend(_keep("stable", "identity_alibaba", p) for p in _alibaba_identity_part(agent))
    # Coding posture: the operating brief and the environment block contain the
    # current cwd/backend, so they belong after project context, not ahead of a
    # large shared AGENTS.md block.
    environment_hints = _pb.build_environment_hints()
    coding_prefix_parts, coding_workspace_parts, coding_trailing_parts = _coding_parts(agent)
    post_workspace_parts = _post_workspace_parts(agent)
    # ── Context tier (project/worktree-dependent, may change between sessions) ──
    # ephemeral_system_prompt is injected at API-call time only, never cached.
    if system_message is not None:
        sections.append(_sec("context", "system_message", system_message))
    sections.extend(_sec("context", "context_files", p)
                    for p in _context_files_part(agent, _ctx_len, _soul_loaded))
    sections.extend(_sec("context", "bootstrap_files", p) for p in _bootstrap_files_part(agent))
    sections.extend(_sec("context", "coding_prefix", p) for p in coding_prefix_parts)
    sections.extend(_sec("context", "workspace_snapshot", p) for p in coding_workspace_parts)
    sections.extend(_sec("context", "coding_trailing", p) for p in coding_trailing_parts)
    sections.extend(_sec("context", key, p) for key, p in post_workspace_parts)
    # Pinned skills are per-agent constants (resolved once). Availability info
    # (skills index, tool listing, docs pointer, kanban protocol) rides last in
    # the context tier: it is the most likely to change, so a changed listing
    # re-prefills from here rather than from the top of the tier. It is NOT in
    # the gateway ephemeral prompt — ephemeral text is re-sent on every API call
    # and the runner has no agent yet when it assembles it, so it cannot know
    # the authoritative tool surface; see _combined_ephemeral_prompt.
    sections.extend(_sec("context", "auto_load", p) for p in _auto_load_parts(agent))
    sections.extend(_sec("context", key, p)
                    for key, p in _availability_parts(agent, context_token_budget=_ctx_len))
    # ── Volatile tier (most likely to differ on a rebuild; kept last so the stable prefix stays reusable) ──
    sections.extend(_sec("volatile", "memory", p) for p in _memory_parts(agent))
    # Plugin sections are confined to one coarse anchor in the volatile tail so
    # a resumed process can reconstruct the stable prefix without re-running plugins.
    sections.extend(_sec("volatile", "plugin_sections", p)
                    for p in _plugin_section_blocks(_frozen_plugin_prompt_sections(agent), "after_memory"))
    # The profile line names this home's path, so it rides in the volatile tier: the stable
    # prefix then stays byte-identical across every profile (and home) on the host.
    sections.append(_keep("volatile", "profile_line", _active_profile_line(agent)))
    # The Runtime line is the only place the IDENTITY.md identity surfaces
    # (name=...); it rides the volatile tier so the stable prefix stays
    # byte-identical across profiles. It goes BEFORE the timestamp trailer:
    # agent/surface_switch.py::identity_line_value reads the LAST paragraph of
    # the identity portion for "Session ID:" / "Model:" / "Provider:", and a
    # Runtime line after the trailer would shadow those staleness checks
    # (a /branch child would reuse its parent's prompt).
    runtime_line = _runtime_line(agent)
    if runtime_line:
        sections.append(_keep("volatile", "runtime_line", runtime_line))
    sections.append(_keep("volatile", "timestamp_line", _timestamp_line(agent)))
    # Keep the renderer-owned runtime anchor after all user/plugin prose so quoted
    # host examples cannot shadow it during persisted-prompt validation.
    if environment_hints:
        # Embedder hints are prose too; reserve the delimiter for the renderer.
        environment_hints = environment_hints.replace(_pb.RUNTIME_ENVIRONMENT_HEADING, "> " + _pb.RUNTIME_ENVIRONMENT_HEADING)
        sections.append(_keep("volatile", "environment_hints",
                              f"{_pb.RUNTIME_ENVIRONMENT_HEADING}\n\n{environment_hints}\n\n{_pb.RUNTIME_ENVIRONMENT_END}"))
    _apply_prompt_ceiling(sections)
    tiers = {
        tier: _join_tier([s.text for s in sections if s.tier == tier])
        for tier in ("stable", "context", "volatile")
    }
    return tiers


def build_system_prompt(agent: Any, system_message: Optional[str] = None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt`` and
    only rebuilt after compression.  Tiers are ordered stable -> context ->
    volatile so implicit longest-prefix caches keep the unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]
    # Surface context-file truncation warnings in chat, not only in logs.
    for warning in drain_truncation_warnings():
        agent._emit_diagnostic_status(warning)
    return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)


def invalidate_system_prompt(agent: Any) -> None:
    """Force a rebuild on the next turn (after compression): reload memory from
    disk and clear the frozen plugin snapshot (previous bytes stashed as the
    fail-open fallback) so plugins re-render at the same boundary.

    Called after context compression events. Also reloads memory from disk so the rebuilt prompt captures
    any writes from this session, and clears the frozen plugin-section snapshot so plugins re-render at the
    same boundary (maintainer-directed, #95681 arc): a plugin section is just another prompt block carrying
    state — freezing it while memory, skills, and guidance refresh would recreate the stale-block disease
    inside plugin-land. The previous bytes are stashed so a plugin whose render RAISES falls back to its
    last good section instead of vanishing (fail-open guard, not a freeze).
    """
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None
    if hasattr(agent, "_plugin_system_prompt_sections_snapshot"):
        agent._plugin_system_prompt_sections_previous = agent._plugin_system_prompt_sections_snapshot
        del agent._plugin_system_prompt_sections_snapshot
    if agent._memory_store:
        agent._memory_store.load_from_disk()


def reconstruct_static_prefix(agent: Any, system_message: Optional[str] = None, *, log_label: str = "restore") -> None:
    """Reconstruct ``_cached_system_prompt_static`` for a stored prompt.
    Only the full prompt is persisted, so restore / keep-prompt compression /
    mid-turn failover to a cache-on provider must rebuild the stable tier to
    regain the ``[static, volatile]`` layout.  The rebuilt tier is used ONLY
    when the stored prompt literally starts with it; otherwise static stays
    None and the stored bytes are sent untouched.  A failed rebuild is memoized
    per stored prompt so the retry-loop hot path doesn't redo the file I/O."""
    stored = getattr(agent, "_cached_system_prompt", None)
    existing = getattr(agent, "_cached_system_prompt_static", None)
    if (
        not getattr(agent, "_use_prompt_caching", False)
        or not isinstance(stored, str) or not stored
        or (isinstance(existing, str) and existing and stored.startswith(existing))
        or getattr(agent, "_static_rebuild_failed_for", None) == stored
    ):
        return
    try:
        static = build_system_prompt_parts(agent, system_message=system_message)["stable"]
        if static and stored.startswith(static):
            agent._cached_system_prompt_static = static
            agent._static_rebuild_failed_for = None
            return
    except Exception:
        logger.debug("static system-prefix reconstruction failed on %s", log_label, exc_info=True)
    agent._cached_system_prompt_static = None
    agent._static_rebuild_failed_for = stored


def format_tools_for_system_message(agent: Any) -> str:
    """JSON tool definitions in the trajectory format."""
    if not agent.tools:
        return "[]"
    return json.dumps([{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                        "parameters": t["function"].get("parameters", {}),
                        "required": None}  # Match the format in the example
                       for t in agent.tools], ensure_ascii=False)


__all__ = [
    "build_system_prompt",
    "build_system_prompt_parts",
    "format_tools_for_system_message",
    "invalidate_system_prompt",
    "platform_hint",
    "restore_plugin_prompt_sections",
]
