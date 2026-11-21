"""Rule registry R001-R110 for the web-designer add-on (audit section 4).

Nothing is dropped: all 110 entries are present. R070/R071 are merged per
the audit's own cleanup note (and the build decision): R071 is retained as
a pointer to R070 so a lookup by either id resolves. ``implemented`` means
Phase 3 ships a deterministic local check for the rule; ``needs_live``
means the check runs only when a live measurement is supplied (Lighthouse
field/lab numbers, or a live feedback-latency probe for R045) and the
rule is otherwise listed as unchecked — never reported as passing
without evidence. No rule is human-owned in Phase 3: every rule has an
owning code judger, and a judger lists rules it could not evaluate as
unchecked in its verdict, never as a silent pass.
"""
from __future__ import annotations

from typing import Any

# judger keys: the five stage 19-23 judgers, plus "human" for rules no
# code judger can evaluate in Phase 1.
JUDGERS = ("visual", "a11y", "performance", "content", "responsive")

# Each entry: id, category, judger, implemented, needs_live, statement.
_RULES: tuple[tuple[str, str, str, bool, bool, str], ...] = (
    ("R001", "geometry", "visual", True, False,
     "Use the golden ratio phi ~1.618 for key proportions: content-to-sidebar width, hero dimensions, type-size steps."),
    ("R002", "geometry", "visual", True, False,
     "Divide a composition into a 3x3 grid and place focal points on intersections and horizons on third lines, not dead centre."),
    ("R003", "geometry", "visual", True, False,
     "Lay pages out on a 12-column grid with consistent 20-24px gutters and margins at least one gutter wide."),
    ("R004", "geometry", "visual", True, False,
     "Set body line-height, then make all vertical spacing a multiple of it (4pt baseline; line-heights divisible by 4 or 8)."),
    ("R005", "geometry", "visual", True, False,
     "Align every element to a shared vertical or horizontal axis; never place elements at arbitrary offsets."),
    ("R006", "geometry", "responsive", True, False,
     "Add a breakpoint only where the content actually breaks, not at a fixed device width."),
    ("R007", "geometry", "visual", True, False,
     "Use CSS Grid for two-dimensional page structure and Flexbox for one-dimensional component internals; never build a 2D page with Flexbox alone."),
    ("R008", "geometry", "visual", True, False,
     "Use subgrid so titles, descriptions and CTAs align across all cards in a row."),
    ("R009", "typography", "visual", True, False,
     "Set body text at 16px minimum (17-20px preferred on desktop); never below 16px for body copy."),
    ("R010", "typography", "visual", True, False,
     "Set body line-height to 1.5-1.7 (unitless); longer lines need the higher end."),
    ("R011", "typography", "visual", True, False,
     "Keep body lines at 45-75 characters (66 ideal, max-width ~66ch); WCAG AAA caps at 80."),
    ("R012", "typography", "visual", True, False,
     "Generate font sizes from one mathematical ratio (1.125, 1.2, 1.25 or 1.333) with named steps; never pick arbitrary sizes."),
    ("R013", "typography", "visual", True, False,
     "Use at most two typefaces per site (one for headings, one for body, or one family with weights)."),
    ("R014", "typography", "visual", True, False,
     "Use heading line-height 1.05-1.25; large type needs proportionally less leading."),
    ("R015", "typography", "visual", True, False,
     "Tighten letter-spacing as type grows (-0.01 to -0.04em for large type); add +0.05 to +0.1em for ALL-CAPS text."),
    ("R016", "typography", "visual", True, False,
     "Scale type fluidly with clamp() between a minimum and maximum, never dropping body text below 16px."),
    ("R017", "typography", "visual", True, False,
     "Signal hierarchy with size, weight and colour in a consistent order: H1 largest, then H2, H3, body."),
    ("R018", "typography", "visual", True, False,
     "Left-align (start-align) body copy; do not justify long text and do not centre paragraphs longer than a line or two."),
    ("R019", "color", "visual", True, False,
     "Allocate roughly 60% dominant (usually neutral background), 30% secondary, 10% accent."),
    ("R020", "color", "visual", True, False,
     "For a single accent that pops, use the complementary hue (180 degrees opposite); keep it rare per 60-30-10."),
    ("R021", "color", "visual", True, False,
     "For calm, cohesive surfaces use analogous hues (neighbours within ~30 degrees) or a monochromatic ramp of one hue."),
    ("R022", "color", "visual", True, False,
     "Use at most 3 hues plus neutrals in a product UI; derive tints/shades from those hues in HSL/OKLCH."),
    ("R023", "color", "visual", True, False,
     "Keep semantic meanings consistent everywhere: red = error/destructive, green = success, amber = warning, blue = info."),
    ("R024", "color", "visual", True, False,
     "Pair every colour signal with an icon, label, shape or pattern (WCAG 1.4.1); never use colour as the sole cue."),
    ("R025", "color", "visual", True, False,
     "Build dark mode as its own theme: base near-black (#121212, not pure black), higher surfaces lighter; never invert the light palette."),
    ("R026", "color", "visual", True, False,
     "Desaturate accent colours and avoid pure white text on pure black in dark mode; use off-white (~87% opacity)."),
    ("R027", "color", "visual", True, False,
     "Name colours by role (--color-surface, --color-text-secondary, --color-error), never by value (--blue-500, --gray-200)."),
    ("R028", "spacing", "visual", True, False,
     "Make all margins, padding, gaps, heights and widths multiples of 8px (4px only as a half-step for tight inline gaps)."),
    ("R029", "spacing", "visual", True, False,
     "Space inside a component must be less than or equal to the space between components."),
    ("R030", "spacing", "visual", True, False,
     "Use one gutter size within a layout and scale section spacing in consistent steps (e.g. 24/48/96px)."),
    ("R031", "spacing", "visual", True, False,
     "Space paragraphs by roughly 75-100% of line-height (18-24px at 16px/1.5)."),
    ("R032", "spacing", "visual", True, False,
     "Leave generous whitespace around focal content; do not fill every pixel."),
    ("R033", "hierarchy", "visual", True, False,
     "Order elements by importance using size/scale, colour/contrast, proximity/grouping, alignment and repetition."),
    ("R034", "hierarchy", "visual", True, False,
     "Give each view one visually dominant primary action; secondary actions must be clearly subordinate."),
    ("R035", "hierarchy", "visual", True, False,
     "Make only one item visually distinct at a time; the item that differs from its neighbours is the one remembered."),
    ("R036", "hierarchy", "visual", True, False,
     "Treat the F-pattern as a symptom of unformatted text: use headings, bullets and front-loaded lines so users do not fall into F-scanning."),
    ("R037", "hierarchy", "visual", True, False,
     "On simple, visual-light pages arrange key items along a Z: logo top-left, key content diagonal, CTA bottom-right."),
    ("R038", "hierarchy", "visual", True, False,
     "Write strong, information-carrying headings so users can scan heading-to-heading (layer-cake pattern)."),
    ("R039", "hierarchy", "visual", True, False,
     "Put the most important information and the primary action in the first viewport; do not bury the point below decoration."),
    ("R040", "ux", "responsive", True, False,
     "Make important targets large and close: acquisition time depends on target size and distance (Fitts, 1954)."),
    ("R041", "ux", "content", True, False,
     "Decision time grows logarithmically with the number and complexity of choices; cut and group options."),
    ("R042", "ux", "content", True, False,
     "Users spend most time on other sites, so they expect yours to work like those; follow established conventions."),
    ("R043", "ux", "content", True, False,
     "Working memory holds about 7\u00b12 chunks (Miller, 1956), revised to 4\u00b11 (Cowan, 2001); chunk information into groups of 4-7."),
    ("R044", "ux", "content", True, False,
     "Every system has irreducible complexity; push it onto the system, not the user (Tesler)."),
    ("R045", "ux", "performance", True, True,
     "Keep system response under 400ms to hold flow (Doherty threshold)."),
    ("R046", "ux", "content", True, False,
     "People judge an experience by its emotional peak and its ending, not the average (Peak-End Rule)."),
    ("R047", "ux", "content", True, False,
     "Attractive designs are perceived as more usable — but beauty cannot rescue a broken core flow (Aesthetic-Usability Effect)."),
    ("R048", "ux", "content", True, False,
     "Be liberal in what you accept, conservative in what you emit (Postel's Law)."),
    ("R049", "ux", "content", True, False,
     "A screen should be self-evident, and when it cannot be, self-explanatory (Don't Make Me Think)."),
    ("R050", "ux", "content", True, False,
     "Show only what is needed now; defer advanced options behind a clear expansion, step or secondary screen (Progressive Disclosure)."),
    ("R051", "ux", "content", True, False,
     "Make options, actions and objects visible; never force users to remember information from another part of the interface."),
    ("R052", "ux", "content", True, False,
     "Provide a clearly marked emergency exit and undo for every consequential action; never trap users."),
    ("R053", "ux", "content", True, False,
     "Keep words, actions, styles and layouts consistent within the site and with platform conventions."),
    ("R054", "ux", "content", True, False,
     "Keep users informed with timely feedback within a reasonable time (Visibility of System Status)."),
    ("R055", "ux", "content", True, False,
     "Speak the users' language with familiar words and concepts, not internal jargon (Match the Real World)."),
    ("R056", "ux", "content", True, False,
     "Prevent errors before they happen with constraints, good defaults and confirmation (Error Prevention)."),
    ("R057", "ux", "content", True, False,
     "Errors must state in plain language what went wrong and constructively suggest a fix (Error Recovery)."),
    ("R058", "ux", "content", True, False,
     "Every extra unit of information competes with relevant units and diminishes their visibility (Minimalist Design)."),
    ("R059", "psychology", "content", True, False,
     "Too many options reduce satisfaction and action (Choice Overload / Paradox of Choice); limit and curate choices."),
    ("R060", "psychology", "content", True, False,
     "Minimise extraneous cognitive load so mental effort goes to the task itself (Sweller)."),
    ("R061", "psychology", "content", True, False,
     "People best remember the first and last items in a list; place the most important items at the start and end (Serial Position)."),
    ("R062", "psychology", "visual", True, False,
     "Objects close together are perceived as related; use spacing, not lines, to signal grouping (Gestalt: Proximity)."),
    ("R063", "psychology", "visual", True, False,
     "Elements that look alike are perceived as related and sharing function (Gestalt: Similarity)."),
    ("R064", "psychology", "visual", True, False,
     "Elements inside a shared boundary are seen as one group (Gestalt: Common Region)."),
    ("R065", "psychology", "visual", True, False,
     "The mind fills in gaps to see a complete shape; use partial shapes and peeks to suggest more (Gestalt: Closure)."),
    ("R066", "psychology", "visual", True, False,
     "The eye follows lines, curves and sequences; arrange elements along a smooth path (Gestalt: Continuity)."),
    ("R067", "psychology", "visual", True, False,
     "Make the separation between figure (focus) and ground (background) clear (Gestalt: Figure-Ground)."),
    ("R068", "psychology", "visual", True, False,
     "Elements that move together are seen as a group; animate grouped items together (Gestalt: Common Fate)."),
    ("R069", "psychology", "visual", True, False,
     "People perceive ambiguous images in the simplest form possible; prefer simple, unambiguous layouts (Gestalt: Pragnanz)."),
    ("R070", "psychology", "visual", True, False,
     "Merged R070/R071: use symmetry for stability and calm and deliberate asymmetry for emphasis; symmetrical arrangements feel balanced and grouped, and visual links (lines, tabs, shared borders) signal relatedness."),
    ("R071", "psychology", "human", False, False,
     "MERGED into R070 (audit cleanup note: near-duplicate symmetry rules). Lookup by R071 resolves to R070."),
    ("R072", "accessibility", "a11y", True, False,
     "Normal text must reach at least 4.5:1 contrast against its background (WCAG 2.2 SC 1.4.3, AA)."),
    ("R073", "accessibility", "a11y", True, False,
     "Aim for AAA 7:1 for normal text; large text (18pt/24px regular or 14pt/18.66px bold) needs at least 3:1 at AA and 4.5:1 at AAA."),
    ("R074", "accessibility", "a11y", True, False,
     "UI component boundaries, focus indicators and meaningful graphics need at least 3:1 against adjacent colours (WCAG 1.4.11)."),
    ("R075", "accessibility", "a11y", True, False,
     "All functionality must be operable by keyboard alone (Tab, Shift+Tab, Enter/Space, arrows, Esc); never require a mouse or hover."),
    ("R076", "accessibility", "a11y", True, False,
     "Show a visible focus indicator (e.g. :focus-visible outline 3px + 2px offset); never set outline:none without a replacement."),
    ("R077", "accessibility", "a11y", True, False,
     "Use the right element for the job: button for actions, a for navigation, nav/main/header/footer, h1-h6 in order; never a div as a button."),
    ("R078", "accessibility", "a11y", True, False,
     "Every informative image needs concise descriptive alt text (<=125 chars); decorative images get alt=\"\"."),
    ("R079", "accessibility", "a11y", True, False,
     "Provide a skip-to-content link as the first focusable element on every page."),
    ("R080", "accessibility", "a11y", True, False,
     "Users must always be able to Tab away from any component, and Esc must close modals, menus and dialogs (WCAG 2.1.2)."),
    ("R081", "accessibility", "a11y", True, False,
     "Focus order must follow a logical sequence matching the visual reading order (top-to-bottom, left-to-right in LTR)."),
    ("R082", "accessibility", "a11y", True, False,
     "Content must survive 200% text resize and 1.5x line/paragraph spacing without clipping or overlap (WCAG 1.4.4/1.4.12), and nothing may flash more than 3 times per second (WCAG 2.3.1)."),
    ("R083", "performance", "performance", True, True,
     "Largest Contentful Paint must be 2.5s or less at the 75th percentile of real visits (Core Web Vitals)."),
    ("R084", "performance", "performance", True, True,
     "Interaction to Next Paint must be 200ms or less at p75."),
    ("R085", "performance", "performance", True, True,
     "Cumulative Layout Shift must be 0.1 or less at p75; reserve space for images, embeds and fonts so content never jumps."),
    ("R086", "performance", "performance", True, False,
     "Set explicit budgets (e.g. LCP <=2.5s, JS <=200KB, critical path <=250KB, requests <=50) and fail the build when exceeded."),
    ("R087", "performance", "performance", True, False,
     "Serve responsive images with srcset/sizes and modern formats (WebP/AVIF with fallback); use picture for art direction."),
    ("R088", "performance", "performance", True, False,
     "Use loading=lazy only for below-fold images, never for the LCP image; give the hero fetchpriority=high."),
    ("R089", "performance", "performance", True, False,
     "Always set width and height (or aspect-ratio) on every img so the browser reserves layout space before load."),
    ("R090", "motion", "visual", True, False,
     "Keep UI animations under 300ms (micro 100-150ms, standard 150-250ms, modals 200-300ms); use ease-out for entering elements."),
    ("R091", "motion", "visual", True, False,
     "Animate only transform and opacity; never animate width, height, top/left or other layout properties."),
    ("R092", "motion", "visual", True, False,
     "Respect prefers-reduced-motion: disable or replace non-essential motion (WCAG 2.3.3/2.2.2); keep the final state and meaning."),
    ("R093", "motion", "visual", True, False,
     "Give every animation a job — orientation, continuity or feedback — and cut decorative motion that slows users down."),
    ("R094", "motion", "visual", True, False,
     "Elements that move together must share easing and duration (modal+overlay, tooltip+arrow, drawer+backdrop)."),
    ("R095", "mobile", "responsive", True, False,
     "Make touch targets at least 44x44px (Apple HIG) / 48x48dp (Material), with 8px gaps; WCAG 2.2 AA floor is 24x24px (2.5.8)."),
    ("R096", "mobile", "responsive", True, False,
     "Place primary actions in the bottom-centre thumb zone; never put critical actions in top corners."),
    ("R097", "mobile", "responsive", True, False,
     "Design for the smallest, most constrained screen first, then add complexity as space grows (mobile-first)."),
    ("R098", "mobile", "responsive", True, False,
     "Never hide essential information or actions behind hover; touch devices have no hover state."),
    ("R099", "mobile", "responsive", True, False,
     "Use env(safe-area-inset-*) for fixed/sticky elements and dvh/svh instead of vh for full-screen layouts."),
    ("R100", "forms", "content", True, False,
     "Lay forms out in a single column with labels above each field; never use multi-column form layouts."),
    ("R101", "forms", "content", True, False,
     "Always render a persistent visible label outside the input (label for); placeholder text is a hint only, never the label (WCAG 3.3.2)."),
    ("R102", "forms", "content", True, False,
     "Validate on blur (field exit), clear the error live as it is corrected, and write errors that state what is wrong and how to fix it; never clear entered data after an error."),
    ("R103", "forms", "content", True, False,
     "Ask only for fields genuinely needed to fulfil the request; Baymard's ideal checkout is 6-8 fields."),
    ("R104", "forms", "content", True, False,
     "Use the correct type/inputmode/autocomplete for each field (email, tel, numeric, address)."),
    ("R105", "navigation", "content", True, False,
     "Keep primary navigation to 5-7 top-level items in plain customer language, grouping the rest beneath them."),
    ("R106", "navigation", "content", True, False,
     "The 3-click rule is a myth: optimise each step for clarity and strong link scent instead of counting clicks."),
    ("R107", "navigation", "content", True, False,
     "Keep main navigation visible on desktop (no hamburger on desktop) and show breadcrumbs on pages deeper than two levels."),
    ("R108", "content", "content", True, False,
     "Front-load every page and paragraph with the most important information first (inverted pyramid / BLUF)."),
    ("R109", "content", "content", True, False,
     "Write in plain language, short sentences (<=20 words) and short paragraphs (2-4 sentences); cut word count by ~50% versus print."),
    ("R110", "trust", "content", True, False,
     "Show the full total before checkout, offer guest checkout as the default path, place honest trust signals next to the CTA, and never use fake urgency, fake scarcity or confirm-shaming."),
)


