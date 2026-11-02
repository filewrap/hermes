"""Private storage and lifecycle helpers for interactive Telegram deep research."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from gateway.deep_research_sources import source_identity, write_observed_source_ledger

MAX_TURNS = 150
MAX_TURNS_CAP = 500
DEFAULT_MAX_TURNS = 150
REPORT_FORMATS = ("md", "pdf", "docx")
DEFAULT_REPORT_FORMAT = "md"
RESEARCH_DEPTHS = ("standard", "deep", "exhaustive")
DEFAULT_RESEARCH_DEPTH = "deep"


def default_research_plan() -> list[dict[str, str]]:
    """Canonical task plan seeded into a fresh session's todo store.

    The plan is the agent's own task state (via ``TodoStore.restore``), never
    invented progress-card text: the card only renders todo_list results, so
    these tasks appear there once the agent adopts and updates them. Seeding
    guarantees a ledger exists even when the agent would otherwise never call
    ``todo_list`` — the completion gate can then judge real unfinished work
    instead of a missing ledger.
    """
    return [
        {"id": "dr-scope", "content": "Scope the question: subquestions, boundaries, and assumptions", "status": "pending"},
        {"id": "dr-search", "content": "Search broadly across independent sources", "status": "pending"},
        {"id": "dr-read-verify", "content": "Read and cross-check source pages; ledger every read URL in research_notes.md", "status": "pending"},
        {"id": "dr-report", "content": "Write the cited report to report.md", "status": "pending"},
        {"id": "dr-verify", "content": "Read back report and notes; verify citations map to read URLs", "status": "pending"},
    ]
_SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_USER_ID_RE = re.compile(r"^[0-9]{1,24}$")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ALLOWED_STATUS = {"starting", "running", "paused", "completed", "failed", "interrupted", "cancelled"}
_UNSET = object()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _identity(user_id: str | int) -> str:
    value = str(user_id or "").strip()
    if not _USER_ID_RE.fullmatch(value):
        raise ValueError("Deep-research sessions require a numeric Telegram user ID")
    return value


def _base_dir(profile_home: str | Path, user_id: str | int) -> Path:
    home = Path(profile_home).expanduser().resolve(strict=False)
    base = home / "deep_research_sessions" / _identity(user_id)
    if base.resolve(strict=False) != base and not base.resolve(strict=False).is_relative_to(home):
        raise ValueError("Invalid deep-research storage path")
    return base


def _session_dir(profile_home: str | Path, user_id: str | int, session_id: str) -> Path:
    sid = str(session_id or "").strip().lower()
    if not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("Invalid deep-research session ID")
    base = _base_dir(profile_home, user_id)
    target = base / sid
    resolved_base = base.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_base):
        raise ValueError("Deep-research session path escaped its private directory")
    return target


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def create_session(
    profile_home: str | Path,
    user_id: str | int,
    query: str,
    *,
    chat_id: str | int,
    chat_type: str,
    thread_id: str | int | None = None,
    custom_instructions: str = "",
    trigger_message_id: str | int | None = None,
    topic_create_random_id: int | None = None,
    max_turns: int | None = None,
    report_format: str | None = None,
    depth: str | None = None,
) -> dict[str, Any]:
    """Create a private session directory and a durable metadata record."""
    query = str(query or "").strip()
    if not query or len(query) > 4000:
        raise ValueError("Research query must contain 1–4,000 characters")
    custom_instructions = str(custom_instructions or "").strip()
    if len(custom_instructions) > 2000:
        raise ValueError("Custom instructions must be 2,000 characters or fewer")
    try:
        max_turns_value = DEFAULT_MAX_TURNS if max_turns is None else int(max_turns)
    except (TypeError, ValueError):
        raise ValueError("Max turns must be a number")
    if not 1 <= max_turns_value <= MAX_TURNS_CAP:
        raise ValueError(f"Max turns must be between 1 and {MAX_TURNS_CAP}")
    report_format_value = str(report_format or DEFAULT_REPORT_FORMAT).strip().lower()
    if report_format_value not in REPORT_FORMATS:
        raise ValueError("Report format must be md, pdf, or docx")
    depth_value = str(depth or DEFAULT_RESEARCH_DEPTH).strip().lower()
    if depth_value not in RESEARCH_DEPTHS:
        raise ValueError("Research depth must be standard, deep, or exhaustive")
    base = _base_dir(profile_home, user_id)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    for _ in range(5):
        session_id = uuid.uuid4().hex[:12]
        directory = _session_dir(profile_home, user_id, session_id)
        try:
            directory.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("Could not allocate a unique deep-research session directory")
    record: dict[str, Any] = {
        "session_id": session_id,
        "user_id": _identity(user_id),
        "chat_id": str(chat_id),
        "chat_type": str(chat_type or "unknown"),
        "thread_id": str(thread_id) if thread_id is not None else None,
        "trigger_message_id": str(trigger_message_id) if trigger_message_id is not None else None,
        "latest_reply_message_id": None,
        "topic_create_random_id": int(topic_create_random_id or (secrets.randbits(63) or 1)),
        "query": query,
        "custom_instructions": custom_instructions,
        "status": "starting",
        "phase": "starting",
        "failure_class": None,
        "artifact_status": "not_checked",
        "final_response_status": "pending",
        "max_turns": max_turns_value,
        "report_format": report_format_value,
        "depth": depth_value,
        "shell_access": True,
        "compaction_allowed": True,
        "tools_allowed": True,
        "mcps_allowed": True,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "session_dir": str(directory.resolve()),
        "report_file": "report.md",
        "notes_file": "research_notes.md",
        "checkpoint_file": "checkpoint.json",
    }
    try:
        with _lock_for(directory / "session.json"):
            _write_json(directory / "session.json", record)
    except BaseException:
        try:
            (directory / "session.json").unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass
        raise
    return record


def resolve_session_dir(profile_home: str | Path, user_id: str | int, session_id: str) -> Path:
    """Return an existing session directory only when its durable record matches the owner."""
    directory = _session_dir(profile_home, user_id, session_id)
    record_path = directory / "session.json"
    if not record_path.is_file():
        raise FileNotFoundError("Deep-research session does not exist")
    resolved = directory.resolve(strict=True)
    base = _base_dir(profile_home, user_id).resolve(strict=True)
    if not resolved.is_relative_to(base):
        raise ValueError("Deep-research session escaped its private directory")
    record = load_session(profile_home, user_id, session_id)
    if record.get("session_id") != session_id or record.get("user_id") != _identity(user_id):
        raise PermissionError("Deep-research session owner mismatch")
    return resolved


def load_session(profile_home: str | Path, user_id: str | int, session_id: str) -> dict[str, Any]:
    directory = _session_dir(profile_home, user_id, session_id)
    path = directory / "session.json"
    with _lock_for(path):
        record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Deep-research session record is invalid")
    if record.get("user_id") != _identity(user_id):
        raise PermissionError("Deep-research session owner mismatch")
    if record.get("session_id") != str(session_id):
        raise ValueError("Deep-research session ID mismatch")
    return record


def update_session(
    profile_home: str | Path,
    user_id: str | int,
    session_id: str,
    **changes: Any,
) -> dict[str, Any]:
    """Update only known lifecycle fields while preserving session ownership."""
    allowed = {
        "status", "thread_id", "trigger_message_id", "latest_reply_message_id",
        "progress_message_id", "phase", "failure_class", "topic_create_random_id",
        "last_error", "completed_at",
        "artifact_status", "final_response_status",
        "max_turns", "report_format", "depth", "custom_instructions",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("Unsupported deep-research session fields: " + ", ".join(sorted(unknown)))
    if "status" in changes and changes["status"] not in _ALLOWED_STATUS:
        raise ValueError("Invalid deep-research session status")
    if "max_turns" in changes:
        try:
            changes["max_turns"] = int(changes["max_turns"])
        except (TypeError, ValueError):
            raise ValueError("Max turns must be a number")
        if not 1 <= changes["max_turns"] <= MAX_TURNS_CAP:
            raise ValueError(f"Max turns must be between 1 and {MAX_TURNS_CAP}")
    if "report_format" in changes:
        changes["report_format"] = str(changes["report_format"] or "").strip().lower()
        if changes["report_format"] not in REPORT_FORMATS:
            raise ValueError("Report format must be md, pdf, or docx")
    if "depth" in changes:
        changes["depth"] = str(changes["depth"] or "").strip().lower()
        if changes["depth"] not in RESEARCH_DEPTHS:
            raise ValueError("Research depth must be standard, deep, or exhaustive")
    if "custom_instructions" in changes:
        changes["custom_instructions"] = str(changes["custom_instructions"] or "").strip()[:2000]
    if "artifact_status" in changes and changes["artifact_status"] not in {"not_checked", "ready", "invalid"}:
        raise ValueError("Invalid deep-research artifact status")
    if "final_response_status" in changes and changes["final_response_status"] not in {"pending", "delivered", "failed", "interrupted"}:
        raise ValueError("Invalid deep-research final-response status")
    if "last_error" in changes:
        changes["last_error"] = str(changes["last_error"] or "")[:500]
    for field in ("thread_id", "trigger_message_id", "latest_reply_message_id", "progress_message_id"):
        if field in changes and changes[field] is not None:
            changes[field] = str(changes[field])[:100]
    for field in ("phase", "failure_class"):
        if field in changes and changes[field] is not None:
            changes[field] = str(changes[field])[:160]
    path = _session_dir(profile_home, user_id, session_id) / "session.json"
    with _lock_for(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("user_id") != _identity(user_id):
            raise PermissionError("Deep-research session owner mismatch")
        record.update(changes)
        record["updated_at"] = _utc_now()
        _write_json(path, record)
    return record


def claim_session_for_resume(
    profile_home: str | Path,
    user_id: str | int,
    session_id: str,
    *,
    accept_artifact_drift: bool = False,
) -> dict[str, Any] | None:
    """Atomically claim one paused/interrupted session before starting resume work.

    ``accept_artifact_drift`` is only for an explicit owner reconcile: the
    on-disk notes/report are newer than their checkpoint snapshots, and the
    owner has reviewed the difference. Identity, topic, task-ledger, and
    evidence discrepancies still block.
    """
    path = _session_dir(profile_home, user_id, session_id) / "session.json"
    with _lock_for(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("user_id") != _identity(user_id):
            raise PermissionError("Deep-research session owner mismatch")
        if record.get("status") not in {"paused", "interrupted"}:
            return None
        validate_resume_state(profile_home, user_id, session_id, accept_artifact_drift=accept_artifact_drift)
        record["status"] = "starting"
        record["phase"] = "resuming"
        record["final_response_status"] = "pending"
        record["updated_at"] = _utc_now()
        _write_json(path, record)
    update_checkpoint_metadata(profile_home, user_id, session_id)
    return record


def _read_checkpoint_artifact(session_dir: Path, filename: str) -> dict[str, Any]:
    """Read a named artifact only when it is a regular file inside this session."""
    path = session_dir / filename
    try:
        if path.is_symlink():
            return {"file": filename, "content": "", "error": "symbolic links are not checkpointed"}
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(session_dir) or not resolved.is_file():
            return {"file": filename, "content": "", "error": "not a regular session file"}
        return {"file": filename, "content": resolved.read_text(encoding="utf-8", errors="replace")}
    except FileNotFoundError:
        return {"file": filename, "content": ""}
    except Exception as exc:
        return {"file": filename, "content": "", "error": type(exc).__name__}


_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid"})


def _url_identity(value: Any) -> tuple[str, str] | None:
    """Canonicalize an HTTP(S) URL for evidence matching, returning URL and host.

    The key identifies the page, not the byte string that named it: a ``www.``
    prefix and tracker query params (``utm_*``, ``fbclid``, ``gclid``) never
    distinguish two citations of the same page. Other query params are kept,
    so pages that differ only by a meaningful query still differ.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip().rstrip(".,;:!?)]}"))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().removeprefix("www.")
        netloc = parsed.netloc.lower()
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
            netloc = userinfo + "@" + hostport.removeprefix("www.")
        else:
            netloc = netloc.removeprefix("www.")
        query = urlencode([
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
        ], doseq=True)
        key = urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/") or "/", query, ""))
        return key, host
    except (TypeError, ValueError):
        return None


