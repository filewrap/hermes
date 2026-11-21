---
name: component-states
description: "Every component with all its states designed and tested."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, components]
    category: web
    related_skills: [web-design, design-systems, micro-interactions]
    web_designer_stages: [components, build]
---

# Component Consistency and States Skill

Every reusable component has a clear purpose, a stable API, and all its states designed and tested — not just its happy path. This skill covers defining each component once, with every state, and composing complex interfaces from simple blocks. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- For every component Hermes creates, before it is used on a second page.
- When the components stage inventories buttons, inputs, cards, dialogs, alerts and navigation.
- When the build stage assembles those components into pages and must keep them identical.
- When a component is about to gain a new variant and the choice is between composing blocks and configuring another variant.
- When a state a user meets rarely — error, empty, loading — has not been designed yet and the component is already in use.

It is not for one-off page elements that are not reused and carry no state of their own. It is not a licence to configure endless variants of one component instead of composing simple blocks.

## Prerequisites

- The run is active, the coordinator loaded this skill for its stage(s).
- Tokens exist for colour, type and spacing, so every state consumes tokens rather than hardcoded values.
- The component inventory is known: the components stage record holds `inventory`.
- The design-systems skill is available for the system the components belong to.
- Microcopy exists for the states that carry words — errors that name the fix, empty states with one clear action.

## How to Run

1. Define each component once, with its purpose and its stable API: what it is for, what it accepts, what it renders.
2. Design every state before the component is used: default, hover, focus, active, disabled, loading, error, empty.
3. Consume tokens for colour, type and spacing in every state; no state invents its own values.
4. Compose complex interfaces from simple blocks rather than configuring endless variants of one component.
5. Test each state on the page where the component appears, including the states users meet least often.
6. Record the component in the inventory with all its states, so a second page reuses it unchanged.
7. When a second page needs the component in a different state, use the state already defined; do not restyle it inline for that page.
8. Check the build after assembly: any file that restyles a component inline is fixed to reuse the component instead.
9. Give each state its own microcopy where words are involved: an error that names the fix, an empty state with one clear action.
10. Check the focus state by keyboard alone, confirming it is visible on every interactive component.
11. When a component gains a new state, add it to the inventory before the component is used in that state.
12. After assembly, walk each page and confirm the same component in the same state is identical wherever it appears.

## Quick Reference

- Components: button, input, card, dialog, alert, nav — each with a clear purpose and stable API.
- States, all eight: default, hover, focus, active, disabled, loading, error, empty.
- Values: tokens for colour, type and spacing in every state.
- Composition: complex interfaces from simple blocks, not endless variants.
- Rule: no component is shipped with only its happy-path state.
- Reuse rule: a second page reuses the component unchanged; no inline restyling.
- Least-used states: loading, error and empty are designed and tested like the rest.
- Words: each state carries its own microcopy — errors name the fix, empty states offer one action.
- Focus: checked by keyboard alone, visible on every interactive component.
- New states: added to the inventory before use, never after.

## Procedure

### 1. What it is

Every reusable component (button, input, card, dialog, alert, nav) has a clear purpose, stable API and all states designed and tested — default, hover, focus, active, disabled, loading, error, empty — with consistent behaviour across the site.

### 2. How Hermes applies it

Hermes defines each component once with all its states, consumes tokens for colour/type/spacing, composes complex UI from simple blocks rather than configuring endless variants, and its judger checks hover/focus/disabled states exist.

### 3. What it returns to the stage

The components stage record in `gateway/web_designer/machine.py` (`STAGE_RECORDS`) requires `inventory`, and the build stage record requires `files`, `build_log` and `token_drift`. This skill's work fills the inventory with each component, its purpose, and all eight states, and fills the build record with the files that consume those components and the build log showing them assembled unchanged. A component recorded without its states, or a build file that restyles a component inline instead of reusing it, is not recorded as consistent. Token drift in the build record is measured against the tokens the states consume.

The inventory is the single list the build reads: a component used in the build that is not in the inventory is added to the inventory first, with all its states, rather than used silently. The build log records the assembly, so a later reader can see which files consumed which components and confirm none of them restyled a component inline.

States are where consistency is won or lost: the default state is the one everyone designs, and the other seven are the ones users actually meet when something goes wrong, loads slowly, or is empty. Designing all eight before use is what keeps the component the same component in every situation. The inventory records that design, and the build record shows it was honoured — a component whose states were designed but never built is as incomplete as one whose states were never designed.

### 4. Good looks like

The same button in the same state looks and behaves identically everywhere; no component is shipped with only its happy-path state.

A component that behaves identically in all eight states is a component that did its job. The states are the test, not the default appearance. Where a state was designed but never built, the component is incomplete, and the user will meet the gap at the worst moment. That is why the least-used states are tested like the rest.

## Pitfalls

- Shipping a component with only its happy-path state, leaving hover, focus, disabled, loading, error or empty undesigned.
- Restyling a component inline on a second page instead of reusing it, so the same button stops being the same button.
- Configuring endless variants of one component instead of composing simple blocks.
- Hardcoding colour, type or spacing in a state instead of consuming tokens.
- Designing the disabled or error state last and never testing it, so users meet it unprepared.
- Using a component in the build before it is in the inventory, so the inventory stops describing what the site is built from.
- Treating loading, error and empty as edge cases that can wait, rather than states designed with the rest.
- A state with no microcopy where words are needed — an error that says nothing, an empty state with no action.
- A focus state that is only visible with a mouse, not by keyboard.
- Using a component in a state that was never added to the inventory.

## Verification

- Every component in the inventory lists all eight states: default, hover, focus, active, disabled, loading, error, empty.
- The same component in the same state looks and behaves identically on every page.
- No build file restyles a component inline; complex interfaces are composed from simple blocks.
- Every component used in the build is in the inventory, with all its states.
- Loading, error and empty states have been tested on the page where the component appears.
- Each state carries its own microcopy where words are involved.
- Focus is visible by keyboard alone on every interactive component.
- No component is used in a state missing from the inventory.
- The visual judger checks hover, focus and disabled states exist, and the a11y judger checks focus visibility and disabled-state clarity. The skill does the work and the judgers check it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: components, build. Returns: components stage-record key `inventory` and build stage-record keys `files`, `build_log`, `token_drift`. Judger boundary: visual judger checks state existence; a11y judger checks focus and disabled states.
