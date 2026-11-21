#!/usr/bin/env python3
"""Memory Search / Memory Get — retrieval over the builtin curated memory store.

FTS5 retrieval over ``MemoryStore``'s live entries (MEMORY.md / USER.md), mirroring
the session_search FTS pattern: the query is compiled to a safe FTS5 MATCH
expression (terms ANDed, quoted phrases, OR groups, prefix on plain words) and
run against a throwaway in-memory FTS5 table built from the live entries. There
is no persisted index to go stale — the whole corpus is a few thousand chars,
so a rebuild costs microseconds — and hits hydrate from the live entries, so
what search returns is exactly what /memories lists. No embeddings, no vector
retrieval: FTS only (F1 scope).

Both tools are read-only. The live path is the inline executor
(``agent/inline_tool_executors.py``), which passes ``store=agent._memory_store``;
the registry handler below takes the same ``store`` kwarg so direct dispatch
works too. ``agent_init.py`` whole-file injection is untouched.
"""

import json
import logging
import re
import sqlite3
from typing import Any, Optional

from tools.memory_tool import ENTRY_DELIMITER, check_memory_requirements
from tools.memory_tool_store import MemoryStore
from tools.registry import _bound_error_text, registry, tool_error

logger = logging.getLogger(__name__)

# Excerpt bounds. Entries are short (the whole store is a few thousand chars),
# so search returns the entry near-whole; memory_get pages longer entries.
_SEARCH_EXCERPT_CHARS = 600
_SEARCH_DEFAULT_MAX_RESULTS = 5
_SEARCH_MAX_RESULTS_CAP = 20
_GET_DEFAULT_MAX_CHARS = 2000
_GET_MAX_CHARS_CAP = 8000
_GET_MIN_MAX_CHARS = 100

_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_PHRASE_RE = re.compile(r'"([^"]*)"')
_OR_SPLIT_RE = re.compile(r"\s+OR\s+", re.IGNORECASE)


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _is_cjk_token(token: str) -> bool:
    return bool(_CJK_RE.search(token))


def _compile_match_expression(query: str) -> Optional[str]:
    """Safe FTS5 MATCH expression for *query*, or None when it has no searchable term.

    Terms are ANDed; ``"quoted spans"`` are phrase matches; ``OR`` splits the
    query into alternatives. Every term is built from ``\\w`` runs only, so no
    user text ever reaches MATCH as syntax — a query of pure punctuation, or of
    nothing but ``OR``, compiles to nothing. Plain words get a prefix ``*``
    (``deploy`` also matches ``deployment``); CJK runs stay exact phrases, since
    unicode61 already tokenizes them per character.
    """
    alternatives: list[str] = []
    for alternative in _OR_SPLIT_RE.split(query):
        parts: list[str] = []
        for phrase in _PHRASE_RE.findall(alternative):
            words = _WORD_RE.findall(phrase)
            if words:
                parts.append('"' + " ".join(words) + '"')
        bare = _PHRASE_RE.sub(" ", alternative)
        for token in _WORD_RE.findall(bare):
            if token.upper() == "OR":
                continue
            if _is_cjk_token(token):
                parts.append('"' + token + '"')
            else:
                parts.append('"' + token + '"*')
        if parts:
            alternatives.append(" ".join(parts))
    if not alternatives:
        return None
    if len(alternatives) == 1:
        return alternatives[0]
    return " OR ".join(f"({alt})" for alt in alternatives)


def _python_search(entries: dict[str, list[str]], query: str, limit: int) -> list[tuple[str, int, float]]:
    """Substring search: an entry matches only when EVERY query term is present
    (casefolded). Same ``(target, 0-based index, score)`` shape as the FTS path.
    Runs when FTS5 is unavailable, and as a second recall pass when FTS5 finds
    nothing (it also matches mid-word, where FTS5 prefix matching does not)."""
    terms: list[str] = []
    for phrase in _PHRASE_RE.findall(query):
        if phrase.strip():
            terms.append(phrase.strip().lower())
    for token in _WORD_RE.findall(_PHRASE_RE.sub(" ", query)):
        if token.upper() != "OR" and token.lower() not in terms:
            terms.append(token.lower())
    if not terms:
        return []
    scored: list[tuple[float, str, int]] = []
    for target, rows in entries.items():
        for index, entry in enumerate(rows):
            folded = entry.lower()
            if all(term in folded for term in terms):
                scored.append((0.0, target, index))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(target, index, score) for score, target, index in scored[:limit]]