def _read_url_aliases(evidence: Any, read_url_keys: set[str]) -> dict[str, str]:
    """Requested→final URL pairs for redirect reads, keyed canonically.

    A citation may name either end of a redirect (the URL the agent asked for,
    or the final URL the provider recorded); both resolve to the same read page.
    Only pairs whose target is a recorded read page count.
    """
    stored = evidence.get("read_url_aliases") if isinstance(evidence, dict) else None
    mapping: dict[str, str] = {}
    if isinstance(stored, dict):
        for alias_raw, target_raw in stored.items():
            alias_id = _url_identity(alias_raw)
            target_id = _url_identity(target_raw)
            if alias_id and target_id and target_id[0] in read_url_keys and alias_id[0] != target_id[0]:
                mapping[alias_id[0]] = target_id[0]
    return mapping


_REFERENCE_ROW_BRACKET = re.compile(r"^\s*(?:[-*+]\s*)?\[(\d{1,3})\]\s*:?\s*(.*)$")
_REFERENCE_ROW_NUMBERED = re.compile(r"^\s*(?:[-*+]\s*)?(\d{1,3})[.)]\s+(.*)$")
_REFERENCE_ROW_LINK = re.compile(r"^\s*(?:[-*+]\s*)?\[[^\]]+\]\((https?://[^)\s]+)\)")


