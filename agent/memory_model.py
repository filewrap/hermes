"""Durable-memory MODEL (external-agent memory-core shape, adapted to Hermes' MemoryStore).

This is the model, not the store: Hermes already has the store (``tools/memory_tool_store.py``,
MEMORY.md / USER.md) and the retrieval tool (``memory_search`` / ``memory_get``, F1). What lived
here is the shape of durable memory itself:

- provenance classes (the external agent ``MemoryEntryOrigin.originClass``) and the resolver that tags
  every write by its real source;
- the guidance-only turn-1 prompt (the external agent's prompt carries guidance, never the whole memory
  files; retrieval runs through the search tools);
- the dreaming promotion gates (Light -> REM -> Deep) as pure decisions over candidates — the
  gates decide when a memory promotes, nothing else; there is no scheduler here.

No I/O, no LLM calls: the gates and the resolver are pure functions over the agent's
already-known state. Durable per-entry provenance lives in the store's sidecar
(``tools/memory_tool_store.py``), not in this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# Provenance classes — the external agent's MemoryEntryOrigin.originClass, verbatim.
ORIGIN_CLASS_OWNER = "owner"
ORIGIN_CLASS_AGENT = "agent"
ORIGIN_CLASS_UNTRUSTED = "untrusted"
ORIGIN_CLASS_SYSTEM = "system"
ORIGIN_CLASSES = frozenset({ORIGIN_CLASS_OWNER, ORIGIN_CLASS_AGENT, ORIGIN_CLASS_UNTRUSTED, ORIGIN_CLASS_SYSTEM})

# Write origins that name a non-interactive system fork: the write was initiated by Hermes
# itself (a background review, the autonomous curator, the pre-compaction flush), not by a
# turn the user is part of. These are the only ``system`` sources — the resolver never
# invents one, and ``owner`` is never inferred here at all (see resolve_memory_origin_class).
SYSTEM_WRITE_ORIGINS = frozenset({"background_review", "background_curator", "compaction_flush"})

# Foreground agent turns write through the memory tool under this origin (agent/agent_init.py).
FOREGROUND_WRITE_ORIGIN = "assistant_tool"


def resolve_memory_origin_class(agent: Any, *, write_origin: Optional[str] = None) -> str:
    """Provenance class for a memory write, from its real source (the external agent's resolveOriginClass).

    ``system``: the write comes from a non-interactive system path — a background-review or
    curator fork, the pre-compaction flush, or a cron agent (``platform == "cron"``: no user
    turn exists to author it). ``untrusted``: the foreground turn observed network-sourced
    tool content (wave-3 turn taint — see ``agent/turn_taint.py``). ``agent``: everything else
    (a foreground agent turn writing on an untainted turn).

    ``owner`` is deliberately absent from this resolver: the agent authors every tool write,
    so no turn state can prove the owner wrote the bytes. Owner provenance is recorded only
    where the owner directly commits the bytes — the ``/memory approve`` replay of a staged
    write (``tools/memory_tool.py::apply_memory_pending``). Never infer it from "the user
    asked for it": a user asking the agent to remember something is still an agent-authored
    write, exactly as the external agent's ``resolveOriginClass`` reports it.
    """
    origin = write_origin or getattr(agent, "_memory_write_origin", FOREGROUND_WRITE_ORIGIN) or FOREGROUND_WRITE_ORIGIN
    if origin in SYSTEM_WRITE_ORIGINS:
        return ORIGIN_CLASS_SYSTEM
    if getattr(agent, "platform", None) == "cron":
        return ORIGIN_CLASS_SYSTEM
    from agent.turn_taint import turn_taint_origin_class

    return turn_taint_origin_class(agent)


# -- Turn-1 guidance-only injection ------------------------------------------

def is_first_memory_turn(agent: Any) -> bool:
    """True on the first turn of a session: ``_user_turn_count`` is hydrated from persisted
    history before the system prompt is built, then incremented, so a fresh session builds
    its prompt at exactly 1 while a resumed session (or any later rebuild) is past 1.
    Unknown counts fail open to full injection — never guidance on a guess."""
    count = getattr(agent, "_user_turn_count", None)
    return isinstance(count, int) and not isinstance(count, bool) and count <= 1


def memory_guidance_block(agent: Any) -> str:
    """Memory RECALL guidance for a turn-1 prompt (the external agent's ``buildMemoryPromptSection``,
    adapted to Hermes' ``memory_search`` / ``memory_get``). Guidance only: no entry text,
    no retrieved content. Lines name only tools actually in this session's surface, so the
    block never advertises a tool the model cannot call. ``""`` when neither recall tool
    is available."""
    tools = set(getattr(agent, "valid_tool_names", None) or ())
    has_search = "memory_search" in tools
    has_get = "memory_get" in tools
    if not has_search and not has_get:
        return ""
    if has_search and has_get:
        how = ("Run memory_search to search the saved entries; for a memory hit, use memory_get "
               "to pull only the needed lines.")
    elif has_search:
        how = "Run memory_search to search the saved entries."
    else:
        how = "Use memory_get with a known entry index to pull only the needed lines."
    return (
        "## Memory Recall\n"
        "Durable memory is not preloaded on this turn — search it when the answer could depend "
        "on prior work, decisions, dates, people, preferences, or todos not already present in "
        f"this turn. {how} Do not repeat memory retrieval when supplied context fully answers "
        "the question. If low confidence after search, say you checked."
    )


# -- Dreaming promotion gates (Light -> REM -> Deep) -------------------------
#
# The external agent's promotion shape (extensions/memory-core/src/):
#   Light  — fresh short-term entries are staged (dreaming-phases.ts prepareLightDreaming);
#   REM    — reflections synthesize from the LIGHT-staged keys, never a fresh rescan
#            (readLightStagedKeys);
#   Deep   — staged candidates are ranked and the survivors promote into MEMORY.md
#            (short-term-promotion.ts rankShortTermPromotionCandidates +
#            dreaming.ts deep phase), gated on score AND provenance.
# Ported here as pure decisions over Hermes-adapted candidates. No scheduler, no LLM, no
# file writes: the gates answer "does this memory promote?" and callers act on the answer.
# Weights and thresholds are the external agent's verbatim (short-term-promotion.ts /
# src/memory-host-sdk/dreaming.ts): minScore 0.75, minRecallCount 3, minUniqueQueries 3.

PROMOTION_WEIGHTS = {
    "frequency": 0.24,
    "relevance": 0.30,
    "diversity": 0.15,
    "recency": 0.15,
    "consolidation": 0.10,
    "conceptual": 0.06,
}
PROMOTION_MIN_SCORE = 0.75
PROMOTION_MIN_RECALL_COUNT = 3
PROMOTION_MIN_UNIQUE_QUERIES = 3
PROMOTION_RECENCY_HALF_LIFE_DAYS = 14
PROMOTION_LIGHT_BOOST_MAX = 0.06
PROMOTION_REM_BOOST_MAX = 0.09
# Light staging only keeps entries recalled recently (the external agent's 14-day ingestion horizon).
LIGHT_FRESH_DAYS = 14
DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class PromotionCandidate:
    """One short-term memory asking to become durable. ``recall_count`` / ``daily_count`` /
    ``grounded_count`` are recall signals; ``unique_queries`` counts distinct user queries
    that recalled it; ``provenance`` is the external-agent origin class (or None when unknown —
    unknown never promotes as trusted). ``light_hits`` / ``rem_hits`` are phase-signal
    counts from earlier Light/REM passes."""

    key: str
    snippet: str
    origin_class: Optional[str] = None
    session_kind: Optional[str] = None
    recall_count: int = 0
    daily_count: int = 0
    grounded_count: int = 0
    avg_score: float = 0.0
    max_score: float = 0.0
    unique_queries: int = 0
    recall_days: list[str] = field(default_factory=list)
    concept_tags: list[str] = field(default_factory=list)
    first_recalled_at: str = ""
    last_recalled_at: str = ""
    promoted_at: Optional[str] = None
    light_hits: int = 0
    last_light_at: Optional[str] = None
    rem_hits: int = 0
    last_rem_at: Optional[str] = None


def _clamp_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _recency_component(age_days: float, half_life_days: float) -> float:
    """Exponential decay: 1.0 fresh, 0.5 after one half-life (the external agent's calculateRecencyComponent)."""
    if not math.isfinite(age_days) or age_days < 0 or not math.isfinite(half_life_days) or half_life_days <= 0:
        return 1.0
    return math.exp(-(math.log(2) / half_life_days) * age_days)


def _parse_ms(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def is_promotion_origin_blocked(candidate: PromotionCandidate) -> bool:
    """Explicitly tainted origins must never promote through any durable write path
    (the external agent's dreaming-consolidation-candidates.ts isPromotionOriginBlocked, verbatim)."""
    return candidate.origin_class in {ORIGIN_CLASS_UNTRUSTED, ORIGIN_CLASS_SYSTEM}


def is_consolidation_candidate_eligible(candidate: PromotionCandidate) -> bool:
    """Trusted origin (owner/agent) only, and a session-derived candidate counts only from
    an interactive session (the external agent's isConsolidationCandidateEligible, verbatim). Hermes
    candidates carry no path, so ``session_kind`` is the whole session-derived test: a
    candidate whose session kind is known and not interactive is not eligible."""
    trusted_origin = candidate.origin_class in {ORIGIN_CLASS_OWNER, ORIGIN_CLASS_AGENT}
    return trusted_origin and (candidate.session_kind is None or candidate.session_kind == "interactive")


def _consolidation_component(recall_days: list[str]) -> float:
    """Spread of recall across days: one day scores 0.2, wider spans score higher
    (the external agent's calculateConsolidationComponent, verbatim)."""
    if not recall_days:
        return 0.0
    parsed = sorted({d for day in recall_days if (d := _parse_ms(day + "T00:00:00.000Z")) is not None})
    if len(parsed) <= 1:
        return 0.2
    span_days = max(0.0, (parsed[-1] - parsed[0]) / DAY_MS)
    spacing = _clamp_score(math.log1p(len(parsed) - 1) / math.log1p(4))
    span = _clamp_score(span_days / 7)
    return _clamp_score(0.55 * spacing + 0.45 * span)


def _phase_signal_boost(candidate: PromotionCandidate, now_ms: float) -> float:
    """Light/REM phase-signal boost, capped (the external agent's calculatePhaseSignalBoost, verbatim)."""

    def contribution(hits: int, last_seen_at: Optional[str], maximum: float) -> float:
        strength = _clamp_score(math.log1p(max(0, hits)) / math.log1p(6))
        seen_ms = _parse_ms(last_seen_at)
        recency = 0.0 if seen_ms is None else _clamp_score(
            _recency_component(max(0.0, (now_ms - seen_ms) / DAY_MS), PROMOTION_RECENCY_HALF_LIFE_DAYS))
        return maximum * strength * recency

    return _clamp_score(
        contribution(candidate.light_hits, candidate.last_light_at, PROMOTION_LIGHT_BOOST_MAX)
        + contribution(candidate.rem_hits, candidate.last_rem_at, PROMOTION_REM_BOOST_MAX)
    )


def score_promotion_candidate(candidate: PromotionCandidate, *, now_ms: float) -> dict[str, float]:
    """Weighted promotion score + components (the external agent's rankShortTermPromotionCandidates,
    verbatim weights). Returns ``{"score", "frequency", "relevance", "diversity",
    "recency", "consolidation", "conceptual", "phase_boost"}``."""
    signal_count = candidate.recall_count + candidate.daily_count + candidate.grounded_count
    frequency = _clamp_score(math.log1p(max(0, signal_count)) / math.log1p(10))
    relevance = _clamp_score(candidate.avg_score)
    diversity = _clamp_score(candidate.unique_queries / 5)
    last_ms = _parse_ms(candidate.last_recalled_at)
    age_days = 0.0 if last_ms is None else max(0.0, (now_ms - last_ms) / DAY_MS)
    recency = _clamp_score(_recency_component(age_days, PROMOTION_RECENCY_HALF_LIFE_DAYS))
    consolidation = max(_consolidation_component(candidate.recall_days), _clamp_score(candidate.grounded_count / 3))
    conceptual = _clamp_score(len(candidate.concept_tags) / 6)
    phase_boost = _phase_signal_boost(candidate, now_ms)
    score = _clamp_score(
        PROMOTION_WEIGHTS["frequency"] * frequency
        + PROMOTION_WEIGHTS["relevance"] * relevance
        + PROMOTION_WEIGHTS["diversity"] * diversity
        + PROMOTION_WEIGHTS["recency"] * recency
        + PROMOTION_WEIGHTS["consolidation"] * consolidation
        + PROMOTION_WEIGHTS["conceptual"] * conceptual
        + phase_boost
    )
    return {
        "score": score, "frequency": frequency, "relevance": relevance, "diversity": diversity,
        "recency": recency, "consolidation": consolidation, "conceptual": conceptual,
        "phase_boost": phase_boost,
    }


def rank_promotion_candidates(
    candidates: list[PromotionCandidate], *, now_ms: float,
    min_score: float = PROMOTION_MIN_SCORE,
    min_recall_count: int = PROMOTION_MIN_RECALL_COUNT,
    min_unique_queries: int = PROMOTION_MIN_UNIQUE_QUERIES,
    max_age_days: int = -1,
    include_promoted: bool = False,
    limit: Optional[int] = None,
) -> list[tuple[PromotionCandidate, dict[str, float]]]:
    """Deep-gate ranking (the external agent's rankShortTermPromotionCandidates, verbatim thresholds):
    origin-blocked, already-promoted, under-signalled, under-diverse, stale, and
    under-scored candidates are dropped before the limit; survivors sort by score, then
    recall count, then key."""
    ranked: list[tuple[PromotionCandidate, dict[str, float]]] = []
    for candidate in candidates:
        if is_promotion_origin_blocked(candidate):
            continue
        if candidate.promoted_at and not include_promoted:
            continue
        signal_count = candidate.recall_count + candidate.daily_count + candidate.grounded_count
        if signal_count <= 0 or signal_count < min_recall_count:
            continue
        if candidate.unique_queries < min_unique_queries:
            continue
        last_ms = _parse_ms(candidate.last_recalled_at)
        age_days = 0.0 if last_ms is None else max(0.0, (now_ms - last_ms) / DAY_MS)
        if max_age_days >= 0 and age_days > max_age_days:
            continue
        components = score_promotion_candidate(candidate, now_ms=now_ms)
        if components["score"] < min_score:
            continue
        ranked.append((candidate, components))
    ranked.sort(key=lambda item: (-item[1]["score"], -item[0].recall_count, item[0].key))
    return ranked[:limit] if limit is not None else ranked


def light_stage_candidates(
    candidates: list[PromotionCandidate], *, now_ms: float,
) -> list[PromotionCandidate]:
    """Light: stage fresh, unpromoted candidates for REM (the external agent's prepareLightDreaming /
    filterFreshLightDreamingEntries shape). Fresh = recalled within the ingestion horizon;
    already-promoted entries are never re-staged."""
    staged: list[PromotionCandidate] = []
    for candidate in candidates:
        if candidate.promoted_at:
            continue
        last_ms = _parse_ms(candidate.last_recalled_at)
        age_days = 0.0 if last_ms is None else max(0.0, (now_ms - last_ms) / DAY_MS)
        if age_days <= LIGHT_FRESH_DAYS:
            staged.append(candidate)
    return staged


def rem_consider_candidates(
    candidates: list[PromotionCandidate], *, light_staged_keys: set[str],
) -> list[PromotionCandidate]:
    """REM: consider only entries Light staged (the external agent's readLightStagedKeys — REM
    synthesises from the sequential light→REM pipeline instead of rescanning)."""
    return [c for c in candidates if c.key in light_staged_keys]


def deep_promotion_candidates(
    candidates: list[PromotionCandidate], *, now_ms: float, **kwargs: Any,
) -> list[tuple[PromotionCandidate, dict[str, float]]]:
    """Deep: rank the REM-considered candidates through the score + provenance gates
    (the external agent's dreaming.ts deep phase). Only the survivors promote into durable memory."""
    return rank_promotion_candidates(candidates, now_ms=now_ms, **kwargs)


# -- Committed-write extraction (provenance recording) -----------------------

def _tool_result_dict(result: Any) -> Optional[dict[str, Any]]:
    """The memory tool's result as a dict, or None when it is not one (a failed/odd
    result is never provenance for a write that may not have landed)."""
    import json

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, RecursionError):
            return None
    return result if isinstance(result, dict) else None


def committed_memory_write_entries(tool_args: dict[str, Any], result: Any) -> list[dict[str, str]]:
    """Entries a memory-tool call actually committed, for provenance recording.

    Mirrors ``MemoryManager.notify_memory_tool_write``'s gating (committed writes only:
    ``success`` true and not staged) and its entry resolution (patch after-text comes
    from the store's result, never the search argument). Returns ``{"target", "action",
    "entry", "previous_entry"}`` rows; ``remove`` rows carry the removed entry so its
    provenance record can be dropped, and replace/patch rows carry the entry they
    overwrote for the same reason. Ops whose committed entry cannot be resolved are
    skipped — an unrecorded entry stays unknown, never mis-tagged."""
    parsed = _tool_result_dict(result)
    if parsed is None or parsed.get("success") is not True or parsed.get("staged") is True:
        return []
    target = str(tool_args.get("target") or "memory")
    operations = tool_args.get("operations")
    batched = isinstance(operations, list) and bool(operations)
    rows: list[dict[str, str]] = []
    for index, op in enumerate(operations if batched else [tool_args], start=1):
        if not isinstance(op, dict):
            continue
        action = str(op.get("action") or "")
        if action not in {"add", "replace", "patch", "remove"}:
            continue
        entry = str(op.get("content") or op.get("new_text") or "")
        previous = ""
        if action == "patch":
            if batched:
                after_entries = parsed.get("patched_entries_after") or {}
                after = after_entries.get(str(index), after_entries.get(index)) if isinstance(after_entries, dict) else None
                before_entries = parsed.get("patched_entries") or {}
                previous = str(before_entries.get(str(index), before_entries.get(index)) or "") if isinstance(before_entries, dict) else ""
            else:
                after = parsed.get("patched_entry_after")
                previous = str(parsed.get("patched_entry_before") or "")
            entry = str(after or "")
        elif action == "replace":
            if batched:
                replaced = parsed.get("replaced_entries") or {}
                previous = str(replaced.get(str(index), replaced.get(index)) or "") if isinstance(replaced, dict) else ""
            else:
                previous = str(parsed.get("replaced_entry") or "")
        elif action == "remove":
            if batched:
                removed = parsed.get("removed_entries") or {}
                entry = str(removed.get(str(index), removed.get(index)) or "") if isinstance(removed, dict) else ""
            else:
                entry = str(parsed.get("removed_entry") or "")
            if not entry:
                # Fall back to the pinned entry the tool resolved (same source the
                # store itself reports for replace/remove); never the raw old_text.
                entry = str(op.get("matched_entry") or "")
        if entry:
            rows.append({"target": target, "action": action, "entry": entry, "previous_entry": previous})
    return rows
