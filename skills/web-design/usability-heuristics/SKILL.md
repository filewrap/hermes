---
name: usability-heuristics
description: "Ten checks that catch interface problems before users do."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, usability]
    category: web
    related_skills: [web-design, laws-of-ux, usability-testing]
    web_designer_stages: [wireframe, build]
---

# Nielsen's Usability Heuristics Skill

Evaluate an interface against Jakob Nielsen's 10 usability heuristics, the default evaluation lens for any interface. This skill covers the heuristic walkthrough and severity rating. It does not cover real user testing — a heuristic evaluation is not a substitute for watching real users — and it does not cover the Laws of UX, which have their own skill. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- As the default evaluation lens for any interface, during design and in heuristic evaluation of a built page.
- On wireframes, before visual design, and again on the built page.
- When a page feels wrong but no single rule names the problem.
- When a fix must be ordered: severity decides what is fixed first.
- When an error path, an undo path or a help path must be checked before delivery.
- Not as a substitute for real user testing: the heuristics catch problems an evaluator can see, not the ones only a real user reveals.
- Not as a checklist to pass once and forget: the same lens applies at every stage.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage or stages.
- A wireframe or a built page exists to evaluate — the heuristics need an interface, not a brief.
- The audience and their top tasks are known, so the evaluation follows real tasks rather than wandering the interface.
- The flows are known, so the walkthrough can follow the happy path, the error path and the exit.

## How to Run

1. Walk the interface task by task, following the top tasks from the journey stage.
   - Follow the happy path first, then the error path, then the exit.
   - Note hesitation points even when no heuristic is obviously broken.
2. Check each screen against all 10 heuristics in order.
   - Visibility of system status: is something always happening visibly?
   - Match with the real world: does the language follow the visitor's world, not the system's?
   - User control and freedom: is undo and exit cheap?
   - Consistency and standards: does the same action look and work the same everywhere?
   - Error prevention: is the error designed out before it is reported?
   - Recognition over recall: is the choice visible, not remembered?
   - Flexibility and efficiency: is there a faster way for a returning visitor?
   - Aesthetic and minimalist design: is anything on screen that the task does not need?
   - Help users recognise, diagnose and recover from errors: does the error say what happened and how to fix it?
   - Help and documentation: is help there when the task needs it, and is it easy to reach?
3. For every problem, note the heuristic broken, where it happens, and how often a visitor would meet it.
4. Rate severity 0–4, weighing frequency, impact and persistence.
   - Frequency: how often a visitor meets the problem.
   - Impact: how hard it is to get past it.
   - Persistence: whether it blocks the visitor once or every time.
5. Fix catastrophes before polish: severity-4 problems first, then the rest in severity order.
6. Record the findings, ratings and fixes in the stage record, and re-check after each fix.
7. Re-check after any fix, and record what changed.
   - A fixed problem is walked again, task by task, before the stage closes.
   - The findings recorded are the ones in the final build, with no severity-4 problem left open.

## Quick Reference

- The 10 heuristics (Nielsen 1994, refined 2020, unchanged since): visibility of system status; match with the real world; user control and freedom; consistency and standards; error prevention; recognition over recall; flexibility and efficiency of use; aesthetic and minimalist design; help users recognise, diagnose and recover from errors; help and documentation.
- Severity: 0–4 (frequency x impact x persistence).
- Order: fix catastrophes before polish.
- The list is never a substitute for real user testing.

## Procedure

### 1. What it is

Jakob Nielsen's 1994 heuristics (refined 2020, unchanged since): visibility of system status; match with the real world; user control/freedom; consistency/standards; error prevention; recognition over recall; flexibility/efficiency; aesthetic/minimalist design; help users recognise, diagnose and recover from errors; help and documentation.

### 2. How Hermes applies it

Hermes uses the 10 as its judger checklist, scores severity 0-4 (frequency x impact x persistence), and fixes catastrophes before polish; it never treats the list as a substitute for real user testing.

### 3. What it returns to the stage

- `wireframe` stage record: `pages` carries the heuristic findings per wireframe page — each problem with its heuristic, severity rating and planned fix — so structure problems are caught before visual design.
- `build` stage record: `files` carries the fixes; `build_log` notes the heuristic findings closed on the built page and any that remain open.

### 4. Good looks like

System status is always visible, undo/exit is cheap, the same action looks and works the same everywhere, and errors are prevented before they are reported.

## Pitfalls

- Treating the list as a substitute for real user testing: heuristics catch evaluator-visible problems, not the hesitation and confusion only a real user shows.
- Rating every problem the same: without severity 0–4, a cosmetic nit and a catastrophe compete for the same fix time.
- Polishing before fixing catastrophes: a polished page with a severity-4 problem is still a broken page.
- Checking screens instead of tasks: a heuristic walk that wanders the interface misses the problems that sit on the real paths.
- Visibility of system status forgotten on slow actions: a visitor who cannot tell whether anything is happening assumes nothing is.
- Error prevention skipped in favour of error messages: a good error message is worse than a design that made the error impossible.
- Consistency checked only within one page: the same action looks different on the next page, and the visitor must relearn it.
- Recognition over recall ignored in navigation: labels that demand the visitor remember where a page lives instead of showing them.
- Help and documentation missing where the task is hard: the one place a visitor needs help is the one place it is absent.

## Verification

- All 10 heuristics were checked on each screen, task by task.
- Every problem carries its heuristic, its location, and a severity rating of 0–4.
- Catastrophes (severity 4) are fixed before polish; no severity-4 problem remains open at delivery.
- System status is always visible, undo and exit are cheap, the same action looks and works the same everywhere, and errors are prevented before they are reported.
- Errors that do happen say what went wrong and how to recover, in the visitor's words.
- Help is reachable from the screens where the task is hardest.
- The walkthrough followed the top tasks, not a wander through the interface.
- Consistency holds across pages: the same action looks and works the same on the next page as on this one.
- Recognition over recall holds in navigation: labels show the visitor where a page lives instead of demanding they remember.
- Help is reachable from the screens where the task is hardest.
- The build log notes the findings closed on the built page and any that remain open.
- The stage record carries the final findings, with no severity-4 problem left open.
- The judgers apply the 10 as their checklist across the build — visual, a11y, performance, content and responsive each check the heuristics in their own area. The skill does the work and the judgers check it; the skill never grades its own output.
- A simulated review is not a substitute for real users: audit 3.25 says so plainly, and this heuristic evaluation does not claim otherwise. Five real users per round remain the test that counts.

## Coordinator wiring

Loaded by stages: wireframe, build. Returns: `wireframe.pages` (heuristic findings per page: heuristic, severity 0–4, planned fix); `build.build_log` (findings closed on the built page, any remaining open). Judger boundary: all five judgers apply the 10 heuristics as their checklist in their own areas.