def _parse_reference_rows(section: str) -> dict[str, set[str]]:
    """Map citation ids to the URLs on their References rows.

    Accepts ``[n] …``, ``[n]: …``, ``n. …``/``n) …``, and bare markdown-link rows
    (``- [Title](URL)``), which take the id of their position in the list. A row
    with no URL still claims its id, so a citation to it fails instead of
    silently passing; a bracketed number with no row at all is left out, and
    the caller ignores it.
    """
    rows: list[tuple[str | None, str]] = []
    for line in section.splitlines():
        match = _REFERENCE_ROW_BRACKET.match(line) or _REFERENCE_ROW_NUMBERED.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
            continue
        if _REFERENCE_ROW_LINK.match(line):
            rows.append((None, line))
    referenced: dict[str, set[str]] = {}
    for position, (ref_id, rest) in enumerate(rows, start=1):
        if ref_id is None:
            ref_id = str(position)
        urls = set()
        for raw in re.findall(r"https?://[^\s<>\]\[)]+", rest, flags=re.IGNORECASE):
            identity = _url_identity(raw)
            if identity:
                urls.add(identity[0])
        referenced.setdefault(ref_id, set()).update(urls)
    return referenced


def merge_research_evidence(previous: Any, current: Any) -> dict[str, Any]:
    """Merge measured tool evidence from turns without trusting model-authored counts."""
    previous = previous if isinstance(previous, dict) else {}
    current = current if isinstance(current, dict) else {}
    result_urls: set[str] = set()
    read_urls: set[str] = set()
    for value in (previous.get("result_urls", []), current.get("result_urls", [])):
        for item in value if isinstance(value, list) else ():
            identity = _url_identity(item)
            if identity:
                result_urls.add(identity[0])
    for value in (previous.get("read_urls", []), current.get("read_urls", [])):
        for item in value if isinstance(value, list) else ():
            identity = _url_identity(item)
            if identity:
                read_urls.add(identity[0])
    sources: dict[str, dict[str, str]] = {}
    for snapshot in (previous, current):
        values = snapshot.get("read_sources", [])
        for item in values if isinstance(values, list) else ():
            source = source_identity(item)
            if source and _url_identity(source["url"])[0] in read_urls:
                old = sources.get(source["url"], {})
                if not source["title"]:
                    source["title"] = old.get("title", "")
                sources[source["url"]] = source
    aliases: dict[str, str] = {}
    for snapshot in (previous, current):
        stored = snapshot.get("read_url_aliases")
        if isinstance(stored, dict):
            for alias_raw, target_raw in stored.items():
                alias_id = _url_identity(alias_raw)
                target_id = _url_identity(target_raw)
                if alias_id and target_id and alias_id[0] != target_id[0]:
                    aliases[alias_id[0]] = target_id[0]
    for source in sources.values():
        alias_id = _url_identity(source.get("requested_url")) if source.get("requested_url") else None
        final_id = _url_identity(source["url"])
        if alias_id and final_id and alias_id[0] != final_id[0]:
            aliases[alias_id[0]] = final_id[0]
    # An alias is only evidence for a page that was actually read; a redirect
    # never manufactures a second read page or a second host for gate 1.
    aliases = {alias: target for alias, target in aliases.items() if target in read_urls}
    try:
        # Each call supplies a run-wide snapshot, not a delta; max avoids counting
        # that same snapshot repeatedly as tool results checkpoint the session.
        searches = max(0, int(previous.get("searches", 0)), int(current.get("searches", 0)))
    except (TypeError, ValueError):
        searches = max(0, int(previous.get("searches", 0) or 0))
    return {
        "searches": searches,
        "result_urls": sorted(result_urls),
        "read_urls": sorted(read_urls),
        "read_sources": sorted(sources.values(), key=lambda row: row["url"]),
        "read_url_aliases": dict(sorted(aliases.items())),
    }


