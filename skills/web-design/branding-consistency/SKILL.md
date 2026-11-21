---
name: branding-consistency
description: "One identity applied identically on every page."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, branding]
    category: web
    related_skills: [web-design, ux-copywriting, colour-theory]
    web_designer_stages: [brief, direction]
---

# Branding Consistency Skill

One visual and verbal identity, applied everywhere, so every page feels like the same brand. This skill covers recording the brand's palette, type, logo variants and voice at the start, applying them identically on every page, and flagging any page that drifts. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- On every page of a branded site, and whenever a new page, asset or channel is added.
- At the brief stage, when the brand identity is recorded before design starts.
- At the direction stage, when the chosen direction must carry the same identity as every other page.
- When a new page is added to a finished site and must feel like the same brand from the first screen.
- When a page has drifted — a new colour, a new font, an off-brand photo style — and the drift needs flagging rather than accepting.

It is not for inventing a fresh identity per page, or for treating a logo swap as a rebrand. It is not a substitute for the design system: branding sets the identity, the system carries it.

## Prerequisites

- The run is active, the coordinator loaded this skill for its stage(s).
- The brand's palette, type, logo variants and voice are known or supplied; where any part is missing, the gap is recorded as a question rather than filled silently.
- The brief is settled, so the identity is recorded against a known audience, primary action and tone.
- The ux-copywriting skill is available for the verbal side of the identity.
- The colour-theory skill is available for the palette the identity is built on.

## How to Run

1. Record the brand at the start: palette as tokens, type as the 1–2 chosen fonts, logo variants, imagery treatment and voice.
2. Apply those records identically on every page; no page picks its own colours or fonts.
3. Use the logo family correctly: primary, simplified and icon-only variants, with clear space and minimum size respected.
4. Keep the imagery treatment and the voice the same across pages.
5. Flag any page that drifts — a new colour, a new font, an off-brand photo style — instead of letting it pass.
6. Check the result by removing the logo from a page: it should still be recognisably the same brand.
7. When a new page, asset or channel is added, check it against the recorded identity before it is delivered, not after.
8. Record every drift flag against the page it concerns, so the flag survives into the fix loop rather than being a passing remark.
9. When the brand supplies a new asset, check it against the recorded identity before it is used on a page.
10. Keep the voice notes with the palette and type records, so the verbal identity is not lost when the visual one is handed over.
11. If the direction stage proposes a colour or font outside the recorded brand, treat it as a change to the brief, not a design detail.
12. At delivery, strip the logo from one page of each template and confirm each still reads as the same brand.

## Quick Reference

- Identity parts: same hex values, same 1–2 fonts, same logo family, same imagery treatment, same voice.
- Logo family: primary / simplified / icon-only, with clear space and minimum size.
- First impressions: form in ~50ms on design alone.
- Rule: no page invents its own colours or fonts.
- Check: a page stripped of its logo is still recognisably the same brand.
- Drift: a new colour, a new font or an off-brand photo style is flagged, with the page named.
- Timing: the brand is recorded at the start, before design, not after pages exist.
- New assets: checked against the recorded identity before use.
- Voice notes: kept with the palette and type records.
- Out-of-brand proposals: treated as a change to the brief.

## Procedure

### 1. What it is

Applying one visual and verbal identity everywhere — same hex values, same 1-2 fonts, same logo family (primary/simplified/icon-only, clear space, minimum size), same imagery treatment, same voice — so every page feels like the same brand; first impressions form in ~50ms on design alone.

### 2. How Hermes applies it

Hermes records the brand's palette, type, logo variants and voice as tokens/rules at the start, applies them identically on every page, and flags any page that drifts (new colour, new font, off-brand photo style).

### 3. What it returns to the stage

The brief stage record in `gateway/web_designer/machine.py` (`STAGE_RECORDS`) requires `problem`, `audience`, `primary_action`, `success_measure`, `tone`, `non_goals` and `intent`, and the direction stage record requires `directions` and `chosen`. This skill's work fills the brief record with the brand identity recorded as tokens and rules — palette, type, logo variants, voice — alongside the tone, and fills the direction record with the chosen direction carrying that same identity. A direction that introduces a new colour or font not in the recorded brand is not recorded as chosen without the drift being flagged. Drift flags are recorded against the page they concern.

Where part of the identity is missing — a logo variant, a voice note, a palette value — the brief record notes the gap as an open question rather than filling it silently. The identity recorded in the brief is the one the direction stage is judged against, so a direction that changes the identity is a change to the brief, not a design choice made in passing.

First impressions form in ~50ms on design alone, so the identity has to be right before the first page is built — a brand applied after design is a coat of paint, not an identity. Recording the identity in the brief, carrying it through direction, and flagging drift at every stage is what keeps that first impression consistent. The brief record is therefore the reference the later stages are judged against, and any change to the identity is a change to the brief.

### 4. Good looks like

Any single page, stripped of its logo, is still recognisably the same brand; no page invents its own colours or fonts.

An identity that survives the logo being stripped away is an identity that did its job. The recognition is the test, not the logo file. Where a page needs its logo to be recognised as part of the brand, the identity has not been applied to that page. That is why the check is done per page, and why drift is flagged wherever it appears.

## Pitfalls

- A new red, a new font, or an off-brand photo style on one page, left unflagged.
- Recording the brand after design has started, so early pages carry a different identity.
- Treating a logo swap as a rebrand while the palette, type and voice drift underneath.
- Ignoring the verbal side: the same voice must hold, not just the same colours.
- Breaking logo clear space or minimum size to fit a layout, instead of fixing the layout.
- Filling a missing part of the identity silently instead of recording it as an open question.
- Checking a new page against the identity only after it is delivered, when the drift is already live.
- Using a new brand asset without checking it against the recorded identity.
- Voice notes stored apart from the visual identity, so they are lost in handover.
- Accepting an out-of-brand colour or font in the direction stage as a design detail.

## Verification

- The palette, type, logo variants and voice are recorded as tokens and rules before design starts.
- Every page applies them identically; no page invents its own colours or fonts.
- Any single page, stripped of its logo, is still recognisably the same brand.
- Drift is flagged wherever it appears, with the page named.
- Gaps in the identity are recorded as open questions, not filled silently.
- New pages, assets and channels are checked against the recorded identity before delivery.
- New assets are checked against the recorded identity before use.
- Voice notes are kept with the palette and type records.
- At delivery, a logo-stripped page from each template still reads as the same brand.
- The visual judger checks the related colour and type rules, and the content judger checks voice consistency. The skill does the work and the judgers check it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: brief, direction. Returns: brief stage-record keys `problem`, `audience`, `primary_action`, `success_measure`, `tone`, `non_goals`, `intent` and direction stage-record keys `directions`, `chosen`, with the brand identity recorded as tokens and rules. Judger boundary: visual judger checks the related colour and type rules; content judger checks voice consistency.
