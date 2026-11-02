"""Tool-observed source identities, separate from model-authored findings/citations."""
from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_READ_TOOLS = {"web_extract", "web_extract_urls", "fetch_url", "browser", "browser_navigate", "browser_open", "browser_read", "browser_snapshot"}
_LEDGER_START = "<!-- hermes:observed-source-reads:start -->"
_LEDGER_END = "<!-- hermes:observed-source-reads:end -->"


def source_identity(item: Any) -> dict[str, str] | None:
    """Keep the tool's URL verbatim; canonical URL keys are only for matching/counts."""
    if not isinstance(item, dict):
        return None
    url = item.get("url") or item.get("href")
    if not isinstance(url, str) or any(char.isspace() for char in url.strip()):
        return None
    url = url.strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().removeprefix("www.")
    except ValueError:
        return None
    metadata = item.get("metadata")
    title = item.get("title") or item.get("page_title") or (metadata.get("title") if isinstance(metadata, dict) else "")
    source = {"url": url, "host": host, "title": " ".join(str(title or "").split())[:500]}
    # The extract layer reports the redirect target in ``url`` and the URL the
    # agent asked for in ``metadata.sourceURL``; keep both, so a citation of
    # either end still resolves to this one read page. The requested URL is an
    # alias, never a second read page.
    requested = metadata.get("sourceURL") if isinstance(metadata, dict) else None
    if not isinstance(requested, str) or not requested.strip():
        # Already-shaped rows (e.g. re-merged from a checkpoint) carry it directly.
        requested = item.get("requested_url")
    if isinstance(requested, str) and requested.strip() and requested.strip() != url:
        try:
            requested_parsed = urlsplit(requested.strip())
            if requested_parsed.scheme.lower() in {"http", "https"} and requested_parsed.hostname:
                source["requested_url"] = requested.strip()
        except ValueError:
            pass
    return source


def successful_read_sources(name: str, payload: Any) -> list[dict[str, str]]:
    if name not in _READ_TOOLS or not isinstance(payload, dict):
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        rows = [payload]
    sources = []
    for item in rows:
        if (not isinstance(item, dict) or item.get("error") or item.get("success") is False
                or item.get("bot_detection_warning") or item.get("blocked_by_policy")):
            continue
        if str(item.get("status") or "").lower() in {"error", "failed", "pending", "skipped", "timeout"}:
            continue
        content = (item.get("content") or item.get("raw_content") or item.get("text")
                   or item.get("markdown") or item.get("snapshot"))
        if not isinstance(content, str) or not content.strip():
            continue
        source = source_identity(item)
        if source:
            sources.append(source)
    return sources


def write_observed_source_ledger(directory: Path, sources: list[dict[str, str]]) -> None:
    """Refresh only the runtime section, preserving findings and refusing unsafe paths.

    This records retrieval, not agreement with a claim or successful final delivery.
    Called inside the session's checkpoint lock, never during read-only resume validation.
    """
    if not sources:
        return
    path = directory / "research_notes.md"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("The private source ledger is not a regular session file")
    notes = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [_LEDGER_START, "## Successfully read source pages (runtime observations)", "",
             "These entries confirm retrieved content only. Claim support, dates, limitations, and citations must be verified separately.", ""]
    for source in sorted(sources, key=lambda row: row["url"]):
        title = source.get("title") or "Title not returned by the tool"
        lines.extend([f"- Source: {source['host']}", f"  Title: {html.escape(title)}", f"  URL: {source['url']}"])
    lines.append(_LEDGER_END)
    section = "\n".join(lines)
    pattern = re.escape(_LEDGER_START) + r".*?" + re.escape(_LEDGER_END)
    if re.search(pattern, notes, flags=re.DOTALL):
        updated = re.sub(pattern, lambda _match: section, notes, flags=re.DOTALL)
    else:
        updated = notes.rstrip() + ("\n\n" if notes.strip() else "") + section + "\n"
    if updated == notes:
        return
    fd, temporary = tempfile.mkstemp(prefix=".source-ledger-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
