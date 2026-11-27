---
name: typographic-scale
description: "One ratio gives every heading a distinct size."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, typography]
    category: web
    related_skills: [web-design, font-pairing, readability]
    web_designer_stages: [font, tokens]
---

# Typographic Scale and Hierarchy Skill

A modular type scale gives every heading and body level a distinct, harmonically related size, weight and spacing, so a reader can tell the reading order at a glance. This skill defines that scale as tokens and enforces it in the generated HTML. It does not choose the typefaces themselves — that is the font-pairing skill — and it does not write the page copy.

This skill is load-on-demand only. The web-designer coordinator loads it for its stages and it never auto-loads. It never contacts the user directly — the coordinator is the only router.

## When to Use

- On every page, before choosing decorative fonts — to establish reading order for hero, sections, cards and body.
- When heading levels look interchangeable, or when a page has no clear reading order.
- When the font stage needs a scale to record, or the tokens stage needs type tokens defined.
- When a new heading level is added and needs a place in the scale rather than a one-off size.
- When the build's headings were sized by eye and the levels no longer read as distinct.

It is NOT for choosing which typefaces to pair, setting body-copy line length, or judging whether the finished page passes — the visual judger does that check.

## Prerequisites

- The run is active and the coordinator loaded this skill for the font or tokens stage.
- The font decision is made, or is being made in the same stage: the scale needs the chosen families before it can be recorded.
- The direction stage is complete, so the scale matches the chosen design direction.
- The brief is frozen, so the scale serves the site's reading order rather than taste alone.

## How to Run

1. Take the base size of 16px and the chosen ratio, 1.25 Major Third or 1.333 Perfect Fourth.
2. Build the scale upward from the base for h1, h2, h3 and any further heading levels, and downward for caption and small text.
3. Assign each level a distinct size, weight and spacing, so no two adjacent levels look interchangeable.
4. Use clamp for the h1 size, so the largest heading scales fluidly with the viewport instead of jumping between fixed sizes.
5. Check each adjacent pair: the size difference must be at least ~1.25x, or the weight or spacing must carry the difference where size alone does not.
6. Record the scale as tokens (for example h1 clamp, h2, h3, body, caption) in the font stage record.
7. In the tokens stage, encode the scale as named type tokens, never as raw pixel values in markup.
8. In generated HTML, enforce one h1 per page and ordered h2/h3 with no skipped levels.
9. Re-check the scale after the pairing changes: a new body face can shift how large a level reads, so the differences must hold on the chosen faces.
10. Confirm the reading order on a real page: hero, sections, cards and body must each show their level clearly.
11. Apply the scale to captions and small text as well as headings and body, so no level falls back to a default.
12. Confirm in the build that no heading size is set outside the tokens.
13. Check the hero separately: the largest heading must still read as an h1, not as decoration.
14. Record the ratio and the base size alongside the levels, so a later change keeps the harmonic relationship.

## Quick Reference

- Base size: 16px.
- Ratios: 1.25 (Major Third) or 1.333 (Perfect Fourth).
- One h1 per page; no skipped heading levels.
- Adjacent heading sizes differ by at least ~1.25x.
- Scale tokens: h1 clamp, h2, h3, body, caption.
- Each level differs in size, weight or spacing — never in none of the three.
- The scale applies to hero, sections, cards and body alike.
- The h1 uses a clamp; lower levels step down by the ratio.
- The ratio and base size are recorded with the levels, not assumed.
- Captions and small text sit in the scale, not outside it.
- The scale is checked on a real page: hero, sections, cards and body.

## Procedure

### 1. What it is

A modular type scale (common ratios 1.25 Major Third or 1.333 Perfect Fourth from a 16px base) that gives every heading and body level a distinct, harmonically related size, weight and spacing, with one h1 per page and no skipped heading levels.

### 2. How Hermes applies it

Hermes defines the scale as tokens (e.g. h1 clamp, h2, h3, body, caption), enforces one h1 and ordered h2/h3 in generated HTML, and its judger checks heading sizes differ by at least ~1.25x and levels are not skipped.

### 3. What it returns to the stage

- Font stage: `families` — the chosen typefaces the scale is built on; `scale` — the full modular scale with ratio, base size and every level's size, weight and spacing.
- Tokens stage: `tokens_path` — the path to the token file where the scale is encoded as named type tokens.

The scale is the value the font stage records and the tokens stage encodes. The font record carries the ratio and the level values; the tokens record carries the path where those values live as named tokens. If either record key is missing, the stage does not complete.

### 4. Good looks like

A reader can tell h1 > h2 > h3 > body at a glance; each level differs in size, weight or spacing; no two adjacent levels look interchangeable.

## Pitfalls

- Skipped heading levels: an h3 directly under an h1 breaks the outline and confuses screen readers.
- Two adjacent levels that differ only slightly, so they read as the same level.
- More than one h1 on a page, which splits the page's main heading.
- A scale recorded in the font stage but never encoded as tokens, so the build falls back to hardcoded sizes.
- Choosing a ratio per heading by eye instead of one ratio for the whole scale, which breaks the harmonic relationship.
- Decorative fonts chosen before the scale exists, so the hierarchy is built on looks rather than order.
- An h1 without a clamp, so the largest heading jumps between fixed sizes instead of scaling fluidly.
- Caption and small text left out of the scale, so they fall back to browser defaults.
- The scale applied to headings while body copy is left at the browser default, so body has no place in the hierarchy.
- A scale that holds on desktop but collapses on phone, where the h1 and h2 end up looking the same size.
- Levels that differ in size but share the same weight and spacing, so the difference is easy to miss.
- A ratio recorded nowhere, so the next change rebuilds the scale from scratch instead of stepping from the base.

## Verification

- Every level of the scale differs from its neighbours by at least ~1.25x in size, weight or spacing.
- The generated HTML has exactly one h1 and no skipped heading levels.
- The scale appears in the font stage record and as named tokens at the recorded tokens path.
- A reader can tell h1 > h2 > h3 > body at a glance.
- The h1 uses a clamp so it scales fluidly with the viewport.
- Body and caption levels are defined in the scale, not left to defaults.
- The hierarchy holds at phone width as well as desktop width.
- The ratio and base size are recorded alongside the levels.
- No heading size is set outside the tokens in the build.
- The hero's largest heading still reads as an h1.

The skill does the work and the visual judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: font, tokens. Returns: `families`, `scale` (font stage); `tokens_path` (tokens stage). Judger boundary: the visual judger checks heading size differences and heading order.
