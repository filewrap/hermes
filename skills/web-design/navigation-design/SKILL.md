---
name: navigation-design
description: "Menus that get visitors to their target in seconds."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, navigation]
    category: web
    related_skills: [web-design, information-architecture]
    web_designer_stages: [ia, components]
---

# Navigation Design Skill

Design the menus, breadcrumbs, search entry and footer links that carry visitors from any page to the page they need. This skill covers global, local, utility and contextual navigation plus breadcrumbs, search and footer navigation. It does not cover the sitemap itself, which belongs to information architecture, and it does not cover visual styling of the menu, which belongs to components and tokens. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For site headers, section sidebars, mobile menus, breadcrumbs and footers.
- When a site has more than a few pages and visitors need a reliable way to move between them.
- When a menu must work by touch and keyboard, not only by mouse hover.
- When a deep page needs breadcrumbs so visitors who arrive from search know where they are.
- When a footer must repeat the main routes for visitors who have scrolled to the bottom.
- Not for deciding what the pages are or how they group — that is information architecture, done before navigation.
- Not for a single link or a one-page site with no menu to design.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage or stages.
- The information architecture stage has produced a sitemap, so navigation labels and order follow the agreed structure rather than inventing one.
- The audience and their top tasks are known from intake and research, so the most-used items can be placed first.
- The component inventory is known, so the navigation component and its states can be recorded once and reused.

## How to Run

1. Take the sitemap and the ranked top tasks from the IA stage record.
   - Rank the top-level items by task frequency, most-used first.
   - Note which pages are deep enough to need breadcrumbs.
2. Choose the navigation types the site needs: global, local, utility and contextual, plus breadcrumbs on deep pages, search, and footer navigation.
   - Global navigation carries the main routes on every page.
   - Local navigation carries section pages inside a large section.
   - Utility navigation carries account, help and similar secondary routes.
   - Contextual navigation carries links inside the content itself.
3. Draft the top-level menu capped at 5–7 items, in a consistent order, with the most-used item first and the primary call to action distinct.
   - Use the users' own words for labels, not org-chart jargon.
   - Keep the order and the wording identical on every page.
4. Make every parent item clickable, make dropdowns operable by keyboard and touch, and never rely on hover alone to reveal a menu.
   - A dropdown must open and close by keyboard, with focus visible throughout.
   - A dropdown must open by tap on touch devices, with a target large enough to tap first time.
5. Add breadcrumbs on deep pages and a footer that repeats the main routes.
   - Breadcrumbs show the path from the home page to the current page.
   - The footer repeats the main routes plus utility links, for visitors who reach the bottom.
6. Test the menu at 360px width and by keyboard alone, then record the result in the stage record.
   - At 360px, every item must be reachable with no horizontal scroll and no clipped label.
   - By keyboard alone, every menu item, dropdown and breadcrumb link must be reachable and operable.
7. Re-check after any fix, and record what changed.
   - A fix to order, wording or dropdown behaviour is re-tested at 360px and by keyboard before the stage closes.
   - The final menu structure recorded is the one tested, not an earlier draft.

## Quick Reference

- Top-level navigation items: 5–7, no more.
- Order and wording: consistent on every page.
- Parents: clickable, never hover-only placeholders.
- Dropdowns: keyboard and touch operable.
- Breadcrumbs: on deep pages.
- Footer navigation: repeats the main routes.
- Search: a search entry belongs in the navigation set where the site is large enough to need it.
- Mobile test width: 360px.
- Primary call to action: distinct from the other items, placed where the top tasks put it.
- Labels: users' words, not org-chart jargon.

## Procedure

### 1. What it is

Global, local, utility and contextual navigation plus breadcrumbs, search and footer nav: 5–7 top-level items, consistent order and wording, clickable parents, no hover-only menus (NN/g 2024 guidance), mobile patterns that work by touch and keyboard.

### 2. How Hermes applies it

Hermes caps top nav at 5–7 items, puts the most-used first and the primary CTA distinct, makes dropdowns keyboard and touch operable, adds breadcrumbs on deep pages, and tests the menu at 360px.

### 3. What it returns to the stage

- `ia` stage record: `navigation` carries the menu structure — the top-level items in order, the dropdown contents, the breadcrumb rule, and the footer links — alongside `sitemap` and `flows`.
- `components` stage record: `inventory` carries the navigation component and its states (default, hover, focus, active, open, closed, mobile open) as one named component, reused on every page rather than rebuilt per page.

### 4. Good looks like

A visitor reaches the target in seconds without reading the whole menu; the same menu appears, in the same order, on every page.

## Pitfalls

- A hover-only menu: on touch devices and for keyboard users the submenu never opens, so the pages behind it are unreachable.
- More than 7 top-level items: the menu becomes a list to read rather than a set of routes to scan, and the most-used item is buried.
- Different order or wording per page: visitors relearn the menu on every page instead of relying on it.
- A parent item that is only a hover trigger: it looks like a link, behaves like nothing, and strands visitors who tap it.
- Breadcrumbs missing on deep pages: visitors who arrive from search have no sense of where they are in the site.
- A mobile menu tested only at desktop width: at 360px the items overflow, overlap, or need horizontal scrolling to reach.
- Labels in org-chart jargon: visitors do not know the internal names, so they cannot match a label to their task.
- A footer that only carries legal links: visitors who scroll to the bottom looking for a route meet a dead end.
- Search missing on a large site: with many pages and no search entry, visitors must browse the whole menu to reach a page they could have named directly.

## Verification

- The top-level menu has 5–7 items, in the same order and wording on every page.
- The most-used item is first and the primary call to action is distinct.
- Every dropdown opens by keyboard and by touch, and every parent item is clickable.
- Breadcrumbs appear on deep pages; the footer repeats the main routes.
- The menu works at 360px with no horizontal scroll and no clipped item.
- Every menu item, dropdown and breadcrumb link is reachable and operable by keyboard alone, with focus visible.
- Labels use the users' words, and the same label always leads to the same page.
- The search entry is present where the site is large enough to need it, and the footer carries the main routes plus utility links.
- Utility and contextual navigation are present where the site needs them, and local navigation appears inside large sections.
- No menu item leads to a page that does not exist, and no page in the sitemap is missing from the navigation.
- The mobile menu opens and closes by touch, and its open state is visible.
- The navigation component is one named component in the inventory, with all its states recorded, reused on every page.
- The stage record carries the final menu structure tested at 360px and by keyboard, not an earlier draft.
- The responsive judger checks the menu across viewports including 360px, and the a11y judger checks keyboard operability and visible focus. The skill does the work and the judgers check it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: ia, components. Returns: `ia.navigation` (menu structure, dropdowns, breadcrumb rule, footer links); `components.inventory` (navigation component with all states). Judger boundary: responsive judger checks the menu at 360px and across viewports; a11y judger checks keyboard and touch operability and focus visibility.
