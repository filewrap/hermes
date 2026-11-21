---
name: usability-testing
description: "Five users, realistic tasks, severity-rated findings."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, testing]
    category: web
    related_skills: [web-design, usability-heuristics, ab-testing]
    web_designer_stages: [preview]
---

# Usability Testing Skill

Watching representative users complete realistic tasks is the only way to learn where a page actually fails them. This skill covers planning small task-based rounds, rating what is found by severity, and fixing and retesting between rounds. It does not run a live test session itself: in a web-designer run Hermes applies the same checklist in its judger role as a simulated review, and that simulated review is not a substitute for five real users. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- On wireframes, prototypes and live sites, in repeated small rounds with fixes between rounds.
- When a built page needs to be checked against real tasks before it is judged ready to deliver.
- When the same problem keeps appearing and the team needs to know whether a fix actually worked.
- When a wireframe or prototype is ready and the structure should be tested before visual design is applied to it.
- When a live site has changed and the changed flow needs to be walked through by representative users again.

It is not for proving a conversion lift, for benchmarking a number, or for settling a design argument by opinion. A five-user round uncovers problems; it does not measure how much better one variant performs than another. Quantitative benchmarking needs a larger sample, and conversion claims belong to A/B testing.

## Prerequisites

- The run is active, the coordinator loaded this skill for its stage(s).
- A preview exists to review: the preview stage record holds `url_or_path`, `screenshots` and `console_log`.
- Realistic tasks are defined before the review starts, each tied to a named user goal from the brief.
- The usability-heuristics skill is available for the severity lens, since findings are rated, not merely listed.
- The participants are representative of the audience named in the brief; testing with the wrong audience answers the wrong question.

## How to Run

1. Define the tasks first, in plain sentences, each with a clear success state: what the user must achieve, not which control they must click.
2. Pick the round size from the Quick Reference: about five users per segment for problem discovery, twenty or more for benchmarking.
3. Watch for hesitation and confusion, using think-aloud protocol where real users are present; in a simulated review, walk each task step by step and note every point where the next action is unclear.
4. Record each finding as a specific, task-based observation: what the user tried, where they stalled, and what happened.
5. Rate every finding by severity, fix the severe findings first, and retest in a fresh round.
6. Check that a fixed problem does not survive into the next round before moving on.
7. Between rounds, fix only what the findings name; do not redesign the page on the strength of one round's opinions.
8. Keep the task list stable across rounds where the flow has not changed, so a repeat round can show whether the fix worked.
9. Record the round size and the tasks walked, so a later round can be compared with this one.
10. Share the severity-rated findings with the coordinator, not raw opinion, so the fix order is decided by severity.
11. Stop a round when its findings are recorded and rated; do not keep testing until a preferred answer appears.
12. After the retest, confirm the fixed finding is gone before the stage is recorded as complete.

## Quick Reference

- Round size for problem discovery: about 5 users per segment (Nielsen & Landauer 1993; Virzi 1992).
- What 5 users uncover: most severe issues for problem discovery.
- Quantitative benchmarking: about 20 or more users.
- Protocol: think-aloud, with realistic tasks.
- Cadence: repeated small rounds, with fixes between rounds.
- Severity: rated per finding; the same problem must not survive two rounds.
- Tasks: realistic, tied to named user goals from the brief, stated as outcomes not clicks.
- Findings: specific, task-based and severity-rated, naming the step where the user stalled.
- Retest: a fresh round after fixes, with the task list stable where the flow is unchanged.
- Evidence per finding: what the user tried, where they stalled, what happened.
- Participants: representative of the brief's audience.
- Fix order: severe, task-blocking findings first.

## Procedure

### 1. What it is

Watching representative users complete realistic tasks with think-aloud protocol, in small rounds of ~5 users per segment (Nielsen & Landauer 1993; Virzi 1992) — 5 finds most severe issues for problem-discovery; quantitative benchmarking needs ~20+.

### 2. How Hermes applies it

Hermes defines tasks, watches for hesitation/confusion (in its judger role it simulates this checklist), rates severity, fixes and retests — and tells the user plainly that its own simulated review is not a substitute for 5 real users.

### 3. What it returns to the stage

The preview stage record in `gateway/web_designer/machine.py` (`STAGE_RECORDS`) requires `url_or_path`, `screenshots` and `console_log`. This skill's work fills the review behind those keys: the task list and the severity-rated findings are recorded against the preview, with each finding naming the task it blocks, the step where the user hesitated, and its severity. A finding that cannot be tied to a task is not recorded as a usability finding. The record also notes, plainly, whether the review was simulated or carried out with real users, so a simulated review is never presented as a user test.

Severity is the ordering principle of the record: severe, task-blocking findings come first, and the fix order follows the rating rather than the order in which findings were noticed. The retest result is recorded against the same finding, so the record shows whether a fix held or the problem survived into the next round. A round that produces no task-based finding is recorded as such, with the tasks walked and the round size stated, rather than left blank.

The severity rating is what makes the record actionable: without it, the fix loop has no order to follow and the most visible problem wins over the most blocking one. The record therefore keeps the rating with the finding from the first round through the retest, so a finding that was severe in round one cannot quietly become minor in round two without a reason. That reason, where one exists, is recorded with the retest result.

### 4. Good looks like

Findings are specific, task-based and severity-rated; the same problem does not survive two rounds.

A round that ends with a severity-rated list and a retest date is a round that did its job. The list is the deliverable, not the session itself. Where the same problem appears in two rounds, the second round's record names the first, so the survival is visible. That visibility is what keeps a severe problem from being fixed in words and surviving in the build.

## Pitfalls

- Claiming a 5-user test proves a conversion lift. It uncovers problems; it does not measure lifts.
- Treating a simulated review as a substitute for 5 real users. The audit says plainly that it is not.
- Recording vague findings such as "the page is confusing" with no task, no step, and no severity.
- Running one large round instead of repeated small rounds with fixes between them.
- Fixing cosmetic issues while a severe, task-blocking finding survives into the next round.
- Testing with tasks that describe the interface ("click the menu") instead of the user's goal ("locate the price").
- Testing with participants who do not match the brief's audience, then generalising the findings to that audience.
- Changing the task list between rounds without noting it, so a fix cannot be shown to have worked.
- Rating findings by opinion rather than by severity, so cosmetic issues jump the queue.
- Stopping after one round while a severe finding is still unfixed.
- Recording a finding with no task attached, so nothing can be retested against it.

## Verification

- Every finding names a task, the step where hesitation appeared, and a severity rating.
- Severe findings are fixed and retested; no fixed problem reappears in the next round.
- The record states whether the review was simulated or with real users; a simulated review is never described as a user test.
- The round size is stated, and the tasks are realistic and tied to named user goals.
- The task list is stable across rounds where the flow is unchanged.
- Fix order follows severity, not the order findings were noticed.
- Each retest result is recorded against the finding it retests.
- No finding is recorded without the task it blocks.
- The visual judger checks the related rules: layout, hierarchy and legibility findings raised here are judged there. The skill does the work and the judger checks it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: preview. Returns: preview stage-record keys `url_or_path`, `screenshots`, `console_log`, with task-based severity-rated findings recorded against the preview. Judger boundary: visual judger checks the related layout and hierarchy rules.
