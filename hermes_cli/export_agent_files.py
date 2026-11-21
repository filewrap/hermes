"""Workspace files and skills for the reverse migration.

Workspace files (``SOUL.md``, ``AGENTS.md``, ``IDENTITY.md``,
``MEMORY.md``, ``USER.md``, ``TOOLS.md``) are copied verbatim into the
agent workspace — the forward provider copies the same files the other
way. Skills are discovered like the forward provider's ``skills.ts``:
every directory containing a ``SKILL.md``, skipping tooling caches and
inactive ``_org`` mirrors, and copied whole into the agent workspace's
``skills/`` directory.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Optional

from hermes_cli.export_agent_report import ExportItem

EXCLUDED_SKILL_DIRS = {
    ".git",
    ".github",
    ".hub",
    ".archive",
    ".venv",
    "venv",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

SKILL_SUPPORT_DIRS = {"references", "templates", "assets", "scripts"}

# Hermes workspace files -> external-agent workspace files (same names).
WORKSPACE_FILES = (
    "SOUL.md",
    "AGENTS.md",
    "IDENTITY.md",
    "MEMORY.md",
    "USER.md",
    "TOOLS.md",
)


def sanitize_name(name: str) -> str:
    """Lowercase slug for skill directory names (forward parity)."""
    import re

    normalized = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower())
    return normalized.strip("-")


def discover_skill_roots(root: Path) -> list[Path]:
    """Every skill root under ``root``: a dir with a ``SKILL.md``.

    Recurses into subdirectories (Hermes skills nest by category); support
    dirs under a skill root are part of that skill, not separate roots.
    Inactive ``_org`` mirrors are skipped — only the org named by
    ``_org/.active_org`` is live.
    """
    org_root = root / "_org"
    active_org: Optional[str] = None
    active_file = org_root / ".active_org"
    if active_file.is_file():
        try:
            active_org = active_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            active_org = None
    roots: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        has_skill = (directory / "SKILL.md").is_file()
        if has_skill:
            roots.append(directory)
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in EXCLUDED_SKILL_DIRS:
                continue
            if has_skill and entry.name in SKILL_SUPPORT_DIRS:
                continue
            if entry == org_root and not active_org:
                continue
            if directory == org_root and entry.name != active_org:
                continue
            visit(entry)

    visit(root)
    return roots


def skill_frontmatter_name(skill_root: Path) -> str:
    """The skill's declared ``name`` from ``SKILL.md`` frontmatter, if any."""
    from ruamel.yaml.error import YAMLError

    from hermes_yaml import safe_load

    skill_md = skill_root / "SKILL.md"
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return skill_root.name
    match = re.match(r"^\ufeff?---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)", content)
    if not match:
        return skill_root.name
    try:
        frontmatter = safe_load(match.group(1))
    except YAMLError:
        return skill_root.name
    if isinstance(frontmatter, dict):
        name = frontmatter.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return skill_root.name


def plan_workspace_files(
    source_root: Path, workspace_dir: Path, overwrite: bool, files: dict[str, Path]
) -> list[ExportItem]:
    """Plan items for the workspace files present in the source home."""
    items: list[ExportItem] = []
    for filename, source_path in files.items():
        target = workspace_dir / filename
        kind = "memory" if filename in ("MEMORY.md", "USER.md") else "workspace"
        conflict = target.exists() and not overwrite
        items.append(
            ExportItem(
                id=f"{kind}:{filename}",
                kind=kind,
                action="copy",
                source=str(source_path),
                target=str(target),
                status="conflict" if conflict else "planned",
                reason="target already exists" if conflict else None,
            )
        )
    return items


def plan_skills(
    skills_dirs: list[Path], workspace_dir: Path, overwrite: bool
) -> list[ExportItem]:
    """Plan items for every discovered skill, copied whole."""
    planned: list[dict[str, Any]] = []
    for skills_dir in skills_dirs:
        for root in discover_skill_roots(skills_dir):
            name = sanitize_name(root.name)
            if not name:
                continue
            relative = root.relative_to(skills_dir)
            planned.append(
                {
                    "id": "skill:"
                    + ":".join(
                        sanitize_name(part)
                        for part in relative.parts
                        if sanitize_name(part)
                    ),
                    "name": name,
                    "skill_name": skill_frontmatter_name(root),
                    "source": root,
                    "target": workspace_dir / "skills" / name,
                }
            )
    counts: dict[str, int] = {}
    for skill in planned:
        counts[skill["name"]] = counts.get(skill["name"], 0) + 1
    items: list[ExportItem] = []
    for skill in planned:
        collides = counts[skill["name"]] > 1
        target_exists = skill["target"].exists()
        if collides:
            status, reason = (
                "conflict",
                f'multiple Hermes skill directories normalize to "{skill["name"]}"',
            )
        elif target_exists and not overwrite:
            status, reason = "conflict", "target already exists"
        else:
            status, reason = "planned", None
        items.append(
            ExportItem(
                id=skill["id"],
                kind="skill",
                action="copy",
                source=str(skill["source"]),
                target=str(skill["target"]),
                status=status,
                reason=reason,
                details={"skill_name": skill["skill_name"]},
            )
        )
    return items


def copy_tree(source: Path, target: Path, *, overwrite: bool) -> None:
    """Copy a file or directory tree; ``overwrite`` replaces the target."""
    if target.exists() and overwrite:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