def validate_research_artifacts(session_dir: str | Path, evidence: Any) -> tuple[bool, str]:
    """Require observed source reads, a source ledger, and citations tied to those reads.

    This is intentionally evidence-based rather than a report-length or elapsed-time check.
    ``evidence`` must come from successful web-extract tool results, not the report text.
    """
    directory = Path(session_dir).resolve(strict=True)
    report = _read_checkpoint_artifact(directory, "report.md").get("content", "")
    notes = _read_checkpoint_artifact(directory, "research_notes.md").get("content", "")
    evidence = evidence if isinstance(evidence, dict) else {}
    read_ids = {_url_identity(item) for item in evidence.get("read_urls", []) if isinstance(item, str)}
    read_ids.discard(None)
    read_hosts = {host for _url, host in read_ids}
    read_url_keys = {url for url, _host in read_ids}
    alias_map = _read_url_aliases(evidence, read_url_keys)
    if len(read_ids) < 2 or len(read_hosts) < 2:
        return False, (
            "Fewer than two successfully read source pages from independent websites are recorded "
            f"(recorded: {len(read_ids)} page(s) from {len(read_hosts)} website(s))."
        )

    if not notes.strip():
        return False, "The private source ledger is missing; source findings have not been documented."
    note_ids = {_url_identity(raw) for raw in re.findall(r"https?://[^\s<>\]\[)]+", notes, flags=re.IGNORECASE)}
    note_ids.discard(None)
    noted_read_keys = {alias_map.get(url, url) for url, _host in note_ids}
    documented = {item for item in read_ids if item[0] in noted_read_keys}
    if documented != read_ids or len({host for _url, host in documented}) < 2:
        return False, "The private source ledger does not document the source pages actually read."

    if not report.strip():
        return False, "A non-empty cited report has not been written."
    references_headings = list(re.finditer(
        r"(?im)^\s{0,3}#{0,6}\s*(?:references|sources|bibliography)\b", report))
    if not references_headings:
        return False, "The report has no References or Sources section."
    accepted_keys = read_url_keys | set(alias_map)
    references_heading = references_headings[-1]
    referenced_urls = _parse_reference_rows(report[references_heading.start():])
    for heading in reversed(references_headings):
        parsed = _parse_reference_rows(report[heading.start():])
        if any(urls.intersection(accepted_keys) for urls in parsed.values()):
            references_heading, referenced_urls = heading, parsed
            break
    body = report[:references_heading.start()]
    cited_ids = set(re.findall(r"\[(\d{1,3})\]", body))
    # A bracketed number with no reference row is not a citation (appendix and
    # table numbering, stray ids); a cited row counts when at least one of its
    # URLs is a read page, even if the row also lists unread URLs.
    if any(ref_id in referenced_urls
           and not referenced_urls[ref_id].intersection(accepted_keys)
           for ref_id in cited_ids):
        return False, "The report cites a source without a matching successfully read page."
    evidenced_refs = {
        ref_id for ref_id in cited_ids.intersection(referenced_urls)
        if referenced_urls[ref_id].intersection(accepted_keys)
    }
    cited_read_urls = {
        alias_map.get(url, url) for ref_id in evidenced_refs
        for url in referenced_urls[ref_id]
        if alias_map.get(url, url) in read_url_keys
    }
    cited_hosts = {host for url, host in read_ids if url in cited_read_urls}
    if len(evidenced_refs) < 2 or len(cited_read_urls) < 2 or len(cited_hosts) < 2:
        return False, "The report does not cite at least two independently hosted pages that were successfully read."
    return True, ""


