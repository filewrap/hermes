---
name: visual-hierarchy
description: "Reading order set by size, weight and space."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, hierarchy]
    category: web
    related_skills: [web-design, content-hierarchy, spacing-system]
    web_designer_stages: [wireframe, direction]
---

# Visual Hierarchy and Gestalt Skill

Guiding the eye by size, weight, colour, contrast, position and whitespace, using Gestalt grouping — so the most important element is seen first. This skill sets the reading order of a screen before any visual styling is applied. It does not write the copy — that is the content-hierarchy skill — and it does not set the type scale — that is the typographic-scale skill.

This skill is load-on-demand only. The web-designer coordinator loads it for its stages and it never auto-loads. It never contacts the user directly — the coordinator is the only router.

## When to Use

- On every screen: hero, pricing, dashboard, article — wherever competing elements need a reading order.
- When the wireframe stage needs priorities set, or the direction stage needs a dominant element named.
- When a screen has no clear first thing to look at.
- When competing elements fight for attention and the eye has nowhere to land first.
- When a screen's emphasis levels have multiplied past three and the order is unreadable.

It is NOT for structuring the copy itself, and it is NOT for judging the finished screen — the visual judger runs the squint test on it.

## Prerequisites

- The run is active and the coordinator loaded this skill for the wireframe or direction stage.
- The information architecture is complete, so the screen's content and order are known.
- The brief is frozen, so the dominant element serves the site's primary action.
- The screen list is known, so the hierarchy is set on every screen, not only the home page.

## How to Run

1. Name one dominant element per viewport: the primary action or value.
2. Give it maximum weight and surrounding space, so the eye lands on it first.
3. Set three clear emphasis levels only: primary, supporting, and the rest.
4. Group by proximity before adding boxes: related items sit closer than unrelated ones, following Gestalt proximity, similarity, common region, continuity and closure.
5. Use size, weight, colour, contrast, position and whitespace together to set the order — never one signal alone.
6. Check the order with the squint test: when the text is illegible, the hierarchy must still be visible.
7. Check the order on every screen — hero, pricing, dashboard, article — not only on the home page.
8. Record the pages in the wireframe stage record and the chosen direction in the direction stage record, with the dominant element named in each.
9. Check the order at phone width: the dominant element must still be first when the screen narrows.
10. Confirm the supporting content follows in order after the dominant element, with no second element competing for first place.
11. Re-check after any direction change: a new direction must keep the same dominant element and emphasis levels.

## Quick Reference

- One dominant element per viewport.
- Three clear emphasis levels only.
- Gestalt grouping: proximity (most important), similarity, common region, continuity, closure.
- Order signals: size, weight, colour, contrast, position, whitespace.
- Group by proximity before adding boxes.
- Squint test: hierarchy visible when text is illegible.
- The eye lands on the primary action or value first, then supporting content.
- One dominant element per viewport; the same one on phone and desktop.
- Supporting content follows in order, with no second element competing for first place.
- The dominant element is still first at phone width.
- The dominant element is the primary action or value, not decoration.

## Procedure

### 1. What it is

Guiding the eye by size, weight, colour, contrast, position and whitespace, using Gestalt grouping — proximity (most important), similarity, common region, continuity, closure — so the most important element is seen first.

### 2. How Hermes applies it

Hermes names one dominant element per viewport, gives it maximum weight and surrounding space, groups by proximity before adding boxes, and its judger runs a squint test (hierarchy visible when text is illegible).

### 3. What it returns to the stage

- Wireframe stage: `pages` — the wireframe pages with the dominant element and the three emphasis levels set on each.
- Direction stage: `directions` — the directions explored, each carrying its hierarchy approach; `chosen` — the direction picked, with its dominant element named.

The reading order is the value both stages record. The wireframe record carries the order per page; the direction record carries the picked direction and its dominant element. If a record key is missing, the stage does not complete.

### 4. Good looks like

Three clear emphasis levels only; the eye lands on the primary action/value first, then supporting content, in that order.

## Pitfalls

- No dominant element, so everything competes and nothing is seen first.
- More than three emphasis levels, so the order is unreadable.
- Boxes added before grouping by proximity, so the structure rests on borders instead of space.
- A dominant element that is not the primary action or value, so the eye is led away from what matters.
- Hierarchy set by one signal alone — size without weight or space — which is easy to miss.
- The same screen with a different dominant element on mobile and desktop, so the reading order changes by viewport.
- Two elements given maximum weight, so the eye has no single place to land first.
- Proximity ignored in favour of similarity, so items that look alike are read as a group even when they are unrelated.
- The dominant element named in the wireframe but never given its weight and space in the direction.
- A screen where the hierarchy is only visible when the text can be read, so the squint test fails.
- Emphasis levels set per screen by eye, so the same level reads differently on different screens.
- A direction change that moves the dominant element without recording the change.

## Verification

- Three emphasis levels only, on every screen.
- The eye lands on the primary action or value first, then supporting content, in that order.
- Hierarchy is still visible when the text is illegible (the squint test).
- The pages and the chosen direction are recorded in their stage records.
- One dominant element per viewport, named in the record.
- The dominant element is the primary action or value, not a decorative element.
- The dominant element is still first at phone width.
- Supporting content follows in order, with no second element competing for first place.

The skill does the work and the visual judger checks it with the squint test. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: wireframe, direction. Returns: `pages` (wireframe stage); `directions`, `chosen` (direction stage). Judger boundary: the visual judger checks hierarchy with the squint test.
