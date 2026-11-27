"""Session groups — external-agent sidebar groups on the ``sessions`` row (F2, wave 5).

The external agent keeps membership on each session entry's ``category`` field and an
ordered catalog of group names beside it (``src/gateway/session-groups.ts``,
``src/gateway/session-group-catalog.ts``). Hermes keeps the membership half
only: ``group_name`` on the row. There is no separate catalog to drift —
:func:`SessionGroupsMixin.list_session_groups` derives the catalog from live
rows, so a group exists exactly while a session is in it. Rename moves every
member; delete ungroups every member (the external agent's delete clears the category
too — only its ``put`` refuses to drop a non-empty group, and Hermes has no
``put``).

A new session is filed under its project automatically (repo root, else
working directory) by
:func:`hermes_state_worker_lifecycle.insert_session_row_in_transaction`;
the agent moves a session with the ``sessions`` tool's patch ``group``
(empty string clears it), and renames or deletes a whole group with that
tool's ``group_rename`` / ``group_delete`` actions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes_state")


def normalize_session_group_name(name) -> Optional[str]:
    """Trimmed group name; None/blank -> None (ungrouped)."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("group must be a string")
    return name.strip() or None


def _project_dir_name(path) -> Optional[str]:
    """Basename of *path* when it names a project directory, else None.

    A filesystem root, the user's home, and the Hermes home are not projects:
    a session started there stays ungrouped instead of filing every bare
    shell under its home directory's name.
    """
    if not path or not isinstance(path, str):
        return None
    candidate = Path(path.strip()).expanduser()
    name = candidate.name
    if not name or candidate.parent == candidate:
        return None
    try:
        if candidate == Path.home():
            return None
    except Exception as e:  # health: allow BLE001 -- home lookup is best-effort; a failure just skips the check
        logger.debug("group auto-file: home lookup failed (%s); skipping the home check", e)
    try:
        from hermes_constants import get_default_hermes_root

        if candidate == get_default_hermes_root():
            return None
    except Exception as e:  # health: allow BLE001 -- same best-effort home check as above
        logger.debug("group auto-file: Hermes-home lookup failed (%s); skipping the home check", e)
    return name


def _derive_session_group_name(*, git_repo_root=None, cwd=None) -> Optional[str]:
    """Project-based default group for a brand-new session row: the git repo
    root's basename first, else the working directory's basename."""
    return _project_dir_name(git_repo_root) or _project_dir_name(cwd)


class SessionGroupsMixin:
    """Group membership on the ``sessions`` row (``group_name``)."""

    def set_session_group(self, session_id: str, group) -> bool:
        """File a session — and its compression lineage, the same conversation —
        under *group*; None/blank clears it (ungrouped). False when the session
        does not exist."""
        return self._set_lineage_column(
            "group_name", session_id, normalize_session_group_name(group))

    def list_session_groups(self) -> list[dict[str, Any]]:
        """Derived group catalog: every group on a live row, with member counts,
        most recently active first."""
        self.flush_for_read()
        rows = self._read_all(
            "SELECT group_name, COUNT(*) AS session_count,"
            " MAX(COALESCE(last_activity_at, started_at)) AS last_active"
            " FROM sessions WHERE group_name IS NOT NULL AND TRIM(group_name) != ''"
            " GROUP BY group_name ORDER BY last_active DESC, group_name ASC", ())
        return [{"name": row["group_name"], "session_count": row["session_count"],
                 "last_active": row["last_active"]} for row in (dict(r) for r in rows)]

    def rename_session_group(self, name: str, to: str) -> int:
        """Move every member of *name* to *to*; renaming onto an existing group
        merges into it. Returns members moved; 0 when the names match.
        Raises ValueError for a blank name or an unknown group."""
        from_name = normalize_session_group_name(name)
        to_name = normalize_session_group_name(to)
        if not from_name or not to_name:
            raise ValueError("group rename requires non-empty names")
        if from_name == to_name:
            return 0

        def _do(conn):
            members = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE group_name = ?", (from_name,)).fetchone()[0]
            if not members:
                raise ValueError(f"unknown session group: {from_name}")
            conn.execute(
                "UPDATE sessions SET group_name = ? WHERE group_name = ?", (to_name, from_name))
            return members

        return self._execute_write(_do)

    def delete_session_group(self, name: str) -> int:
        """Ungroup every member of *name*; the group then no longer exists.
        Returns members ungrouped. Raises ValueError for a blank name or an
        unknown group."""
        group = normalize_session_group_name(name)
        if not group:
            raise ValueError("group delete requires a non-empty name")

        def _do(conn):
            members = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE group_name = ?", (group,)).fetchone()[0]
            if not members:
                raise ValueError(f"unknown session group: {group}")
            conn.execute(
                "UPDATE sessions SET group_name = NULL WHERE group_name = ?", (group,))
            return members

        return self._execute_write(_do)