def save_checkpoint(
    profile_home: str | Path,
    user_id: str | int,
    session_id: str,
    *,
    todos: Any = _UNSET,
    phase: str | None = None,
    status: str | None = None,
    last_error: str | None = None,
    latest_reply_message_id: str | int | None = None,
    research_evidence: Any = _UNSET,
    tool_history: Any = _UNSET,
) -> dict[str, Any]:
    """Atomically checkpoint actual todos and the current private notes/report files."""
    directory = resolve_session_dir(profile_home, user_id, session_id)
    session = load_session(profile_home, user_id, session_id)
    path = directory / "checkpoint.json"
    try:
        with _lock_for(path):
            previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if path.is_file() and (not isinstance(previous, dict) or previous.get("session_id") != str(session_id)):
                raise ValueError("Deep-research checkpoint is invalid; refusing to replace saved state")
            current_todos = previous.get("todos", []) if todos is _UNSET else todos
            if not isinstance(current_todos, list):
                current_todos = []
            current_evidence = (previous.get("research_evidence", {}) if research_evidence is _UNSET
                                else merge_research_evidence(previous.get("research_evidence"), research_evidence))
            if research_evidence is not _UNSET:
                # Only observed tool metadata generates entries, never search hits,
                # report citations, or an unvalidated read-only resume checkpoint.
                write_observed_source_ledger(directory, current_evidence.get("read_sources", []))
            record = {
                "session_id": str(session_id),
                "status": status or session.get("status") or previous.get("status", "running"),
                "phase": str(phase or previous.get("phase") or "research")[:160],
                "todos": current_todos,
                "research_evidence": current_evidence if isinstance(current_evidence, dict) else {},
                "tool_history": (previous.get("tool_history", []) if tool_history is _UNSET else tool_history),
                "query": session.get("query"),
                "user_id": session.get("user_id"),
                "source_ledger": _read_checkpoint_artifact(directory, "research_notes.md"),
                "partial_report": _read_checkpoint_artifact(directory, "report.md"),
                "last_error": str(last_error if last_error is not None else previous.get("last_error", ""))[:500],
                "session_dir": str(directory.resolve()),
                "chat_id": str(session.get("chat_id") or ""),
                "chat_type": str(session.get("chat_type") or "unknown"),
                "thread_id": session.get("thread_id"),
                "trigger_message_id": session.get("trigger_message_id"),
                "latest_reply_message_id": (str(latest_reply_message_id)
                                            if latest_reply_message_id is not None
                                            else session.get("latest_reply_message_id")),
                "progress_message_id": session.get("progress_message_id"),
                "failure_class": session.get("failure_class"),
                "artifact_status": session.get("artifact_status", "not_checked"),
                "final_response_status": session.get("final_response_status", "pending"),
                "updated_at": _utc_now(),
            }
            _write_json(path, record)
    except Exception:
        raise
    changes: dict[str, Any] = {"phase": record["phase"]}
    if status is not None:
        changes["status"] = status
    if last_error is not None:
        changes["last_error"] = last_error
    if latest_reply_message_id is not None:
        changes["latest_reply_message_id"] = str(latest_reply_message_id)
    update_session(profile_home, user_id, session_id, **changes)
    return record


