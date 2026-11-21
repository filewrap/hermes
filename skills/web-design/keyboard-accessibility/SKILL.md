---
name: keyboard-accessibility
description: "Native semantics, visible focus, no keyboard traps."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, accessibility]
    category: web
    related_skills: [web-design, accessibility-wcag, component-states]
    web_designer_stages: [components, build]
---

# Keyboard, Focus and Screen-Reader Accessibility Skill

This skill makes every interactive element work by keyboard, focus, and screen reader: semantic HTML first, visible focus, logical DOM and tab order, no keyboard traps, and ARIA only when native HTML cannot express it. It covers the technique of operable interfaces. It does not cover contrast, targets, or the WCAG standard as a whole — the accessibility-wcag skill owns that — and it does not cover form error wording — the form-validation skill owns that. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For every interactive element, dialog, menu and single-page navigation change.
- When a component is built from a non-native element (a div acting as a button, a custom menu, a modal).
- When a page changes without a reload and the user needs to be told what changed.

It is NOT for:

- Contrast ratios or target sizes — that is accessibility-wcag.
- Error message wording — that is form-validation.
- Deciding the site's navigation structure — that is the IA stage.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage(s).
- The components inventory lists the interactive components, so each one can be checked for native semantics and focus behaviour.
- The build stage is in progress or complete, so keyboard behaviour can be checked in the built page.

## How to Run

1. Use native elements first: real `<button>`, `<a>`, `<nav>`, `<main>`, landmarks, one h1, logical headings. Native elements give keyboard, focus and screen-reader semantics free.
2. Keep focus visible with `:focus-visible`. Never use `outline: none` without a replacement.
3. Keep DOM order and tab order logical; use no positive tabindex.
4. Check there is no keyboard trap: focus can always leave every component, including dialogs and menus.
5. Move focus into dialogs and back to the trigger on close; announce route changes on single-page navigation.
6. Use ARIA only when native HTML cannot express the meaning (first rule of ARIA, W3C).
7. Test by Tabbing with the mouse aside: Tab, Shift+Tab, Enter, Space, Escape through the whole main task.
8. Record the semantics and focus behaviour per component in the inventory and the build log, then hand the work to the judger; do not grade it here.

## Quick Reference

- Native first: real `<button>`, `<a>`, `<nav>`, `<main>`, landmarks, one h1, logical headings.
- Focus: visible `:focus-visible`; never `outline: none` without a replacement.
- Tab order: logical DOM order, no positive tabindex.
- No keyboard traps.
- Dialogs: focus moves in on open, back to the trigger on close.
- Route changes announced.
- ARIA only when native HTML cannot express it (first rule of ARIA, W3C).
- Keys: Tab, Shift+Tab, Enter, Space, Escape.

## Procedure

### 1. What it is

Semantic HTML first (real <button>, <a>, <nav>, <main>, landmarks, one h1, logical headings) — native elements give keyboard, focus and screen-reader semantics free; visible :focus-visible (never outline:none without replacement), logical DOM/tab order, no positive tabindex, no keyboard traps, ARIA only when native HTML cannot express it (first rule of ARIA, W3C).

### 2. How Hermes applies it

Hermes uses native elements, tests by Tabbing with mouse aside, moves focus into dialogs and back on close, announces route changes, and its judger checks focus is always visible and never trapped.

### 3. What it returns to the stage

- `components` stage — `inventory`: each interactive component records its native element (or the ARIA it needs when native HTML cannot express it), its focus behaviour, and its dialog/menu focus management.
- `build` stage — `files`: the built pages carry the semantics and focus behaviour. `build_log`: notes the Tab-through test and any focus management added. `token_drift`: nothing new; semantics add no tokens.

### 4. Good looks like

The whole main task works with Tab/Shift+Tab/Enter/Space/Escape; focus is always visible; a screen reader can list buttons, headings and landmarks.

## Pitfalls

- `outline: none` with no replacement, so keyboard users cannot see where focus is.
- A div or span acting as a button, with no keyboard or screen-reader semantics.
- Positive tabindex, which scrambles the tab order the DOM already gives for free.
- Keyboard traps: focus enters a dialog or menu and cannot leave.
- Focus left behind when a dialog closes, or never moved into it when it opens.
- Route changes that happen silently, so a screen-reader user does not know the page changed.
- ARIA used where a native element would have done the job — ARIA is the last resort, not the first.

## Verification

- The whole main task completes with Tab, Shift+Tab, Enter, Space, and Escape, mouse aside.
- Focus is always visible and never trapped; dialogs return focus to their trigger on close.
- A screen reader can list the page's buttons, headings, and landmarks.
- The a11y judger blocks delivery on AA failures, including keyboard and focus failures; the skill does the work and the judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: components, build. Returns: `components` — `inventory`; `build` — `files`, `build_log`, `token_drift`. Judger boundary: a11y judger checks keyboard, focus, and screen-reader behaviour and blocks delivery on failures.
