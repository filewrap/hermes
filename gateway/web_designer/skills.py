"""Stage -> skill registry for the Phase 2 skill pack (audit §3, 3.1-3.45).

The machine reads this, never prose: ``skills_for_stage`` is the load
list the coordinator feeds to ``skill_view`` when a stage starts, and
``web_designer_status`` reports it for the run's current stage. Each
skill's own SKILL.md declares the same stages in
``metadata.hermes.web_designer_stages``; the registry test cross-checks
the two so prose and code cannot drift apart.

Skills are working knowledge for DOING a stage. Judgers (stages 19-23)
check rules; no skill loads for a judger stage, and no skill grades its
own output. Stages with no entry take no skills: the load gate (1) and
scope freeze (4) are coordinator decisions, fix_loop (24) reuses the
skills of the stage whose verdict failed, and checkpoints (25),
delivery (26) and the machine stages (27/28/30) are human or machine
operations, not design work. No skill in this registry auto-loads.
"""
from __future__ import annotations

from .types import STAGE_KEYS, STAGE_ORDER

__all__ = [
    "ALL_SKILLS",
    "SKILL_DIR",
    "SKILL_STAGES",
    "STAGE_SKILLS",
    "skills_for_stage",
    "stages_for_skill",
]

#: Directory each skill lives in: skills/web-design/<name>/SKILL.md.
SKILL_DIR = "skills/web-design"

#: Skill name -> the coordinator stages that load it (audit §3 order).
#: Every skill names 1-3 stages; every stage in 2-18 except load_gate
#: (1) and scope_freeze (4) has at least one skill.
SKILL_STAGES: dict[str, tuple[str, ...]] = {
    # 3.1-3.12
    "typographic-scale": ("font", "tokens"),
    "font-pairing": ("font",),
    "readability": ("copy", "font"),
    "colour-theory": ("direction", "colour"),
    "colour-contrast": ("colour", "theme"),
    "semantic-colour": ("colour", "tokens", "components"),
    "css-grid": ("build",),
    "flexbox": ("build",),
    "spacing-system": ("tokens", "build"),
    "visual-hierarchy": ("wireframe", "direction"),
    "content-hierarchy": ("copy", "wireframe"),
    "information-architecture": ("ia",),
    # 3.13-3.24
    "navigation-design": ("ia", "components"),
    "responsive-design": ("build",),
    "iconography": ("icon", "components"),
    "imagery": ("direction", "build"),
    "motion-design": ("components", "build"),
    "micro-interactions": ("components", "build"),
    "usability-heuristics": ("wireframe", "build"),
    "laws-of-ux": ("ia", "wireframe"),
    "user-research": ("intake", "research", "brief"),
    "user-journeys": ("research", "ia"),
    "wireframing": ("wireframe",),
    "prototyping": ("preview",),
    # 3.25-3.34
    "usability-testing": ("preview",),
    "ab-testing": ("revisions",),
    "design-systems": ("components", "tokens"),
    "design-tokens": ("tokens",),
    "component-states": ("components", "build"),
    "dark-mode": ("theme", "tokens"),
    "branding-consistency": ("brief", "direction"),
    "ux-copywriting": ("copy",),
    "microcopy": ("copy", "components"),
    "conversion-cro": ("copy", "direction"),
    # 3.35-3.45
    "trust-design": ("copy", "wireframe"),
    "form-design": ("wireframe", "components"),
    "form-validation": ("components", "build"),
    "empty-states": ("wireframe", "components"),
    "edge-states": ("components", "build"),
    "accessibility-wcag": ("tokens", "build"),
    "keyboard-accessibility": ("components", "build"),
    "performance-design": ("stack", "build"),
    "seo-design": ("copy", "build"),
    "touch-targets": ("components", "build"),
    "onboarding-design": ("wireframe", "copy"),
}

#: Every skill in audit order.
ALL_SKILLS: tuple[str, ...] = tuple(SKILL_STAGES)

#: Stage -> skills that load for it, in pipeline order.
STAGE_SKILLS: dict[str, tuple[str, ...]] = {
    stage: tuple(
        name for name in ALL_SKILLS if stage in SKILL_STAGES[name]
    )
    for stage in STAGE_KEYS
}


def skills_for_stage(stage: str) -> tuple[str, ...]:
    """Skill names the coordinator loads when *stage* starts.

    An empty tuple means the stage takes no skills (see the module
    docstring). Raises ValueError for a stage that does not exist.
    """
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown web-designer stage {stage!r}")
    return STAGE_SKILLS[stage]


def stages_for_skill(name: str) -> tuple[str, ...]:
    """Stages that load *name*. Raises ValueError for an unknown skill."""
    if name not in SKILL_STAGES:
        raise ValueError(f"unknown web-designer skill {name!r}")
    return SKILL_STAGES[name]
