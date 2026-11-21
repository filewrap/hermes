---
name: accessibility-wcag
description: "Pages that pass AA and work by keyboard and reader."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, accessibility]
    category: web
    related_skills: [web-design, keyboard-accessibility, colour-contrast]
    web_designer_stages: [tokens, build]
---

# Accessibility: WCAG 2.2 POUR Skill

This skill applies W3C WCAG 2.2 as the baseline for every site, organised as POUR — Perceivable, Operable, Understandable, Robust — at level AA. It covers the standard itself: contrast, targets, alt text, heading order, and error identification, from the first token onward. It does not cover keyboard and screen-reader technique in detail — the keyboard-accessibility skill owns that — and it does not write the site's content. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- As the baseline for every site, not a bolt-on audit at the end.
- When tokens are being defined, so contrast and target sizes are decided before any component is built.
- When a page, component, or theme is about to be judged for delivery.

It is NOT for:

- Keyboard, focus, and screen-reader technique — that is keyboard-accessibility.
- Colour palette choice itself — that is the colour stage; this skill sets the contrast floor the palette must meet.
- Legal advice on accessibility obligations; it applies the standard, it does not interpret the law.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage(s).
- The tokens stage is in progress or complete, so contrast and target sizes can be set as tokens rather than patched per component.
- Automated check tooling is available to the judger (axe, Lighthouse, WAVE); this skill does the design work, the judger runs the checks.

## How to Run

1. Design to AA from the first token: every text/background pair, every UI component, every target size.
2. Apply the POUR structure: Perceivable (contrast, alt text), Operable (targets, keyboard), Understandable (heading order, error identification), Robust (valid, parseable markup).
3. Check contrast as tokens are defined: 4.5:1 for normal text, 3:1 for large text and UI components/icons.
4. Give every interactive target at least 24x24px, the WCAG 2.2 minimum.
5. Write alt text for every meaningful image and keep heading order logical with one h1 per page.
6. Run automated checks (axe/Lighthouse/WAVE) plus manual keyboard and screen-reader checks through the judger; treat AA failures as blocking, not cosmetic.
7. Record the contrast decisions in the tokens record and the build record, then hand the work to the judger; do not grade it here.

## Quick Reference

- Standard: W3C WCAG 2.2, current Recommendation (5 Oct 2023).
- Structure: POUR — Perceivable, Operable, Understandable, Robust; 13 guidelines; levels A/AA/AAA.
- Target: AA is the usual target.
- Contrast: 4.5:1 normal text, 3:1 large text and UI components/icons.
- Minimum targets: 24x24px (WCAG 2.2).
- Alt text on meaningful images; heading order logical; errors identified.
- Automated checks: axe, Lighthouse, WAVE — plus manual keyboard and screen-reader checks.

## Procedure

### 1. What it is

W3C WCAG 2.2, current Recommendation (5 Oct 2023), organised as POUR — Perceivable, Operable, Understandable, Robust — 13 guidelines, levels A/AA/AAA; AA is the usual target (4.5:1 text, 3:1 large/UI, 24x24px minimum targets in 2.2, alt text, heading order, error identification).

### 2. How Hermes applies it

Hermes designs to AA from the first token, runs automated checks (axe/Lighthouse/WAVE) plus manual keyboard and screen-reader checks, and its judger treats AA failures as blocking, not cosmetic.

### 3. What it returns to the stage

- `tokens` stage — `tokens_path`: the token file records the contrast-verified colour pairs and the 24x24px minimum target size as tokens.
- `build` stage — `files`: the built pages carry the AA decisions. `build_log`: notes the automated and manual checks run. `token_drift`: any raw value that bypasses the tokens is recorded here.

### 4. Good looks like

A page passes AA, works with keyboard and screen reader, and automated tools plus manual testing both agree.

## Pitfalls

- Treating accessibility as a bolt-on audit at the end, when contrast and target sizes are already baked into components.
- Checking contrast in light mode only and shipping a dark theme that fails AA.
- Colour as the only way to convey meaning — status shown by colour alone fails for users with colour-vision deficiency.
- Alt text missing, or alt text that names the file instead of describing the image.
- Skipped heading levels or more than one h1, so the page outline is broken for assistive technology.
- Automated tools passing while a keyboard or screen-reader user still cannot complete the task — automated checks alone are not enough.

## Verification

- The page passes AA: 4.5:1 normal text, 3:1 large text and UI components, 24x24px minimum targets.
- Alt text is present on meaningful images; heading order is logical with one h1 per page.
- Automated tools (axe/Lighthouse/WAVE) and manual keyboard and screen-reader checks both agree the page works.
- The a11y judger blocks delivery on AA failures; the skill does the work and the judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: tokens, build. Returns: `tokens` — `tokens_path`; `build` — `files`, `build_log`, `token_drift`. Judger boundary: a11y judger checks WCAG 2.2 AA and blocks delivery on failures.
