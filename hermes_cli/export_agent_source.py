"""Hermes-home discovery for the reverse migration (Hermes -> the external agent).

Mirrors the external agent's ``extensions/migrate-hermes/source.ts`` in the other
direction: where the forward provider discovers a Hermes home to read from,
this module discovers the same home to export from. A Hermes home is the
active profile home (``HERMES_HOME`` / ``active_profile``) unless the caller
names an explicit ``--source`` path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


# Directories/files that never become live external-agent config. They are copied
# into the export archive for manual review, exactly like the forward
# provider archives them on import.
ARCHIVE_DIRS = (
    "plugins",
    "sessions",
    "logs",
    "cron",
    "mcp-tokens",
    "plans",
    "workspace",
    "skins",
    "kanban",
    "pairing",
    "platforms",
)

ARCHIVE_FILES = (
    "state.db",
    "hermes_state.db",
    "projects.db",
    "response_store.db",
    "memory_store.db",
    "verification_evidence.db",
    "kanban.db",
    "retaindb_queue.db",
    "gateway_state.json",
    "channel_directory.json",
    "channel_aliases.json",
    "processes.json",
    "feishu_comment_pairing.json",
)

# Markers that prove a directory is a Hermes home (same set as the forward
# provider's HERMES_STATE_MARKERS).
STATE_MARKERS = (
    "config.yaml",
    ".env",
    "auth.json",
    "active_profile",
    "SOUL.md",
    "AGENTS.md",
    "skills",
    "memories",
    *ARCHIVE_DIRS,
    *ARCHIVE_FILES,
)


@dataclass
class ArchiveEntry:
    """One archive-only path: ``id`` labels it in the plan/report."""

    id: str
    path: Path
    relative: str


@dataclass
class HermesExportSource:
    """Everything the exporter reads from one Hermes home."""

    root: Path
    config_path: Optional[Path] = None
    env_path: Optional[Path] = None
    auth_path: Optional[Path] = None
    soul_path: Optional[Path] = None
    agents_path: Optional[Path] = None
    tools_path: Optional[Path] = None
    memory_path: Optional[Path] = None
    user_path: Optional[Path] = None
    skills_dirs: list[Path] = field(default_factory=list)
    archive: list[ArchiveEntry] = field(default_factory=list)

    @property
    def has_state(self) -> bool:
        """Whether any Hermes state was found at all."""
        return bool(
            self.config_path
            or self.env_path
            or self.auth_path
            or self.soul_path
            or self.agents_path
            or self.memory_path
            or self.user_path
            or self.skills_dirs
            or self.archive
        )


def discover_hermes_source(explicit: Optional[str] = None) -> HermesExportSource:
    """Resolve the Hermes home to export.

    ``explicit`` is a ``--source`` path used verbatim; otherwise the active
    profile home from :func:`hermes_constants.get_hermes_home` (which already
    honours ``HERMES_HOME`` and ``active_profile``).
    """
    root = (
        Path(os.path.expanduser(explicit)).resolve()
        if explicit
        else get_hermes_home().resolve()
    )
    source = HermesExportSource(root=root)
    for key, candidate, is_dir in (
        ("config_path", root / "config.yaml", False),
        ("env_path", root / ".env", False),
        ("auth_path", root / "auth.json", False),
        ("soul_path", root / "SOUL.md", False),
        ("agents_path", root / "AGENTS.md", False),
        ("tools_path", root / "TOOLS.md", False),
        ("memory_path", root / "memories" / "MEMORY.md", False),
        ("user_path", root / "memories" / "USER.md", False),
    ):
        probe = candidate.is_dir() if is_dir else candidate.is_file()
        if probe:
            setattr(source, key, candidate)
    # Daily memory files ride along with the main memory file.
    skills_root = root / "skills"
    if skills_root.is_dir():
        source.skills_dirs.append(skills_root)
    for dirname in ARCHIVE_DIRS:
        candidate = root / dirname
        if candidate.is_dir():
            source.archive.append(
                ArchiveEntry(f"archive:{dirname}", candidate, dirname)
            )
    for filename in ARCHIVE_FILES:
        candidate = root / filename
        if candidate.is_file():
            source.archive.append(
                ArchiveEntry(f"archive:{filename}", candidate, filename)
            )
    return source


def has_hermes_state(root: Path) -> bool:
    """Whether ``root`` looks like a Hermes home at all."""
    return any((root / marker).exists() for marker in STATE_MARKERS)


def profile_name_for(root: Path) -> str:
    """The profile name a Hermes home belongs to (``default`` at the root)."""
    if root.parent.name == "profiles":
        return root.name
    return "default"
