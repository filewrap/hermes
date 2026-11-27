"""Archive-only state for the reverse migration.

Hermes plugins, sessions, and scheduled jobs have no live external-agent
equivalent in this export — the forward provider archives them the same
way on import. They are copied into the export's ``archive/`` directory
for manual review, never activated:

- ``archive/sessions/`` — a readable ``sessions-index.json`` (id, title,
  source, model, started, message count) plus a self-contained snapshot
  of each session database (SQLite ``VACUUM INTO``, read-only — a raw
  ``state.db`` copy can omit rows still living in ``state.db-wal``, the
  same reason the forward provider snapshots before archiving).
- ``archive/cron/`` — the verbatim ``jobs.json`` plus a readable
  ``cron-jobs.md`` list. Jobs are NOT recreated as external-agent cron jobs.
- ``archive/plugins/`` — an index of installed plugins and their config
  files, copied verbatim.
- ``archive/hermes-state/`` — every other Hermes-only state file the
  forward provider archives (gateway state, channel directory, …).

The archive snapshot may contain secrets that were inside sessions or
plugin state; it is chmod 0600, lives outside the live external-agent tree, and
the report says so plainly. The secret scan covers live files only.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

from hermes_cli.export_agent_report import ExportItem

logger = logging.getLogger(__name__)


def list_sessions_index(home: Path, *, limit: int = 10000) -> list[dict[str, Any]]:
    """Readable session rows from the home's ``state.db``, if it opens.

    Read-only; a missing or unreadable database yields an empty index
    rather than failing the export. The rich listing is tried first; when
    it cannot run (a partial or older database), a minimal ``SELECT`` over
    the ``sessions`` table still yields an index.
    """
    db_path = home / "state.db"
    if not db_path.is_file():
        return []
    try:
        from hermes_state import SessionDB
    except ImportError:
        return _list_sessions_minimal(db_path, limit=limit)
    try:
        db = SessionDB(db_path=db_path, read_only=True)
    except Exception:
        # An unreadable database still yields an index via the minimal
        # listing; the traceback is the only record of why the rich path failed.
        logger.debug(
            "SessionDB open failed; falling back to a minimal listing", exc_info=True
        )
        return _list_sessions_minimal(db_path, limit=limit)
    try:
        rows = db.list_sessions_rich(
            limit=limit,
            include_archived=True,
            include_hidden=True,
            include_subagents=True,
        )
    except Exception:
        # A partial or older database still yields an index via the minimal
        # listing; the traceback is the only record of why the rich path failed.
        logger.debug(
            "rich session listing failed; falling back to a minimal listing",
            exc_info=True,
        )
        return _list_sessions_minimal(db_path, limit=limit)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("closing SessionDB failed", exc_info=True)
    index: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        index.append(
            {
                "id": str(row.get("id") or row.get("session_id") or ""),
                "title": row.get("title") or None,
                "source": row.get("source") or None,
                "model": row.get("model") or None,
                "started_at": row.get("started_at") or None,
                "last_active": row.get("last_active") or None,
                "message_count": row.get("message_count"),
            }
        )
    return index


def _list_sessions_minimal(db_path: Path, *, limit: int) -> list[dict[str, Any]]:
    """Session rows from a bare ``SELECT`` (no SessionDB schema needed)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        wanted = [
            "id",
            "title",
            "source",
            "model",
            "started_at",
            "last_active",
            "message_count",
        ]
        selected = [col for col in wanted if col in columns]
        if "id" not in selected:
            return []
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{col: row[col] for col in selected} for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def snapshot_sqlite(source: Path, target: Path) -> bool:
    """Self-contained copy of a SQLite database via ``VACUUM INTO``.

    Falls back to a plain file copy (plus ``-wal``/``-shm`` sidecars when
    present) when the database cannot be opened. Returns True when the
    snapshot was written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()
        target.chmod(0o600)
        return True
    except (sqlite3.Error, OSError):
        pass
    try:
        shutil.copyfile(source, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, Path(str(target) + suffix))
        target.chmod(0o600)
        return True
    except OSError:
        return False


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def plan_archive(
    source_root: Path, archive_dir: Path, archive_entries: list, overwrite: bool
) -> list[ExportItem]:
    """Plan items for every archive-only path in the source home."""
    items: list[ExportItem] = []
    for entry in archive_entries:
        target = archive_dir / _archive_target_name(entry.relative)
        conflict = target.exists() and not overwrite
        items.append(
            ExportItem(
                id=entry.id,
                kind="archive",
                action="archive",
                source=str(entry.path),
                target=str(target),
                status="conflict" if conflict else "planned",
                reason="target already exists" if conflict else None,
                message="Archived for manual review; not imported into live external-agent config.",
                details={"archiveRelativePath": entry.relative},
            )
        )
    return items


def _archive_target_name(relative: str) -> str:
    """Where one archived path lands inside ``archive/``."""
    if relative == "cron":
        return "cron"
    if relative in ("sessions", "plugins"):
        return relative
    if relative.endswith(".db"):
        return f"sessions/{relative}"
    if relative in (
        "gateway_state.json",
        "channel_directory.json",
        "channel_aliases.json",
        "processes.json",
        "feishu_comment_pairing.json",
    ):
        return f"hermes-state/{relative}"
    return f"hermes-state/{relative}"


def apply_archive_item(
    item: ExportItem, *, sessions_index: Optional[list[dict[str, Any]]] = None
) -> ExportItem:
    """Execute one archive plan item; returns the item with its outcome."""
    if item.source is None or item.target is None:
        return ExportItem(
            **{**item.__dict__, "status": "error", "reason": "missing source or target"}
        )
    source, target = Path(item.source), Path(item.target)
    if not source.exists():
        return ExportItem(
            **{
                **item.__dict__,
                "status": "skipped",
                "reason": "source no longer present",
            }
        )
    try:
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.suffix == ".db":
            if not snapshot_sqlite(source, target):
                return ExportItem(
                    **{
                        **item.__dict__,
                        "status": "error",
                        "reason": "could not copy database",
                    }
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o600)
    except OSError as exc:
        return ExportItem(**{**item.__dict__, "status": "error", "reason": str(exc)})
    return ExportItem(**{**item.__dict__, "status": "migrated"})


def write_sessions_index(archive_dir: Path, index: list[dict[str, Any]]) -> Path:
    """The readable session list next to the database snapshots."""
    path = archive_dir / "sessions" / "sessions-index.json"
    _write_json(
        path,
        {
            "sessions": index,
            "count": len(index),
            "note": "Archive only. Sessions are not loaded into the external agent; "
            "the state.db snapshot beside this file holds the full transcripts.",
        },
    )
    return path


def write_cron_readme(archive_dir: Path, jobs: list[dict[str, Any]]) -> Path:
    """A readable list of the archived cron jobs (paused, never activated)."""
    lines = [
        "# Archived Hermes cron jobs",
        "",
        "Archive only — these jobs were NOT recreated as external-agent cron jobs.",
        "Recreate the ones you still want with the external agent's scheduler after review.",
        "",
    ]
    if not jobs:
        lines.append("_No cron jobs found in this Hermes home._")
    for job in jobs:
        name = job.get("name") or job.get("id") or "unnamed"
        schedule = job.get("schedule") or {}
        when = (
            schedule.get("cron")
            or schedule.get("expression")
            or schedule.get("kind")
            or "unscheduled"
        )
        enabled = job.get("enabled", True)
        lines.append(
            f"- **{name}** (`{job.get('id')}`) — {when} — {'enabled' if enabled else 'paused'}"
        )
        prompt = (job.get("prompt") or "").strip()
        if prompt:
            first = prompt.splitlines()[0][:120]
            lines.append(f"  - prompt: {first}")
        if job.get("skill") or job.get("skills"):
            lines.append(f"  - skill: {job.get('skill') or job.get('skills')}")
    path = archive_dir / "cron" / "cron-jobs.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_cron_jobs(home: Path) -> list[dict[str, Any]]:
    """Cron jobs from the home's ``cron/jobs.json``, without side effects."""
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.is_file():
        return []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    else:
        jobs = data
    if isinstance(jobs, dict):
        jobs = [
            ({"id": key} | value if isinstance(value, dict) else {"id": key})
            for key, value in jobs.items()
        ]
    return [job for job in jobs if isinstance(job, dict)]


def write_plugins_index(archive_dir: Path, plugins_dir: Path) -> Optional[Path]:
    """An index of installed plugins (names + config files), archive-only."""
    if not plugins_dir.is_dir():
        return None
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(plugins_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for child in children:
        if child.name.startswith("."):
            continue
        entries.append(
            {
                "name": child.name,
                "kind": "directory" if child.is_dir() else "file",
                "note": "Archived only; Hermes plugins do not run under the external agent.",
            }
        )
    path = archive_dir / "plugins" / "plugins-index.json"
    _write_json(path, {"plugins": entries, "count": len(entries)})
    return path