def rule_record(rule_id: str) -> dict[str, Any]:
    """The registry entry for a rule id. R071 resolves to the merged R070."""
    wanted = "R070" if rule_id == "R071" else rule_id
    for rid, category, judger, implemented, needs_live, statement in _RULES:
        if rid == wanted:
            return {
                "id": rid, "category": category, "judger": judger,
                "implemented": implemented, "needs_live": needs_live,
                "statement": statement,
                "merged_into": "R070" if rule_id == "R071" else None,
            }
    raise KeyError(f"unknown web-designer rule {rule_id!r}")


def all_rule_ids() -> list[str]:
    return [rid for rid, *_ in _RULES]


def rules_for_judger(judger: str) -> list[dict[str, Any]]:
    """Registry entries owned by one judger (human-owned rules included so
    a judger can list exactly what it does not evaluate)."""
    out = []
    for rid, category, j, implemented, needs_live, statement in _RULES:
        if j == judger:
            out.append({
                "id": rid, "category": category, "judger": j,
                "implemented": implemented, "needs_live": needs_live,
                "statement": statement,
            })
    return out


def implemented_rule_ids(judger: str) -> list[str]:
    return [r["id"] for r in rules_for_judger(judger) if r["implemented"]]


def unchecked_rule_ids(judger: str, *, live_measured: bool = False) -> list[str]:
    """Rule ids the judger cannot evaluate: unimplemented rules, plus
    live-only rules when no live measurement was supplied."""
    out = []
    for r in rules_for_judger(judger):
        if not r["implemented"] or r["needs_live"] and not live_measured:
            out.append(r["id"])
    return out
