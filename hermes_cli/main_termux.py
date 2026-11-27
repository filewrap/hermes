"""Termux startup helpers: bundled-skills sync stamp and update-check prefetch.

Split out of ``hermes_cli.main`` (file-size ratchet); the names stay importable from the facade
so existing imports keep working. ``PROJECT_ROOT`` is derived here the same way the facade
derives it (``_startup_fast.project_root_str()``), so the fingerprint is identical.
"""

from __future__ import annotations

import os
from pathlib import Path
from hermes_cli import _startup_fast

PROJECT_ROOT = Path(_startup_fast.project_root_str())


def _is_termux_startup_environment(env: dict[str, str] | None = None) -> bool:
    """Import-safe Termux check for cold-start-sensitive CLI paths."""
    check = env or os.environ
    prefix = str(check.get("PREFIX", ""))
    return bool(
        check.get("TERMUX_VERSION")
        or "com.termux/files/usr" in prefix
        or prefix.startswith("/data/data/com.termux/")
    )


def _read_packed_ref(common_dir: Path, ref: str) -> str | None:
    """Look up a ref in .git/packed-refs without spawning git.

    packed-refs lines look like ``<sha> <ref>`` with optional ``^<sha>``
    peel lines and ``#``-prefixed comments / ``# pack-refs with:`` header.
    """
    try:
        text = (common_dir / "packed-refs").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip()
    return None


def _read_git_revision_fingerprint(repo_root: Path) -> str | None:
    """Return a cheap checkout fingerprint without spawning git."""
    git_dir = repo_root / ".git"
    try:
        if git_dir.is_file():
            for line in git_dir.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "gitdir" and value.strip():
                    git_dir = (repo_root / value.strip()).resolve()
                    break
        # Worktrees point HEAD at a per-worktree gitdir but pack their refs
        # in the main repo's gitdir (referenced via ``commondir``). Resolve
        # that up front so packed-refs lookups hit the right file.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            try:
                rel = commondir_file.read_text(encoding="utf-8-sig", errors="replace").strip()
                if rel:
                    common_dir = (git_dir / rel).resolve()
            except OSError:
                pass
        head = (git_dir / "HEAD").read_text(encoding="utf-8-sig", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            # Loose refs may live in the worktree gitdir OR the common dir
            # (branches created via `git worktree add` typically live in the
            # common dir's refs/heads/).
            for candidate in (git_dir, common_dir):
                ref_file = candidate / ref
                if ref_file.exists():
                    return f"git:{ref}:{ref_file.read_text(encoding='utf-8-sig', errors='replace').strip()}"
            packed_sha = _read_packed_ref(common_dir, ref)
            if packed_sha:
                return f"git:{ref}:{packed_sha}"
            # Ref name is known but unresolved — still stable across launches,
            # and the version/release fallback in the caller will invalidate
            # after `hermes update`.
            return f"git:{ref}:unresolved"
        return f"git:HEAD:{head}"
    except OSError:
        return None


def _termux_bundled_skills_fingerprint() -> str:
    """Cheap invalidation key for Termux bundled-skill startup sync."""
    git_fp = _read_git_revision_fingerprint(PROJECT_ROOT)
    if git_fp:
        return git_fp
    from hermes_cli.version_info import get_version_info

    version_info = get_version_info()
    code_identity = version_info.commit or version_info.derived_version
    skills_dir = PROJECT_ROOT / "skills"
    try:
        stat = skills_dir.stat()
        return f"skills:{code_identity}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return f"skills:{code_identity}:missing"


def _termux_bundled_skills_stamp_path() -> Path:
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "skills" / ".termux_bundled_sync_stamp"


def _termux_bundled_skills_sync_needed() -> bool:
    if not _is_termux_startup_environment():
        return True
    if os.environ.get("HERMES_TERMUX_FORCE_SKILLS_SYNC") == "1":
        return True
    try:
        stamp = _termux_bundled_skills_stamp_path()
        return stamp.read_text(encoding="utf-8-sig").strip() != _termux_bundled_skills_fingerprint()
    except OSError:
        return True


def _mark_termux_bundled_skills_synced() -> None:
    if not _is_termux_startup_environment():
        return
    try:
        stamp = _termux_bundled_skills_stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(_termux_bundled_skills_fingerprint() + "\n", encoding="utf-8")
    except OSError:
        pass


def _sync_bundled_skills_for_startup() -> bool:
    """Sync bundled skills, but skip unchanged Termux checkouts cheaply.

    Hashing every bundled skill is safe but expensive on older Android
    storage. The git/ref stamp keeps post-update correctness: a changed
    checkout revision forces one real sync, then later starts skip it.
    """
    if _is_termux_startup_environment() and not _termux_bundled_skills_sync_needed():
        return False

    from tools.skills_sync import sync_skills

    sync_skills(quiet=True)
    _mark_termux_bundled_skills_synced()
    return True


def _termux_should_prefetch_update_check() -> bool:
    if not _is_termux_startup_environment():
        return True
    return os.environ.get("HERMES_TERMUX_PREFETCH_UPDATES") == "1"