def update_checkpoint_metadata(
    profile_home: str | Path,
    user_id: str | int,
    session_id: str,
    *,
    latest_reply_message_id: Any = _UNSET,
    progress_message_id: Any = _UNSET,
) -> dict[str, Any]:
    """Refresh lightweight task-chain metadata without rereading large research artifacts."""
    directory = resolve_session_dir(profile_home, user_id, session_id)
    session = load_session(profile_home, user_id, session_id)
    path = directory / "checkpoint.json"
    with _lock_for(path):
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if path.is_file() and (not isinstance(previous, dict) or previous.get("session_id") != str(session_id)):
            raise ValueError("Deep-research checkpoint is invalid; refusing to replace saved state")
        if not path.is_file():
            previous = {
                "session_id": str(session_id), "todos": [],
                "source_ledger": {"file": "research_notes.md", "content": ""},
                "partial_report": {"file": "report.md", "content": ""},
                "phase": str(session.get("phase") or "starting"),
                "last_error": "",
            }
        previous.update({
            "status": str(session.get("status") or "starting"),
            "session_dir": str(directory.resolve()),
            "chat_id": str(session.get("chat_id") or ""),
            "chat_type": str(session.get("chat_type") or "unknown"),
            "thread_id": session.get("thread_id"),
            "trigger_message_id": session.get("trigger_message_id"),
            "latest_reply_message_id": session.get("latest_reply_message_id"),
            "progress_message_id": session.get("progress_message_id"),
            "failure_class": session.get("failure_class"),
            "artifact_status": session.get("artifact_status", "not_checked"),
            "final_response_status": session.get("final_response_status", "pending"),
            "updated_at": _utc_now(),
        })
        if latest_reply_message_id is not _UNSET:
            previous["latest_reply_message_id"] = (
                str(latest_reply_message_id) if latest_reply_message_id is not None else None
            )
        if progress_message_id is not _UNSET:
            previous["progress_message_id"] = (
                str(progress_message_id) if progress_message_id is not None else None
            )
        _write_json(path, previous)
    return previous