def _fts_search(entries: dict[str, list[str]], match_expr: str, limit: int) -> list[tuple[str, int, float]]:
    """``(target, 0-based index, bm25 score)`` ranked by FTS5, best first."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5("
                "target UNINDEXED, entry_index UNINDEXED, text, tokenize='unicode61')")
            for target, rows in entries.items():
                for index, entry in enumerate(rows):
                    conn.execute("INSERT INTO memory_fts(target, entry_index, text) VALUES (?, ?, ?)",
                                 (target, index, entry))
            rows = conn.execute(
                "SELECT target, entry_index, bm25(memory_fts) AS score FROM memory_fts "
                "WHERE memory_fts MATCH ? ORDER BY score LIMIT ?", (match_expr, limit)).fetchall()
            return [(str(target), int(index), float(score)) for target, index, score in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("Memory FTS5 unavailable; falling back to substring search", exc_info=True)
        return []


def _entry_start_line(entries: list[str], index: int) -> int:
    """1-based line of entry *index* in the file the store writes: entries joined
    by ENTRY_DELIMITER, so the line is 1 + the newlines in everything before it —
    the earlier entries plus the delimiter that separates the last of them from
    this one."""
    if index <= 0:
        return 1
    before = ENTRY_DELIMITER.join(entries[:index]) + ENTRY_DELIMITER
    return 1 + before.count("\n")


def _source(target: str, entries: list[str], index: int) -> str:
    """``MEMORY.md:<line>`` / ``USER.md:<line>`` provenance for a hit."""
    filename = "USER.md" if target == "user" else "MEMORY.md"
    return f"{filename}:{_entry_start_line(entries, index)}"


def _excerpt(entry: str) -> tuple[str, bool]:
    """Entry bounded to ``_SEARCH_EXCERPT_CHARS``; ``(text, truncated)``."""
    if len(entry) <= _SEARCH_EXCERPT_CHARS:
        return entry, False
    return entry[:_SEARCH_EXCERPT_CHARS] + "…", True


def _enabled_targets(store: MemoryStore, target: str) -> tuple[Optional[list[str]], Optional[str]]:
    """Targets *target* (``memory``/``user``/``all``) resolves to among the enabled
    stores, or ``(None, error_json)`` when the request names nothing enabled."""
    if target not in ("memory", "user", "all"):
        return None, tool_error(_bound_error_text(
            f"Invalid memory target '{target}'. Use 'memory', 'user', or 'all'."), success=False)
    wanted = ("memory", "user") if target == "all" else (target,)
    enabled = [t for t in wanted if store.target_enabled(t)]
    if not enabled:
        label = " and ".join("USER.md" if t == "user" else "MEMORY.md" for t in wanted)
        return None, tool_error(f"Built-in {label} reads are disabled in memory config.", success=False)
    return enabled, None


def memory_search(query: str = "", target: str = "all", max_results: Optional[int] = None,
                  store: Optional[MemoryStore] = None) -> str:
    """Search saved memories (FTS5 over the live entries). Returns ranked hits with
    the excerpt and its source file+line; a truncated excerpt names memory_get
    (same target + index) for the rest."""
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.",
                          success=False)
    query_text = (query or "").strip()
    if not query_text:
        return tool_error("query is required: keywords or a phrase to find in saved memories.",
                          success=False)
    targets, error = _enabled_targets(store, str(target or "all").strip().lower())
    if error is not None:
        return error
    assert targets is not None
    entries = {t: store.entries(t) for t in targets}
    match_expr = _compile_match_expression(query_text)
    if match_expr is None:
        return tool_error("No searchable terms in query: use keywords or a quoted phrase.",
                          success=False)
    limit = _clamp_int(max_results, _SEARCH_DEFAULT_MAX_RESULTS, 1, _SEARCH_MAX_RESULTS_CAP)
    hits = _fts_search(entries, match_expr, limit)
    if not hits:
        hits = _python_search(entries, query_text, limit)
    results: list[dict[str, Any]] = []
    for rank, (hit_target, index, _score) in enumerate(hits, 1):
        entry = entries[hit_target][index]
        excerpt, truncated = _excerpt(entry)
        results.append({
            "target": hit_target, "index": index + 1, "rank": rank,
            "source": _source(hit_target, entries[hit_target], index),
            "excerpt": excerpt, "excerpt_truncated": truncated, "entry_chars": len(entry),
            "origin_class": store.entry_origin(hit_target, entry),
        })
    if not results:
        return _ok(query=query_text, results=[], count=0, message=(
            "No matching memory entries. FTS5 ANDs all terms by default — broaden with OR "
            "(`alpha OR beta`), exact-match with a quoted phrase, or prefix-match with a "
            "shorter word."))
    return _ok(query=query_text, results=results, count=len(results),
               **({"message": "An excerpt is truncated; use memory_get with its target + index to read the rest."}
                  if any(r["excerpt_truncated"] for r in results) else {}))


def memory_get(target: str = "memory", index: Optional[Any] = None, start_char: int = 0,
               max_chars: Optional[int] = None, store: Optional[MemoryStore] = None) -> str:
    """Bounded exact read of one memory entry by 1-based index (the index
    memory_search and /memories report). Returns the excerpt with truncation /
    continuation info: ``truncated`` + ``next_start_char`` re-call for the rest."""
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.",
                          success=False)
    targets, error = _enabled_targets(store, str(target or "memory").strip().lower())
    if error is not None:
        return error
    if targets is None or len(targets) != 1:
        return tool_error("target must be 'memory' or 'user'.", success=False)
    resolved_target = targets[0]
    try:
        entry_index = int(str(index).strip()) if isinstance(index, str) else int(index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return tool_error("index is required: the 1-based entry number from memory_search or /memories.",
                          success=False)
    entries = store.entries(resolved_target)
    if entry_index < 1 or entry_index > len(entries):
        return tool_error(
            f"index {entry_index} out of range: {resolved_target} has {len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'}. Search first to find the entry's index.",
            success=False)
    entry = entries[entry_index - 1]
    start = _clamp_int(start_char, 0, 0, len(entry))
    limit = _clamp_int(max_chars, _GET_DEFAULT_MAX_CHARS, _GET_MIN_MAX_CHARS, _GET_MAX_CHARS_CAP)
    excerpt = entry[start:start + limit]
    truncated = start + limit < len(entry)
    return _ok(target=resolved_target, index=entry_index,
               source=_source(resolved_target, entries, entry_index - 1),
               entry_chars=len(entry), start_char=start, excerpt=excerpt, truncated=truncated,
               next_start_char=(start + limit if truncated else None),
               origin_class=store.entry_origin(resolved_target, entry),
               **({"message": f"Entry continues; re-call memory_get with start_char={start + limit} for the rest."}
                  if truncated else {}))


MEMORY_SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": (
        "Search your saved memories (personal notes + user profile). Use it when an answer depends on "
        "prior work, decisions, dates, people, preferences, or todos not already present in "
        "the current turn. Full-text search over the memory entries: terms are ANDed, "
        "quoted spans match exact phrases, OR broadens, and a plain word also matches longer "
        "words starting with it. Returns ranked hits with the excerpt and its source file+line; "
        "use memory_get with a hit's target + index to read the rest of a truncated entry. "
        "Do not repeat memory retrieval when supplied context fully answers the question. "
        "If low confidence after searching, say you checked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keywords, a phrase, or a boolean expression to find in saved memories "
                    "(e.g. 'deployment checklist', '\"release process\"', 'deploy OR rollback')."
                ),
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user", "all"],
                "description": (
                    "Which store to search: 'memory' for personal notes, 'user' for user profile, "
                    "'all' (default) for both."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max hits to return. Default 5, max 20.",
            },
        },
        "required": ["query"],
    },
}

MEMORY_GET_SCHEMA = {
    "name": "memory_get",
    "description": (
        "Read one saved memory entry in full, by its 1-based index. The index is the one memory_search "
        "returns, or /memories lists. Returns a bounded excerpt with truncation and continuation "
        "info: when truncated, re-call with start_char set to next_start_char for the rest. "
        "Use it to pull only the needed entry instead of re-searching."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which store holds the entry: 'memory' for personal notes, 'user' for user profile.",
            },
            "index": {
                "type": "integer",
                "description": "1-based entry number, as returned by memory_search or /memories.",
            },
            "start_char": {
                "type": "integer",
                "description": "Character offset to start the excerpt at. Default 0; use next_start_char to continue.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters in the excerpt. Default 2000, max 8000.",
            },
        },
        "required": ["target", "index"],
    },
}

registry.register(
    name="memory_search",
    toolset="memory",
    schema=MEMORY_SEARCH_SCHEMA,
    handler=lambda args, **kw: memory_search(
        query=args.get("query") or "", target=args.get("target") or "all",
        max_results=args.get("max_results"), store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🔍")

registry.register(
    name="memory_get",
    toolset="memory",
    schema=MEMORY_GET_SCHEMA,
    handler=lambda args, **kw: memory_get(
        target=args.get("target") or "memory", index=args.get("index"),
        start_char=args.get("start_char", 0), max_chars=args.get("max_chars"), store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="📄")
