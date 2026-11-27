---
name: micro-interactions
description: "Every tap answers back within two hundred milliseconds."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [web, design, motion]
    category: web
    related_skills: [web-design, motion-design, component-states]
    web_designer_stages: [components, build]
---

# Micro-Interactions Skill

Give every single user action an immediate, visible answer. This skill covers small single-task feedback loops — button press, toggle slide, like fill, field shake, cart fly-to-icon, hover lift — and their timing. It does not cover page-level transitions, which belong to motion design, and it does not cover the component states themselves, which belong to component-states. Load-on-demand only, loaded by the web-designer coordinator for its stages; never auto-loads; never contacts the user directly — the coordinator is the only router.

## When to Use

- Around every single user action where confirmation or state change needs to be felt.
- For primary actions, toggles, likes, form fields, cart actions and hover states.
- When an action changes state silently and the visitor cannot tell whether it worked.
- When a form field fails and the visitor needs to know what to fix, at the moment it fails.
- When a success must be confirmed without interrupting the flow.
- Not for page loads or route changes — those are transitions, not micro-interactions.
- Not for feedback that needs a modal or a page: a micro-interaction confirms without interrupting.

## Prerequisites

- The run is active and the coordinator loaded this skill for its stage or stages.
- The component inventory is known, so every primary action and interactive element is listed.
- Motion tokens are defined, so feedback timing is a token value rather than a one-off number.
- The component states are defined, so the feedback has a state to show for each action.

## How to Run

1. List every primary action and interactive element from the component inventory.
   - Include buttons, toggles, likes, form fields, cart actions and hover states.
   - Include the failure case for each one, not only the success case.
2. For each one, define the loop in Dan Saffer's structure: trigger, rules, feedback, loops and modes.
   - The trigger is the action the visitor takes.
   - The rules say what happens in response.
   - The feedback is what the visitor sees and feels.
   - Loops and modes say what repeats and what state the element ends in.
3. Choose the feedback: press scale, toggle slide, success check, field shake, hover lift, or the equivalent for the action.
   - A press scales down briefly, then returns.
   - A toggle slides to its new position.
   - A success shows a check; a failure shakes and explains.
4. Keep the feedback to transform and colour, and keep it under about 300ms, with the visible answer inside 200ms.
   - The answer must be visible before the visitor decides nothing happened.
5. Make failure feedback explain: a failed field shakes and explains what to fix.
   - The explanation names the fix, in the visitor's words, beside the field.
6. Record each micro-interaction against its component in the stage record.
   - Record the trigger, rules, feedback, and loops and modes for each one.
7. Re-check after any fix, and record what changed.
   - A changed feedback or timing is re-checked against the 200ms answer and the 300ms cap before the stage closes.
   - The feedback recorded is the feedback in the final build, not an earlier draft.

## Quick Reference

- Structure (Dan Saffer): trigger, rules, feedback, loops/modes.
- Duration: each micro-interaction under about 300ms.
- Visible answer: every tap answers back in under 200ms.
- Feedback properties: transform and colour.
- Examples: button press, toggle slide, like fill, field shake, cart fly-to-icon, hover lift.
- Failure: a failed field shakes and explains; success is confirmed without a modal.

## Procedure

### 1. What it is

Small single-task feedback loops — trigger, rules, feedback, loops/modes (Dan Saffer's structure): button press, toggle slide, like fill, field shake, cart fly-to-icon, hover lift — each under ~300ms.

### 2. How Hermes applies it

Hermes adds a micro-interaction to each primary action (press scale, toggle slide, success check), keeps it to transform/colour, and its judger checks every interactive element gives immediate visible feedback.

### 3. What it returns to the stage

- `components` stage record: `inventory` carries the micro-interaction for each component — trigger, rules, feedback, and loops/modes — beside the component it belongs to.
- `build` stage record: `files` carries the transform and colour feedback; `build_log` notes the feedback per primary action; `token_drift` must be empty.

### 4. Good looks like

Every tap answers back in <200ms; a failed field shakes and explains; a success is confirmed without a modal.

## Pitfalls

- An action with no feedback: the visitor taps again, submits twice, or assumes the tap failed.
- Feedback slower than 200ms: the answer arrives after the visitor has already decided nothing happened.
- Feedback that needs a modal: a success message that interrupts the flow for news a check mark could carry.
- A failed field that only shakes: motion without words leaves the visitor guessing what to fix; the shake must come with an explanation.
- Feedback on layout properties: jank instead of a crisp answer; feedback stays on transform and colour.
- A micro-interaction on every element, including static text: feedback everywhere means feedback nowhere.
- A toggle with no visible end state: the visitor cannot tell whether the toggle is on or off after the slide.
- A like that fills with no count change: the feedback contradicts the state it claims to show.
- A cart action with no fly-to-icon or count change: the visitor adds an item and the cart gives no sign it arrived.

## Verification

- Every primary action has a micro-interaction, and every interactive element gives immediate visible feedback.
- Every tap answers back in under 200ms, and each micro-interaction runs under about 300ms.
- Feedback uses transform and colour only.
- A failed field shakes and explains the fix; a success is confirmed without a modal.
- Every toggle, like and cart action ends in a visible state the visitor can read.
- No action is confirmed by a modal where a check mark would do.
- Every micro-interaction is recorded against its component with trigger, rules, feedback, and loops and modes.
- Every toggle, like and cart action ends in a visible state the visitor can read.
- No action is confirmed by a modal where a check mark would do.
- Feedback timing comes from tokens; token drift is empty.
- No micro-interaction sits on static text or any other element with no action behind it.
- The build log notes the feedback per primary action.
- The stage record carries the final feedback, not earlier drafts.
- The visual judger checks immediate visible feedback on every interactive element; the performance judger checks interaction timing against the INP budget of under 200ms. The skill does the work and the judgers check it; the skill never grades its own output.

## Coordinator wiring

Loaded by stages: components, build. Returns: `components.inventory` (micro-interaction per component: trigger, rules, feedback, loops/modes); `build.files` (transform and colour feedback). Judger boundary: visual judger checks immediate visible feedback; performance judger checks interaction timing (INP under 200ms).