def load_checkpoint(profile_home: str | Path, user_id: str | int, session_id: str) -> dict[str, Any]:
    """Load an owner-bound checkpoint; return an empty initial checkpoint when none exists."""
    directory = resolve_session_dir(profile_home, user_id, session_id)
    path = directory / "checkpoint.json"
    if not path.is_file():
        session = load_session(profile_home, user_id, session_id)
        return {
            "session_id": str(session_id), "status": session.get("status", "starting"),
            "phase": session.get("phase", "starting"), "todos": [],
            "source_ledger": {"file": "research_notes.md", "content": ""},
            "partial_report": {"file": "report.md", "content": ""},
        }
    with _lock_for(path):
        value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("session_id") != str(session_id):
        raise ValueError("Deep-research checkpoint is invalid")
    return value


def validate_resume_state(profile_home: str | Path, user_id: str | int, session_id: str,
                           *, accept_artifact_drift: bool = False) -> dict[str, Any]:
    """Read and compare durable state without repairing, overwriting or claiming it.

    A discrepancy requires owner review, not a model-invented recovery plan.
    Older checkpoints may omit newly introduced optional fields, but must retain
    their original identity, topic, task ledger and artifact snapshots.
    ``accept_artifact_drift`` only waives the notes/report snapshot-equality
    check, for an explicit owner reconcile; every other discrepancy still blocks.
    """
    directory = resolve_session_dir(profile_home, user_id, session_id)
    session = load_session(profile_home, user_id, session_id)
    path = directory / "checkpoint.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("checkpoint.json is missing or is a symbolic link")
    checkpoint = load_checkpoint(profile_home, user_id, session_id)
    issues = []
    for key in ("session_id", "chat_id", "thread_id", "session_dir", "status"):
        if key not in checkpoint:
            issues.append(f"{key} is missing from checkpoint.json")
    for key in ("session_id", "chat_id", "thread_id", "session_dir", "status",
                "artifact_status", "final_response_status", "progress_message_id",
                "latest_reply_message_id", "query", "user_id"):
        if key in checkpoint and checkpoint.get(key) != session.get(key):
            issues.append(f"{key} differs between session.json and checkpoint.json")
    if not session.get("thread_id"):
        issues.append("the original research topic is missing")
    if not str(session.get("query") or "").strip():
        issues.append("the original question is missing")
    todos = checkpoint.get("todos")
    if not isinstance(todos, list):
        issues.append("the task ledger is missing or invalid")
    else:
        ids = set()
        for item in todos:
            if (not isinstance(item, dict) or not item.get("id") or not item.get("content")
                    or item.get("status") not in {"pending", "in_progress", "completed", "cancelled"}):
                issues.append("the task ledger contains an invalid task")
                break
            if item["id"] in ids:
                issues.append("the task ledger contains duplicate task IDs")
                break
            ids.add(item["id"])
    evidence = checkpoint.get("research_evidence", {})
    if (not isinstance(evidence, dict)
            or not isinstance(evidence.get("searches", 0), int)
            or evidence.get("searches", 0) < 0
            or any(not isinstance(evidence.get(key, []), list) for key in ("read_urls", "result_urls"))):
        issues.append("the durable research evidence is invalid")
    elif (not isinstance(evidence.get("read_sources", []), list)
          or any(not source_identity(item) or _url_identity(item.get("url")) not in {
              _url_identity(url) for url in evidence.get("read_urls", [])
          } for item in evidence.get("read_sources", []))):
        issues.append("the durable source-read identities are invalid or lack successful read evidence")
    history = checkpoint.get("tool_history")
    has_work = isinstance(evidence, dict) and bool(evidence.get("searches") or evidence.get("read_urls") or evidence.get("result_urls"))
    if has_work and (not isinstance(history, list) or not history):
        issues.append("the prior tool-outcome history is missing; source counters alone cannot reconstruct inspected outputs")
    elif history is not None and (not isinstance(history, list) or any(not isinstance(item, dict) for item in history)):
        issues.append("the prior tool-outcome history is invalid")
    if any(isinstance(checkpoint.get(key), dict) and checkpoint[key].get("content")
           for key in ("source_ledger", "partial_report")) and not evidence:
        issues.append("saved artifacts exist but their successful source evidence is missing")
    for key, filename in (("source_ledger", "research_notes.md"), ("partial_report", "report.md")):
        saved = checkpoint.get(key)
        actual = _read_checkpoint_artifact(directory, filename)
        if not isinstance(saved, dict) or not isinstance(saved.get("content"), str):
            issues.append(f"the {filename} checkpoint snapshot is missing or invalid")
        elif actual.get("error") or saved.get("error") or not accept_artifact_drift and saved["content"] != actual.get("content"):
            issues.append(f"{filename} differs from its checkpoint snapshot or cannot be read")
    if session.get("final_response_status") == "delivered":
        issues.append("final delivery is already recorded; replay could duplicate the report")
    if session.get("artifact_status") == "ready":
        valid, issue = validate_research_artifacts(directory, evidence)
        if not valid:
            issues.append(f"the ready report fails evidence validation: {issue}")
    if issues:
        raise ValueError("; ".join(issues))
    return checkpoint


