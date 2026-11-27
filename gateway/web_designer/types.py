"""Shared types for the web-designer add-on (Phase 1 spine).

The add-on is a load-on-demand skill pack + toolset + explicit state
machine — there is no add-on registry in Hermes, so nothing here registers
one. These types are the contract every stage, judger, boundary check and
UI card reads and writes; a stage that cannot fill its contract refuses to
complete instead of faking output.

Stages follow the audit's coordinator (30 stages, in order). Stage 27
(checkpointing), 28 (rollback) and 30 (routing/audit) are machine-level
operations: the machine records them as stage completions, it does not ask
an agent to perform them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Stages (audit coordinator 6.1-6.30, in order) ---------------------------
STAGES: tuple[tuple[int, str, str], ...] = (
    (1, "load_gate", "Load gate and coordinator boot"),
    (2, "intake", "Adaptive intake questioning"),
    (3, "brief", "Brief synthesis and intent inference"),
    (4, "scope_freeze", "Scope freeze, constraints and stack boundary"),
    (5, "research", "Research and inspiration pass"),
    (6, "ia", "Information architecture"),
    (7, "wireframe", "Wireframe — low-fidelity structure"),
    (8, "copy", "Content draft and copy skeleton"),
    (9, "direction", "Design direction exploration"),
    (10, "font", "Font decision rule"),
    (11, "colour", "Colour decision rule"),
    (12, "theme", "Theme decision rule"),
    (13, "icon", "Icon decision rule"),
    (14, "tokens", "Design tokens — single source of truth"),
    (15, "components", "Component inventory and system"),
    (16, "stack", "Build plan and stack choice"),
    (17, "build", "Build execution"),
    (18, "preview", "Local run and preview"),
    (19, "judge_visual", "Visual judger"),
    (20, "judge_a11y", "Accessibility judger — WCAG 2.2 AA"),
    (21, "judge_perf", "Performance judger — budgets and Core Web Vitals"),
    (22, "judge_content", "Content and copy judger"),
    (23, "judge_responsive", "Responsive and cross-viewport judger"),
    (24, "fix_loop", "Fix loop — prioritised, capped, evidence-driven"),
    (25, "checkpoints", "Human checkpoint — gates, not micromanagement"),
    (26, "delivery", "Delivery and handoff package"),
    (27, "checkpointing", "Checkpointing and state persistence"),
    (28, "rollback", "Failure recovery and rollback"),
    (29, "revisions", "Post-delivery revision handling"),
    (30, "routing_audit", "Coordinator guardrails, routing, audit log"),
)
STAGE_KEYS: tuple[str, ...] = tuple(key for _, key, _ in STAGES)
STAGE_ORDER: dict[str, int] = {key: order for order, key, _ in STAGES}
STAGE_NAMES: dict[str, str] = {key: name for _, key, name in STAGES}

#: Human gates (audit 5.35 / coordinator 6.25). (a) brief, (b) wireframe +
#: IA, (c) design direction, (d) pre-delivery preview. The brief gate in
#: Phase 1 is stage 3/4 (brief + frozen scope); the machine pauses at each
#: gate key and resumes only on an explicit human decision.
GATE_STAGES: dict[str, str] = {
    "brief": "brief",
    "wireframe": "wireframe",
    "direction": "direction",
    "preview": "preview",
}

# --- Run status ---------------------------------------------------------------
# Mirrors the autonomous run store (gateway/autonomous_state.py) with one
# addition: ``awaiting_checkpoint`` is a durable human-gate pause, distinct
# from ``needs_input`` (the agent waits for a fact) and from ``paused``
# (approval timeout counted as a deny, audit 5.33).
RUN_STATUSES = (
    "idle", "running", "awaiting_checkpoint", "needs_input",
    "paused", "done", "stopped", "failed",
)
_TRANSITIONS: dict[str, frozenset[str]] = {
    "idle": frozenset({"running"}),
    "running": frozenset(
        {"awaiting_checkpoint", "needs_input", "done", "stopped", "failed"}
    ),
    "awaiting_checkpoint": frozenset({"running", "paused", "stopped", "failed"}),
    "needs_input": frozenset({"running", "stopped", "failed"}),
    "paused": frozenset({"running", "stopped"}),
    "done": frozenset(),
    "stopped": frozenset(),
    "failed": frozenset(),
}


def transition_allowed(current: str, target: str) -> bool:
    """True when the run-status transition is legal; anything else fails closed."""
    return target in _TRANSITIONS.get(current, frozenset())


# --- Budgets (audit boundaries 5.1-5.10, 5.16, 5.29) ------------------------
@dataclass(frozen=True)
class Budgets:
    """Executor-enforced caps. Values are the audit's numbers, not defaults
    to tune at runtime; raising one needs an explicit user order."""

    iteration_cap: int = 20            # 5.1 build iterations, brief to delivery
    same_error_cap: int = 3             # 5.2 identical error in consecutive fixes
    no_progress_cap: int = 3            # 5.3 iterations with no change
    fix_loop_cap: int = 3               # 5.4 / 6.24 fix->re-judge cycles per issue
    wall_clock_build_minutes: int = 120  # 5.5
    wall_clock_revision_minutes: int = 45  # 5.6
    token_budget: int = 150_000         # 5.7 total tokens per build
    token_warn_fraction: float = 0.8     # 5.7 warn at 80%
    cost_budget_usd: float = 2.0        # 5.8 per build
    daily_cost_budget_usd: float = 10.0  # 5.8 per calendar day, Asia/Kolkata
    tool_call_cap: int = 30             # 5.9 per run
    same_tool_repeat_cap: int = 3       # 5.9 same tool + same args in a row
    subagent_depth_cap: int = 2         # 5.10 spawn depth (agent -> sub-agent)
    subagent_fanout_cap: int = 4        # 5.10 siblings at one layer
    revision_cap: int = 2               # 5.16 revision rounds per deliverable
    max_files: int = 200                # 5.29 project files
    max_total_mb: float = 50.0          # 5.29 project total
    max_file_mb: float = 2.0            # 5.29 single generated file
    approval_timeout_minutes: int = 30  # 5.33 timeout is a deny


DEFAULT_BUDGETS = Budgets()

# --- Findings and verdicts ----------------------------------------------------
SEVERITIES = ("blocker", "defect", "change", "observation")
FINDING_STATUSES = ("open", "fixed", "parked", "accepted")


@dataclass
class Finding:
    """One judger finding. Every field is mandatory: a judger never reports
    a bare pass/fail without the exact violation and its evidence."""

    rule_id: str
    judger: str
    severity: str  # blocker | defect | change | observation
    location: str  # page / element / viewport
    expected: str
    actual: str
    evidence: str  # screenshot path, file:line, measured value — never "trust me"
    status: str = "open"  # open | fixed | parked | accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "judger": self.judger,
            "severity": self.severity, "location": self.location,
            "expected": self.expected, "actual": self.actual,
            "evidence": self.evidence, "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            rule_id=str(data["rule_id"]), judger=str(data["judger"]),
            severity=str(data["severity"]), location=str(data["location"]),
            expected=str(data["expected"]), actual=str(data["actual"]),
            evidence=str(data["evidence"]), status=str(data.get("status", "open")),
        )


@dataclass
class Verdict:
    """One judger's verdict over one build. ``passed`` is True only when
    every CHECKED rule passed; ``unchecked`` lists rules the judger could
    not evaluate (no live browser, needs a human eye) — a verdict with
    unchecked rules is never a silent pass, and ``unjudged`` blocks delivery."""

    judger: str
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)  # rule ids not evaluated
    measured: dict[str, Any] = field(default_factory=dict)
    unjudged: bool = False  # could not run at all (e.g. no preview)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker" and f.status == "open"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "judger": self.judger, "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "unchecked": list(self.unchecked), "measured": dict(self.measured),
            "unjudged": self.unjudged,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Verdict:
        return cls(
            judger=str(data["judger"]), passed=bool(data["passed"]),
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
            unchecked=list(data.get("unchecked", [])),
            measured=dict(data.get("measured", {})),
            unjudged=bool(data.get("unjudged", False)),
        )


# --- Run record ---------------------------------------------------------------
@dataclass
class RunRecord:
    """Durable per-run state. Persisted as JSON after every stage (see
    store.py); a resume with the same run_id continues from the last
    checkpoint, skipping completed stages."""

    run_id: str
    thread_id: str
    user_id: str
    chat_id: str
    home: str  # profile home the run lives under
    status: str = "idle"
    stage: str = "load_gate"
    completed_stages: list[str] = field(default_factory=list)
    stage_data: dict[str, Any] = field(default_factory=dict)
    brief: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    tokens_path: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    iterations: int = 0
    started_at: str = ""
    last_step_at: str = ""
    fix_attempts: dict[str, int] = field(default_factory=dict)
    revision_rounds: int = 0
    checkpoint_decisions: list[dict[str, Any]] = field(default_factory=list)
    delivery: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def project_dir(self) -> str:
        return f"{self.home}/web-designer/{self.run_id}/project"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "thread_id": self.thread_id,
            "user_id": self.user_id, "chat_id": self.chat_id,
            "home": self.home, "status": self.status, "stage": self.stage,
            "completed_stages": list(self.completed_stages),
            "stage_data": dict(self.stage_data),
            "brief": dict(self.brief), "scope": dict(self.scope),
            "tokens_path": self.tokens_path,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd, "tool_calls": self.tool_calls,
            "iterations": self.iterations, "started_at": self.started_at,
            "last_step_at": self.last_step_at,
            "fix_attempts": dict(self.fix_attempts),
            "revision_rounds": self.revision_rounds,
            "checkpoint_decisions": list(self.checkpoint_decisions),
            "delivery": dict(self.delivery),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        return cls(
            run_id=str(data["run_id"]), thread_id=str(data["thread_id"]),
            user_id=str(data["user_id"]), chat_id=str(data["chat_id"]),
            home=str(data["home"]), status=str(data.get("status", "idle")),
            stage=str(data.get("stage", "load_gate")),
            completed_stages=list(data.get("completed_stages", [])),
            stage_data=dict(data.get("stage_data", {})),
            brief=dict(data.get("brief", {})), scope=dict(data.get("scope", {})),
            tokens_path=str(data.get("tokens_path", "")),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            tool_calls=int(data.get("tool_calls", 0)),
            iterations=int(data.get("iterations", 0)),
            started_at=str(data.get("started_at", "")),
            last_step_at=str(data.get("last_step_at", "")),
            fix_attempts=dict(data.get("fix_attempts", {})),
            revision_rounds=int(data.get("revision_rounds", 0)),
            checkpoint_decisions=list(data.get("checkpoint_decisions", [])),
            delivery=dict(data.get("delivery", {})),
        )
