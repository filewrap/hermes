---
name: onboarding-design
description: "First runs that teach one real win, fast."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, onboarding]
    category: web
    related_skills: [web-design, empty-states, ux-copywriting]
    web_designer_stages: [wireframe, copy]
---

# Onboarding and First-Run Design Skill

This skill designs the first-run experience as the empty state's highest-leverage form: teach the first real win, show one clear next step, use progressive disclosure rather than a tour of everything, and let the empty state be the onboarding. It covers what a new user sees and does first. It does not cover the empty state's per-origin variants in detail — the empty-states skill owns those — and it does not cover long-form copy — the copy stage owns that. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For new accounts, first visits, and any product with a cold start.
- When a first-run screen currently tours every feature before the user has done anything.
- When a new user cannot tell what to do first within minutes of arriving.

It is NOT for:

- The empty state's four origins and their variants — that is empty-states.
- Feature documentation or help content — that is the copy stage.
- Returning users; it governs the first run only.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage(s).
- The wireframe records the first-run screen, so the single achievable action can be placed on it.
- The brief records the primary action, so the first win is the real win the product promises.

## How to Run

1. Name the first real win: the one achievable action a new user should complete first (time-to-first-win).
2. Design the first-run screen around that single action, with one clear next step.
3. Defer advanced features: use progressive disclosure rather than a tour of everything.
4. Let the empty state be the onboarding — the first-run screen is the empty state, pointing at the first win.
5. Check whether a new user reaches that first win without help; if not, simplify the step, not the explanation.
6. Record the first win and the single next step in the wireframe pages and the copy record, then hand the work to the judger; do not grade it here.

## Quick Reference

- First-run experience: the empty state's highest-leverage form.
- Time-to-first-win: teach the first real win, fast.
- One clear next step, one achievable action.
- Progressive disclosure, not a tour of everything.
- The empty state is the onboarding.
- A new user does it in minutes, without help.

## Procedure

### 1. What it is

The first-run experience as the empty state's highest-leverage form: teach the first real win (time-to-first-win), show one clear next step, use progressive disclosure rather than a tour of everything, and let the empty state be the onboarding.

### 2. How Hermes applies it

Hermes designs the first-run screen around a single achievable action, defers advanced features, and measures whether a new user reaches that first win without help.

### 3. What it returns to the stage

- `wireframe` stage — `pages`: each wireframe page records the first-run screen, its single achievable action, and the one clear next step.
- `copy` stage — `pages`: the first-run copy records the single next step and the wording that points at the first win. `meta`: nothing new; onboarding does not change page metadata.

### 4. Good looks like

A new user knows exactly what to do first and does it in minutes; no multi-screen tour is needed to start.

## Pitfalls

- A multi-screen tour of everything before the user has done anything — the tour replaces the win instead of leading to it.
- No single next step: several competing actions on the first screen, so the user cannot tell what to do first.
- Advanced features shown up front, burying the one action that matters.
- A first-run screen that apologises for being empty instead of pointing at the first win.
- Measuring whether the user read the tour, instead of whether they reached the first win without help.

## Verification

- The first-run screen has one achievable action and one clear next step.
- A new user reaches the first win in minutes, without help and without a multi-screen tour.
- Advanced features are deferred, not shown up front.
- The content judger checks the first-run copy and the single next step; the skill does the work and the judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: wireframe, copy. Returns: `wireframe` — `pages`; `copy` — `pages`, `meta`. Judger boundary: content judger checks first-run copy and the single next step.
