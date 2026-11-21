---
name: content-hierarchy
description: "Copy structured so scanners get the message fast."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, content]
    category: web
    related_skills: [web-design, visual-hierarchy, ux-copywriting]
    web_designer_stages: [copy, wireframe]
---

# Content Hierarchy and Scannability Skill

Writing and structuring for scanners: front-load the key words, use the inverted pyramid, keep one idea per paragraph, and use lists and bold key terms — so a 30-second scanner and a 5-minute reader both extract value. This skill structures the copy. It does not set the visual reading order — that is the visual-hierarchy skill — and it does not set body-copy measures — that is the readability skill.

This skill is load-on-demand only. The web-designer coordinator loads it for its stages and it never auto-loads. It never contacts the user directly — the coordinator is the only router.

## When to Use

- For all page copy, articles, landing pages and long product pages.
- When the copy stage needs copy structured for scanners, or the wireframe stage needs the content order set.
- When a page reads as a wall of text that no scanner can get through.
- When headings do not form a readable outline on their own.
- When the copy was written for the full reader only and scanners get nothing from it.

It is NOT for visual emphasis levels, body size and leading, or judging the finished copy — the content judger checks whether a scanner can get the message.

## Prerequisites

- The run is active and the coordinator loaded this skill for the copy or wireframe stage.
- The information architecture is complete, so the copy follows the agreed content order.
- The brief is frozen, so the copy serves the site's audience and primary action.
- The page copy is known, so the structure is set on real text rather than placeholder copy.

## How to Run

1. Front-load headings and first sentences: the key words come first, not last.
2. Use the inverted pyramid: conclusion first, detail after.
3. Keep paragraphs to 1–3 sentences, one idea per paragraph.
4. Use descriptive (not clever) headings, and lists where items are parallel.
5. Bold key terms so a scanner can get the message from headings and bold alone.
6. Check the outline: headings alone must form a readable outline of the page.
7. Check both readers: a 30-second scanner and a 5-minute reader must both extract value from the same copy.
8. Record the structured pages and their metadata in the copy stage record, and the content order in the wireframe stage record.
9. Check the first sentences: each one must front-load its key words, not set the scene.
10. Confirm the bold terms are the key terms: a scanner reading bold alone must get the message.
11. Check long product pages the same way as articles: the structure must hold wherever the copy is long.

## Quick Reference

- Users read only about 20–28% of words.
- Scan patterns: F-pattern on unformatted text; layer-cake pattern when headings are distinct.
- Paragraphs: 1–3 sentences, one idea each.
- Headings: descriptive, not clever.
- A scanner must get the message from headings and bold alone.
- Front-load key words; inverted pyramid: conclusion first.
- Headings alone form a readable outline.
- Bold the key terms: a scanner reading bold alone must get the message.
- The structure holds on long product pages as well as articles.
- First sentences front-load their key words; they do not set the scene.
- The conclusion comes first; detail follows.

## Procedure

### 1. What it is

Writing and structuring for scanners: NN/g eye-tracking shows users read only ~20-28% of words, scan in F-pattern on unformatted text and in a layer-cake pattern when headings are distinct; front-load key words, use inverted pyramid, one idea per paragraph, lists and bold key terms.

### 2. How Hermes applies it

Hermes front-loads headings and first sentences, keeps paragraphs to 1-3 sentences, uses descriptive (not clever) headings and lists, and its judger checks a scanner can get the message from headings/bold alone.

### 3. What it returns to the stage

- Copy stage: `pages` — the page copy structured for scanners; `meta` — the page metadata carrying the headings and structure.
- Wireframe stage: `pages` — the wireframe pages with the content order and heading structure set.

The structured copy is the value both stages record. The copy record carries the structured pages and their metadata; the wireframe record carries the content order. If a record key is missing, the stage does not complete.

### 4. Good looks like

A 30-second scanner and a 5-minute reader both extract value; headings alone form a readable outline.

## Pitfalls

- Clever headings that hide their meaning, so the outline reads as riddles.
- Key words buried at the end of sentences and paragraphs, where scanners never reach them.
- Paragraphs longer than 3 sentences carrying several ideas at once.
- A wall of unformatted text with no headings, lists or bold, which forces the F-pattern and loses the layer-cake.
- Bold used on everything, so nothing stands out to a scanner.
- Copy written for the full reader only, with nothing for the scanner who reads 20–28% of the words.
- Headings that are distinct in wording but identical in look, so the layer-cake pattern never forms.
- Lists used for prose that should be paragraphs, so the structure lies about the content.
- The conclusion buried after the detail, so a scanner leaves before reaching the point.
- First sentences that set the scene instead of front-loading the key words.
- Headings written for the full reader, so the outline they form says nothing on its own.
- Bold terms that are not the key terms, so a scanner reading bold alone gets the wrong message.

## Verification

- A scanner gets the message from headings and bold alone.
- Headings alone form a readable outline.
- Paragraphs are 1–3 sentences, one idea each.
- A 30-second scanner and a 5-minute reader both extract value.
- Key words sit at the front of headings and first sentences.
- The structured pages, metadata and content order are recorded in their stage records.
- The bold terms are the key terms, not decoration.
- The structure holds on long product pages as well as articles.

The skill does the work and the content judger checks it. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: copy, wireframe. Returns: `pages`, `meta` (copy stage); `pages` (wireframe stage). Judger boundary: the content judger checks scannability and heading structure.
