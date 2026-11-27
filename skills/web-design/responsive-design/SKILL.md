---
name: responsive-design
description: "One layout that works from phone to wide desktop."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, responsive]
    category: web
    related_skills: [web-design, css-grid, touch-targets]
    web_designer_stages: [build]
---

# Responsive and Mobile-First Design Skill

Design base styles for the smallest screen and enhance upward, so one layout works from phone to wide desktop. This skill covers fluid grids, flexible images, fluid type with clamp(), container queries, and content-defined breakpoints. It does not cover the choice of breakpoints for their own sake, and it does not cover touch target sizing, which belongs to the touch-targets skill. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For every site, from the first layout decision — not as a desktop shrink afterwards.
- When a layout must hold from the smallest phone to a wide desktop without a separate mobile version.
- When type, spacing or images must scale smoothly rather than jump between fixed sizes.
- When a component must adapt to its container rather than to the viewport.
- When the mobile version must carry the same content and priority as the desktop version.
- Not for a desktop-only internal tool where the audience and the viewport are fixed and known.
- Not as a fix applied after the build: mobile-first is a starting point, not a patch.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage or stages.
- Tokens, components and the stack plan are in place, so the build uses token values rather than hardcoded widths.
- The wireframe exists for mobile and desktop, so the build knows what the single-column and multi-column forms should be.
- The breakpoints are not yet fixed: they are decided by where the layout breaks, during the build.

## How to Run

1. Write base styles for the smallest screen first, single-column, with the same content and priority as the desktop form.
   - Start at the smallest width and confirm the page reads top to bottom in task order.
   - Do not hide content to make the small screen fit; reorder it instead.
2. Use fluid grids and flexible images, and clamp() for type and spacing so values scale smoothly.
   - Grids use flexible units so column counts follow the available width.
   - Images scale with their container and never force their container wider.
3. Add breakpoints only where the layout breaks — content-defined breakpoints, not device names.
   - When a line gets too long, a card gets too narrow, or a row crowds, add a breakpoint there.
   - Record each breakpoint and the break that caused it in the build log.
4. Use container queries where a component must adapt to its container rather than the viewport.
   - A card that appears in a sidebar and in a full-width row adapts to each container.
5. Cap max-width so lines do not stretch on very wide screens.
   - The cap keeps the measure readable when the viewport keeps growing.
6. Check the build at 320px, 768px and 1280px for pinch-zoom, horizontal scroll, and content priority.
   - At each width, confirm the same content appears in the same priority order.
   - Confirm no element forces horizontal scroll at any width between the checked widths.
7. Re-check after any fix, and record what changed.
   - A fix to a breakpoint, a cap or a fluid value is re-checked at all three widths before the stage closes.
   - The breakpoints recorded are the ones in the final build, not earlier drafts.

## Quick Reference

- Approach: mobile-first, base styles for the smallest screen, enhanced upward with min-width queries.
- Type and spacing: clamp() fluid values.
- Container queries: baseline since 2023.
- Breakpoints: added only where the layout breaks; defined by content, not by device.
- Max-width: capped so lines do not stretch on very wide screens.
- Judger check widths: 320px, 768px and 1280px.
- Preview viewports: 1440, 768 and 375.
- Google mobile-first indexing: the mobile version is the ranking version.
- Grids: fluid, so column counts follow the available width.
- Images: flexible, never forcing their container wider.
- Result: no pinch-zoom, no horizontal scroll, single-column on phone, multi-column on desktop.
- Content and priority: the same on phone and desktop.
- Checked widths: 320px, 768px, 1280px, plus the 1440/768/375 preview viewports.

## Procedure

### 1. What it is

Designing base styles for the smallest screen and enhancing upward with min-width queries; fluid grids, flexible images, clamp() fluid type, container queries (baseline since 2023), and content-defined breakpoints — Google mobile-first indexing makes the mobile version the ranking version.

### 2. How Hermes applies it

Hermes writes mobile-first CSS, uses clamp() for type and spacing, adds breakpoints only where the layout breaks, caps max-width so lines do not stretch on very wide screens, and its judger checks 320px, 768px and 1280px.

### 3. What it returns to the stage

- `build` stage record: `files` carries the mobile-first CSS and markup; `build_log` notes the breakpoints added and where each layout break occurred; `token_drift` must be empty — every width, type size and spacing value comes from a token, never a hardcoded value.

### 4. Good looks like

No pinch-zoom or horizontal scroll at any width; single-column on phone, multi-column on desktop, same content and priority throughout.

## Pitfalls

- Designing desktop first and shrinking afterwards: the mobile form becomes a cramped copy, with priority and content cut to fit.
- A breakpoint per device: breakpoints multiply, and a new device size breaks the layout again; breakpoints belong where the content breaks.
- No max-width cap: on very wide screens lines stretch until text is unreadable.
- Fixed-width elements inside a fluid grid: one fixed image or card forces horizontal scroll at small widths.
- Hiding content on mobile instead of reordering it: the mobile version is a different site, and with mobile-first indexing it is the version that ranks.
- Fixed type sizes instead of clamp(): type jumps between breakpoints or stays too small on a phone.
- Flexible images without a container limit: an image wider than its container pushes the page sideways at small widths.
- Checking only the three judger widths: a layout can pass at 320px, 768px and 1280px and still break at a width between them.
- A multi-column desktop layout with no single-column plan: the phone form is improvised during the build and the priority order changes with it.

## Verification

- No pinch-zoom and no horizontal scroll at 320px, 768px or 1280px, or at any width between them.
- Single-column on phone, multi-column on desktop, with the same content and the same priority in both.
- Lines do not stretch on very wide screens; the max-width cap holds.
- Breakpoints exist only where the layout breaks, and each one is recorded in the build log.
- Type and spacing scale smoothly through clamp(); no fixed type size jumps between breakpoints.
- Images stay inside their containers at every width.
- The layout holds at widths between 320px, 768px and 1280px, not only at the checked widths.
- Container queries adapt components to their containers where the build uses them.
- No fixed-width element forces horizontal scroll at any width.
- The build log records every breakpoint and the layout break that caused it.
- Every width, type size and spacing value comes from a token; token drift is empty.
- The stage record carries the final mobile-first CSS, not an earlier draft.
- The responsive judger checks behaviour across the 1440/768/375 viewports and the 320px, 768px and 1280px widths. The skill does the work and the judger checks it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: build. Returns: `build.files` (mobile-first CSS and markup); `build.build_log` (breakpoints and where each layout break occurred); `build.token_drift` (empty — every value tokenised). Judger boundary: responsive judger checks the build at 320px, 768px and 1280px and across the preview viewports.
