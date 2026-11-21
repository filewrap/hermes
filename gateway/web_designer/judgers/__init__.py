"""The five web-designer judgers (stages 19-23).

Each ``judge`` takes a built project directory and returns a
``types.Verdict``. ``judge_all`` runs all five in stage order and returns
one verdict per judger. A verdict with ``unjudged=True`` blocks delivery;
a verdict with unchecked rules is never a silent pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import a11y, content, performance, responsive, visual
from ..types import Verdict

JUDGERS = ("visual", "a11y", "performance", "content", "responsive")

_JUDGE_FNS = {
    "visual": visual.judge,
    "a11y": a11y.judge,
    "performance": performance.judge,
    "content": content.judge,
    "responsive": responsive.judge,
}


def judge_all(project_dir: str | Path, **kwargs: Any) -> dict[str, Verdict]:
    """Run all five judgers in stage order (visual first, responsive last)."""
    return {name: _JUDGE_FNS[name](project_dir, **kwargs) for name in JUDGERS}


__all__ = ["JUDGERS", "judge_all"]
