"""Plan items and the export report for the reverse migration.

A plan is a list of :class:`ExportItem` values, one per thing the export
will write, copy, archive, or ask the user to do by hand — the same item
shape the forward provider plans with, minus the apply-time config-runtime
machinery (the reverse export writes a fresh tree, it never patches a live
external-agent config in place).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExportItem:
    """One planned export step."""

    id: str
    kind: str  # config | workspace | memory | skill | archive | secret-ref | manual
    action: str  # write | copy | append | archive | reference | review
    source: Optional[str] = None
    target: Optional[str] = None
    status: str = "planned"  # planned | conflict | skipped | migrated | error
    reason: Optional[str] = None
    message: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "action": self.action,
            "status": self.status,
        }
        if self.source is not None:
            out["source"] = self.source
        if self.target is not None:
            out["target"] = self.target
        if self.reason is not None:
            out["reason"] = self.reason
        if self.message is not None:
            out["message"] = self.message
        if self.details:
            out["details"] = self.details
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExportItem:
        return cls(
            id=str(raw["id"]),
            kind=str(raw["kind"]),
            action=str(raw["action"]),
            source=raw.get("source"),
            target=raw.get("target"),
            status=str(raw.get("status", "planned")),
            reason=raw.get("reason"),
            message=raw.get("message"),
            details=dict(raw.get("details") or {}),
        )


def manual_item(
    item_id: str, source: str, message: str, recommendation: str
) -> ExportItem:
    """An item the user must handle by hand after the export."""
    return ExportItem(
        id=item_id,
        kind="manual",
        action="review",
        source=source,
        status="planned",
        message=message,
        details={"recommendation": recommendation},
    )


def summarize_items(items: list[ExportItem]) -> dict[str, int]:
    """Counts per status, for the plan summary and the report."""
    summary = {"planned": 0, "migrated": 0, "skipped": 0, "conflict": 0, "error": 0}
    for item in items:
        summary[item.status] = summary.get(item.status, 0) + 1
    summary["total"] = len(items)
    return summary


@dataclass
class ExportPlan:
    """The full export plan: what will be written, and what needs a human."""

    source_root: str
    target_root: str
    agent_id: str
    items: list[ExportItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)  # referenced, never valued
    next_steps: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return summarize_items(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": "hermes-export-external-agent",
            "source": self.source_root,
            "target": self.target_root,
            "agent_id": self.agent_id,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
            "warnings": self.warnings,
            "env_vars": self.env_vars,
            "next_steps": self.next_steps,
        }


@dataclass
class ExportReport:
    """The outcome of an applied export, written as ``export-report.json``."""

    plan: ExportPlan
    output_dir: str
    validated: bool = False
    validation_errors: list[str] = field(default_factory=list)
    zip_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data = self.plan.to_dict()
        data.update(
            {
                "output_dir": self.output_dir,
                "validated": self.validated,
                "validation_errors": self.validation_errors,
            }
        )
        if self.zip_path:
            data["zip_path"] = self.zip_path
        return data

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def render_summary(self) -> str:
        """Human-readable summary printed after the export."""
        summary = self.plan.summary
        lines = [
            f"Exported Hermes home {self.plan.source_root} -> {self.output_dir}",
            f"Agent id: {self.plan.agent_id}",
            f"Items: {summary['total']} total, {summary['migrated']} written, "
            f"{summary['skipped']} skipped, {summary['conflict']} conflict, {summary['error']} error",
        ]
        if self.plan.env_vars:
            lines.append(
                "Environment variables to set in the external agent (values never exported):"
            )
            lines.extend(f"  - {name}" for name in self.plan.env_vars)
        manual = [item for item in self.plan.items if item.kind == "manual"]
        if manual:
            lines.append(
                f"{len(manual)} item(s) need manual review (see export-report.json):"
            )
            lines.extend(f"  - {item.message}" for item in manual[:10])
            if len(manual) > 10:
                lines.append(f"  - ... and {len(manual) - 10} more")
        for warning in self.plan.warnings:
            lines.append(f"Warning: {warning}")
        if self.validation_errors:
            lines.append("Validation failed:")
            lines.extend(f"  - {error}" for error in self.validation_errors)
        elif self.validated:
            lines.append(
                "Validation: exported openclaw.json and models.json parse and match the external-agent provider shape; no secret values found in live files."
            )
        for step in self.plan.next_steps:
            lines.append(f"Next: {step}")
        return "\n".join(lines) + "\n"