def find_session_for_topic(profile_home: str | Path, chat_id: str | int,
                           thread_id: str | int | None) -> dict[str, Any] | None:
    """Find the research owner for an exact dedicated topic, across user scopes.

    Group session keys may be shared, so looking up only the incoming user's
    directory would let a different user bypass the research owner's boundary.
    Read metadata only; never reconcile or mutate saved sessions here.
    """
    if thread_id in (None, "", 0, "0"):
        return None
    base = Path(profile_home).expanduser().resolve(strict=False) / "deep_research_sessions"
    if not base.exists():
        return None
    matches = []
    for owner in base.iterdir():
        if not _USER_ID_RE.fullmatch(owner.name):
            continue
        if owner.is_symlink() or not owner.is_dir():
            raise ValueError("Cannot verify saved research topic ownership: invalid owner directory")
        for directory in owner.iterdir():
            if not _SESSION_ID_RE.fullmatch(directory.name):
                continue
            path = directory / "session.json"
            if directory.is_symlink() or path.is_symlink() or not directory.is_dir():
                raise ValueError("Cannot verify saved research topic ownership: invalid session path")
            try:
                record = load_session(profile_home, owner.name, directory.name)
            except Exception as exc:
                raise ValueError("Cannot verify saved research topic ownership: unreadable session metadata") from exc
            if str(record.get("chat_id")) == str(chat_id) and str(record.get("thread_id")) == str(thread_id):
                matches.append(record)
    if len(matches) > 1:
        raise ValueError("Multiple research sessions claim the same Telegram topic; owner review is required")
    return matches[0] if matches else None


def list_sessions(profile_home: str | Path, user_id: str | int, *, limit: int = 10) -> list[dict[str, Any]]:
    """List only this Telegram user's sessions, newest first."""
    base = _base_dir(profile_home, user_id)
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for directory in base.iterdir():
        if not _SESSION_ID_RE.fullmatch(directory.name) or not directory.is_dir():
            continue
        try:
            rows.append(load_session(profile_home, user_id, directory.name))
        except Exception:
            continue
    rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
    return rows[: max(1, min(int(limit), 50))]
