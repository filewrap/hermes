---
name: information-architecture
description: "Content organised, labelled and linked for finding."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, ia]
    category: web
    related_skills: [web-design, navigation-design, user-journeys]
    web_designer_stages: [ia]
---

# Information Architecture Skill

Organising, labelling and connecting content — organisation systems, labelling, navigation and search — so visitors and search engines can find things. This skill inventories the content, groups it by user task, and drafts the sitemap before any visual design begins. It does not design the navigation itself — that is the navigation-design skill — and it does not draw the wireframe.

This skill is load-on-demand only. The web-designer coordinator loads it for its stages and it never auto-loads. It never contacts the user directly — the coordinator is the only router.

## When to Use

- Before any visual design, when a site has more than a few pages or competing audiences.
- When the IA stage needs a sitemap, navigation model and flows recorded.
- When content is buried, categories mix levels of specificity, or labels use org-chart jargon instead of users' words.
- When a site has grown page by page and no longer has an examined structure.
- When two audiences compete for the same navigation and each needs its own path.

It is NOT for visual design, navigation styling, or judging the finished structure — the content judger checks whether the structure reads clearly.

## Prerequisites

- The run is active and the coordinator loaded this skill for the IA stage.
- The brief is frozen, so the structure serves the site's audience and primary action.
- The research stage is complete, so the content inventory is grounded in real content and real tasks.
- The content inventory is known, so the sitemap is drawn from real content rather than assumed pages.

## How to Run

1. Inventory the content: list everything the site must carry.
2. Group it by user task, using card sorting and tree testing in principle, not by org chart.
3. Draft a sitemap with shallow depth: key pages within 3 clicks of the home page.
4. Label categories in users' words, at the same level of specificity.
5. Connect the content: navigation and search paths for every group, so nothing is reachable only by guessing.
6. Check the depth: no important page buried 4 levels deep, and no category mixing levels of specificity.
7. Check the labels against users' words: any label that reads as org-chart jargon goes back for relabelling.
8. Record the sitemap, navigation model and flows in the IA stage record — and only then design navigation.
9. Check that every group has both a navigation path and a search path.
10. Confirm the flows cover the key user tasks: each top task must have a path through the structure.
11. Re-check the depth after any page is added: a new page must not push a key page to a fourth level.

## Quick Reference

- Rosenfeld & Morville's four blocks: organisation systems, labelling, navigation, search.
- Key pages within 3 clicks; no important page buried 4 levels deep.
- Categories at the same level of specificity.
- Labels use users' words, not org-chart jargon.
- Group by user task: card sorting and tree testing in principle.
- Inventory first, group second, sitemap third, navigation last.
- Navigation and search paths for every group.
- Every top user task has a path through the structure.
- A new page must not push a key page to a fourth level.
- Labels are checked in users' words on every page, not only on the home page.
- The sitemap is drawn before visual design, not after it.

## Procedure

### 1. What it is

Organising, labelling and connecting content — Rosenfeld & Morville's four blocks: organisation systems, labelling, navigation and search — so visitors and search engines can find things.

### 2. How Hermes applies it

Hermes inventories content, groups it by user task (card sorting / tree testing in principle), drafts a sitemap with shallow depth (key pages within 3 clicks), and only then designs navigation.

### 3. What it returns to the stage

- IA stage: `sitemap` — the drafted sitemap with shallow depth; `navigation` — the navigation model connecting the groups; `flows` — the user-task flows the structure serves.

The sitemap is the value the IA stage records. The sitemap carries the shallow depth; the navigation model carries the connections between groups; the flows carry the tasks the structure serves. If a record key is missing, the stage does not complete.

### 4. Good looks like

No important page is buried 4 levels deep; categories are at the same level of specificity; labels use users' words, not org-chart jargon.

## Pitfalls

- An important page buried 4 levels deep, reachable only by guessing.
- Categories mixing levels of specificity, so siblings are not comparable.
- Labels in org-chart jargon that users do not recognise.
- Content grouped by internal department instead of by user task.
- Navigation designed before the sitemap exists, so the menu encodes an unexamined structure.
- A sitemap recorded but never used, so the wireframe invents its own structure.
- An inventory that lists pages but never groups them, so the sitemap is a flat list rather than a structure.
- Search left out of the structure, so content is reachable only through the navigation paths.
- Competing audiences given one shared structure, so neither audience finds its content.
- A sitemap drawn after the visual design, when the structure can no longer change anything.
- Flows recorded for the happy path only, with no path for the error or exit.
- A category relabelled in users' words on one page and left as jargon on another.

## Verification

- No important page is more than 3 clicks from the home page.
- Categories sit at the same level of specificity.
- Labels use users' words, not org-chart jargon.
- The sitemap, navigation model and flows are recorded in the IA stage record.
- Every group has a navigation and search path.
- The content is grouped by user task, not by org chart.
- Every top user task has a path through the structure.
- A new page does not push a key page to a fourth level.

The skill does the work and the content judger checks the structure. The skill never grades its own output.

## Coordinator wiring

Loaded by stages: ia. Returns: `sitemap`, `navigation`, `flows` (ia stage). Judger boundary: the content judger checks structure and findability.
