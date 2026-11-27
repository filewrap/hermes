---
name: performance-design
description: "Pages that load fast and never shift as they render."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, performance]
    category: web
    related_skills: [web-design, imagery, seo-design]
    web_designer_stages: [stack, build]
---

# Performance-Aware Design Skill

This skill designs for Google's Core Web Vitals at the 75th percentile: LCP, INP, and CLS, plus TTFB. It covers the design decisions that keep pages fast — image formats and sizes, reserved space, preloading, lazy-loading, and minimal third-party scripts. It does not run the measurement itself — the performance judger measures — and it does not choose the stack's hosting or deploy target. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For every page, especially heroes, galleries and landing pages where speed is conversion.
- When the stack is being chosen, so image and script budgets are set before visual polish.
- When a page's images, embeds, or scripts are the main weight on the page.

It is NOT for:

- Measuring performance or declaring a page fast — the performance judger measures LCP, INP, and CLS.
- Server or hosting performance tuning; it covers the design decisions in the pages Hermes builds.
- Cutting content to hit a budget without the user's say-so; budgets are set in the stack stage and honoured, not silently enforced by deleting content.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage(s).
- The stack stage records the page weight budget, so image and script choices are made against a number.
- The build stage is in progress or complete, so the built pages carry the performance decisions.

## How to Run

1. Budget weight per page in the stack stage, before visual polish.
2. Choose image formats and sizes first: modern formats, right-sized and responsive images.
3. Reserve space for every image and embed with width/height or aspect-ratio, so nothing shifts as it loads.
4. Preload the LCP image; lazy-load below-fold images; never lazy-load the LCP image.
5. Defer non-critical JS and keep third-party scripts to a minimum.
6. Record the budget and the decisions in the stack record and the build log, then hand the work to the judger; do not grade it here.

## Quick Reference

- LCP <=2.5s, INP <200ms, CLS <0.1 — at the 75th percentile.
- TTFB <=0.8s is good.
- Images are usually the LCP element and the main CLS cause.
- Fixes: modern formats, right-sized/responsive images, preloading the LCP image, reserving space with width/height or aspect-ratio, lazy-loading below the fold, minimal third-party scripts.
- Never lazy-load the LCP image.

## Procedure

### 1. What it is

Designing for Google's Core Web Vitals at the 75th percentile: LCP <=2.5s, INP <200ms, CLS <0.1 (plus TTFB <=0.8s good). Images are usually the LCP element and the main CLS cause; fixes are modern formats, right-sized/responsive images, preloading the LCP image, reserving space with width/height or aspect-ratio, lazy-loading below the fold, and minimal third-party scripts.

### 2. How Hermes applies it

Hermes budgets weight per page, chooses formats/sizes before visual polish, reserves space for every image/embed, defers non-critical JS, and its judger measures LCP/INP/CLS rather than asserting 'fast'.

### 3. What it returns to the stage

- `stack` stage — `stack`: the chosen stack records the per-page weight budget. `files`: the planned files note which images are preloaded and which are lazy-loaded. `commands`: the build commands include the image optimisation steps.
- `build` stage — `files`: the built pages carry the reserved space, preload, and lazy-loading. `build_log`: notes the formats, sizes, and deferrals applied. `token_drift`: nothing new; performance adds no tokens.

### 4. Good looks like

LCP under 2.5s on mobile, CLS under 0.1, no layout jump as images load, and the page is interactive in under 200ms per interaction.

## Pitfalls

- Asserting a page is 'fast' without a measurement; only the judger's measured LCP, INP, and CLS count.
- Lazy-loading the LCP image, which delays the very element the metric watches.
- Images and embeds with no reserved space, so the layout jumps as each one loads.
- Oversized or unresponsive images served where a smaller crop would do.
- Third-party scripts loaded early, blocking the page's own interactivity.
- Choosing formats and sizes after visual polish, when the heavy choices are already locked in.

## Verification

- LCP under 2.5s on mobile, CLS under 0.1, INP under 200ms per interaction, TTFB at or under 0.8s — all at the 75th percentile.
- No layout jump as images load; every image and embed has reserved space.
- The LCP image is preloaded and never lazy-loaded; below-fold images are lazy-loaded.
- The performance judger measures LCP, INP, and CLS rather than asserting 'fast'; the skill does the work and the judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: stack, build. Returns: `stack` — `stack`, `files`, `commands`; `build` — `files`, `build_log`, `token_drift`. Judger boundary: performance judger measures LCP, INP, and CLS.
