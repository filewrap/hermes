"""Hermes Web Designer add-on (Phase 1 spine).

A load-on-demand skill pack + toolset + explicit state machine — there is
no add-on registry in Hermes. Scope is web designing ONLY (HTML/CSS/JS/TS).
Pipeline: questioning -> brief -> research -> wireframe -> tokens -> build
-> run -> judge -> fix loop -> deliver.

Package map (Phase 1):
- ``types`` — stages (30), run status machine, budgets, Finding/Verdict.
- ``rules`` — the R001-R110 registry (R070/R071 merged), judger mapping.
- ``store`` — durable per-run state under ``<home>/web-designer/<run_id>/``.
- ``load_gate`` — stage 1: load only for website/webapp requests.
- ``machine`` — the 30-stage state machine + checkpointing + rollback.
- ``boundaries`` — executor-enforced caps and never-without-approval gates.
- ``tokens`` — 3-tier W3C Design Tokens JSON + generated CSS variables.
- ``judgers`` — the five stage 19-23 judgers (visual, a11y, performance,
  content, responsive).
- ``fix_loop`` — capped fix -> re-judge loop (3 per issue).
- ``tool_registry`` — the 64 audit tools as a reference registry; only the
  locally verifiable checkers are live.
- ``skills`` — the Phase 2 skill pack registry (audit §3, 3.1-3.45):
  stage -> the ``skills/web-design/<name>/SKILL.md`` files the coordinator
  loads with ``skill_view`` when the stage starts.
"""
from __future__ import annotations

from .types import DEFAULT_BUDGETS, Budgets, Finding, RunRecord, Verdict

__all__ = ["DEFAULT_BUDGETS", "Budgets", "Finding", "RunRecord", "Verdict"]
