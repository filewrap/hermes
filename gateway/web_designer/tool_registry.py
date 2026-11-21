"""The 64 audit tools (section 2) as a registry: reference plus live slots.

Phase 3: every entry now carries its audit text verbatim — "what" (the
audit's "What it does") and "hermes_use" (the audit's "Hermes use") —
alongside name, URL, bucket, licence and kind. Exactly nine entries are
``kind="live"``: checks verifiable locally with no network. The other 55
are ``kind="reference"``: a URL and licence, no network call, no API key,
no new dependency. A reference entry is NEVER claimed as a live check.

Live slots and their checkers (``gateway/web_designer/judgers/checkers.py``
unless noted):
- ``html_parse`` (W3C Markup Validation Service slot): local HTML structure
  parse via ``judgers.common.parse_html`` — the W3C service itself is
  never called; only the local parse runs.
- ``css_validity`` (W3C CSS Validation Service slot): local CSS validity
  check; the W3C service itself is never called.
- ``link_check`` (Dead Link Checker slot): internal links/assets resolve
  inside the project; external links are recorded unchecked, never
  claimed checked.
- ``lighthouse`` / ``axe``: run ONLY when the binary exists on the host
  (``shutil.which`` at call time; otherwise reference behaviour).
- ``contrast`` (WebAIM Contrast Checker slot): local WCAG contrast math via
  ``judgers.common.contrast_ratio`` — the WebAIM page is never called.
- ``visual_diff`` (BackstopJS slot): a LOCAL visual diff implemented with
  PIL pixel comparison of baseline vs current screenshots. The BackstopJS
  binary is NOT run and must never be implied to run; ``live_via`` says so.
- ``font_sources`` (Google Fonts / Fontshare slots): local check that fonts
  used come from allowed open sources or local WOFF2/WOFF files.

Three reference entries name a genuine live stand-in in
``live_equivalent``: PageSpeed Insights -> Lighthouse, WAVE -> axe DevTools,
Responsinator -> the responsive judger's 1440/768/375 sweep. No other
equivalences are claimed.

The Tailwind / shadcn / daisyUI overlap is by design (audit): Tailwind
CSS, shadcn/ui and daisyUI appear with different roles (project vs
tool); all three stay in the registry.
"""
from __future__ import annotations

import shutil
from typing import Any

BUCKETS = (
    "inspiration/awards",
    "idea galleries",
    "templates/components",
    "validators/bug finders",
    "performance",
    "accessibility",
    "SEO",
    "visual-diff",
    "fonts",
    "colours",
    "icons",
    "imagery",
    "CSS generators",
    "responsiveness",
)

# name, url, bucket, license, kind, what, hermes_use
_TOOLS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    ("Awwwards", "https://www.awwwards.com/", "inspiration/awards",
     "Free to browse; paid submission (~$65 standard)", "reference",
     "Juried web-design awards platform and winners gallery (Site of the Day/Month/Year, Developer Award), filterable by category, technology, country and colour; ~10,000 awarded sites.",
     "Inspiration and quality bar: pull SOTD winners in the user's niche as reference before designing, and score Hermes builds against its design/usability/creativity/content criteria in the judging step."),
    ("CSS Design Awards", "https://www.cssdesignawards.com/", "inspiration/awards",
     "Free to browse; submission $50", "reference",
     "Independent web-design awards judging UI, UX and innovation, with daily Website of the Day (needs judge average above 8.00), Special Kudos, monthly and yearly awards.",
     "Second design opinion and judging rubric: use its UI/UX/innovation scores as a checklist when judging a build, and study WOTD winners for polished agency/studio patterns."),
    ("The FWA", "https://thefwa.com/", "inspiration/awards",
     "Free to browse; submission \u00a399", "reference",
     "Longest-running (since 2000) awards for cutting-edge, experimental web work \u2014 WebGL, motion, immersive and AI experiences; from Nov 2026 a blind jury in a public live queue plus an AI critique (FWAi).",
     "Ceiling reference for ambitious briefs: study FWA winners when the user wants motion/3D/storytelling, and use its bar to judge whether an experimental build is genuinely ambitious or just busy."),
    ("The Webby Awards", "https://www.webbyawards.com/", "inspiration/awards",
     "Free to browse; paid per entry", "reference",
     "Mainstream digital awards run by the International Academy of Digital Arts and Sciences, covering websites, apps, video and more across many categories.",
     "Mainstream-recognition benchmark: check what wins in the user's category (e.g. e-commerce, portfolio) so a build aims at recognised craft, not just a pretty hero."),
    ("Dribbble", "https://dribbble.com/", "inspiration/awards",
     "Free to browse; paid plans from $4/month", "reference",
     "Large designer community posting 'shots' \u2014 single-screen UI, illustration, icon and branding work \u2014 searchable by colour, tag and timeframe.",
     "Fast visual direction: browse shots for colour, typography and component mood at the questioning stage; treat shots as mood, not proven layouts, since many are unbuilt concepts."),
    ("Behance", "https://www.behance.net/", "inspiration/awards",
     "Free", "reference",
     "Adobe's portfolio network hosting full project case studies \u2014 brief, research, iterations and final work \u2014 across web design, branding, motion and more.",
     "Study the reasoning behind a design, not just the screenshot: read case studies for the user's industry to ground layout and brand decisions in a real brief."),
    ("Land-book", "https://land-book.com/", "inspiration/awards",
     "Free", "reference",
     "Strictly curated gallery of landing pages and websites, filterable by industry, type, style and colour, focused on conversion-oriented pages.",
     "Landing-page structure reference: compare several pages in the user's category side by side to decide section order, proof placement and CTA repetition before building."),
    ("Lapa Ninja", "https://www.lapa.ninja/", "inspiration/awards",
     "Free to browse", "reference",
     "Catalogue of landing-page designs with full-page screenshots, categorised by industry (SaaS, finance, education, health), colour and style.",
     "Landing-page pattern lookup: fetch examples matching the user's sector and steal proven section structures (hero, features, pricing, social proof) rather than inventing them."),
    ("SiteInspire", "https://www.siteinspire.com/", "inspiration/awards",
     "Free", "reference",
     "Hand-curated web-design gallery since 2010, filterable by style, type, subject and platform; strong on studios, portfolios, cultural and editorial sites.",
     "Calm/confident design reference: use when the user wants editorial, minimal or typographically sophisticated work instead of loud award-style spectacle."),
    ("Mobbin", "https://mobbin.com/", "idea galleries",
     "Free tier (limited); Pro $10/month billed yearly", "reference",
     "Library of real screenshots and user flows from shipped apps and websites (onboarding, sign-up, checkout, settings), organised by flow and screen type, with a Figma plugin.",
     "Real-product UX patterns: look up how established products handle the exact flow the user needs (e.g. checkout, onboarding) and copy the proven flow, not a concept."),
    ("One Page Love", "https://onepagelove.com/", "idea galleries",
     "Free to browse; templates paid", "reference",
     "Showcase dedicated exclusively to single-page websites (portfolios, launches, events, personal sites), plus paid one-page templates.",
     "Single-page briefs: study how one-pagers earn every section on one scroll, and use its categories to pick a structure for landing, event or portfolio pages."),
    ("Recent", "https://recent.design/", "idea galleries",
     "Free to browse", "reference",
     "Curated gallery of ambitious, award-level websites (formerly godly.website), focused on visual craft, motion and interactive design.",
     "High-craft visual inspiration: browse for hero sections, motion ideas and art direction when the brief calls for a striking, memorable site."),
    ("Product Hunt", "https://www.producthunt.com/", "idea galleries",
     "Free", "reference",
     "Daily curation of newly launched products and webapps, with upvotes, comments and maker replies \u2014 the main launch-day discovery feed for tech products.",
     "Webapp idea validation: check what similar products launched, what users praised or criticised in comments, and what gaps exist before designing a new webapp."),
    ("HyperUI", "https://hyperui.dev/", "templates/components",
     "Free, MIT", "reference",
     "Largest free collection of Tailwind CSS components \u2014 500+ copy-paste marketing sections, heroes, feature grids, pricing tables and footers in plain HTML, no build step.",
     "Primary component source for Tailwind builds: copy a proven section, adapt copy/colours, and paste into the generated site instead of hand-writing every block."),
    ("Flowbite", "https://flowbite.com/", "templates/components",
     "Free, MIT; Pro paid", "reference",
     "Open-source Tailwind CSS UI toolkit with 400-600+ interactive components (dropdowns, modals, carousels, datepickers), plus templates and a Figma design system.",
     "Interactive components for Tailwind builds: use its JS-powered components when a build needs working dropdowns, modals or carousels without writing custom JS."),
    ("DaisyUI", "https://daisyui.com/", "templates/components",
     "Free, MIT; theme store $29-$69", "reference",
     "Tailwind CSS plugin adding pre-styled semantic components and 35+ built-in themes, so a site can be themed and prototyped very fast.",
     "Fast prototyping and theming: apply a DaisyUI theme for instant light/dark and brand colour variants, and use its components for rapid first drafts."),
    ("shadcn/ui", "https://ui.shadcn.com/", "templates/components",
     "Free, MIT", "reference",
     "Copy-paste accessible React components built on Radix/Base UI primitives and Tailwind CSS, installed via CLI directly into the project so the team owns the code; TypeScript-first.",
     "React/Next.js webapp builds: add accessible, themeable components (forms, dialogs, tables, charts) with full code ownership instead of a closed component dependency."),
    ("Tailwind CSS", "https://tailwindcss.com/", "templates/components",
     "Free, MIT", "reference",
     "Utility-first CSS framework (v4, CSS-first config) for building custom interfaces rapidly without writing bespoke CSS; the base for HyperUI, Flowbite, DaisyUI and shadcn/ui.",
     "Default styling engine for generated sites: build layouts with utility classes, define the design system via CSS variables/theme, and keep the CSS small and consistent."),
    ("Tailblocks", "https://tailblocks.cc/", "templates/components",
     "Free, MIT", "reference",
     "Collection of 60+ ready-to-use minimalist Tailwind layout blocks \u2014 hero, header, features, pricing, testimonials, team, gallery, footer, e-commerce sections.",
     "Section assembly: pick blocks matching the page plan and compose a complete landing page quickly, then customise copy and styling."),
    ("HTML5 UP", "https://html5up.net/", "templates/components",
     "Free, CCA 3.0", "reference",
     "Library of free, fully responsive HTML5/CSS3 site templates (portfolios, landing pages, business sites) that work as plain static HTML with no framework.",
     "Framework-free starting points: use a template as the skeleton for a simple static site when the user wants no build step, then restyle and replace content."),
    ("Preline UI", "https://preline.co/", "templates/components",
     "Free, MIT; Pro $249-$459", "reference",
     "Large Tailwind CSS component library (600+ components) for dashboards, landing pages and admin UIs, in HTML, React and Vue, with plugins and templates.",
     "Dashboard/admin webapp builds: source data-dense components (tables, sidebars, stats, forms) that HyperUI's marketing sections do not cover."),
    ("W3C Markup Validation Service", "https://validator.w3.org/", "validators/bug finders",
     "Free", "live",
     "Official W3C validator: checks HTML/XHTML documents by URL, file upload or direct input and lists markup errors, with source view and outline.",
     "Pre-ship gate: validate every generated page's HTML and fix errors before judging; a page that fails validation fails the build step."),
    ("W3C CSS Validation Service", "https://jigsaw.w3.org/css-validator/", "validators/bug finders",
     "Free", "live",
     "Official W3C validator for CSS: checks stylesheets by URL, upload or direct input against CSS specifications and reports errors and warnings.",
     "Pre-ship gate: validate generated CSS (including Tailwind output) to catch invalid properties, typos and unsupported syntax before delivery."),
    ("Dead Link Checker", "https://www.dead-link-checker.com/", "validators/bug finders",
     "Free tier (up to 6,000 pages per run); Standard \u00a54,400/month", "live",
     "Online broken-link scanner: crawls a whole site, reports broken links with HTTP status, linking page and error reason, with scheduled checks and CSV export.",
     "Link audit in the judging step: scan the built site for 404s, redirect chains and dead external links and fix them before calling the site done."),
    ("Sentry", "https://sentry.io/", "validators/bug finders",
     "Free tier; paid plans", "reference",
     "Error tracking and performance monitoring: captures JavaScript runtime errors, stack traces and affected users from live sites, with alerts and release tracking.",
     "Live bug-finding after launch: wire Sentry into a shipped webapp so real user-facing JS errors are caught and reported instead of silently breaking the experience."),
    ("PageSpeed Insights", "https://pagespeed.web.dev/", "performance",
     "Free", "reference",
     "Google's free tool combining a Lighthouse lab test with real-user Chrome User Experience Report field data (Core Web Vitals: LCP, INP, CLS) for any public URL.",
     "Primary performance judge: run on every built site's URL, report the Core Web Vitals assessment, and fix what the diagnostics list before delivery."),
    ("Lighthouse", "https://github.com/GoogleChrome/lighthouse", "performance",
     "Free, open source (Apache-2.0)", "live",
     "Open-source automated auditing engine (also in Chrome DevTools and behind PageSpeed Insights) scoring performance, accessibility, best practices and SEO 0-100 with named failing audits.",
     "Local/offline audit engine: run Lighthouse via CLI/DevTools on local or password-protected builds that PageSpeed Insights cannot reach, and use its scores as the build's pass bar."),
    ("WebPageTest", "https://www.webpagetest.org/", "performance",
     "Free tier; paid plans", "reference",
     "Deep performance testing from real browsers, locations and connections, with waterfall charts, filmstrips, repeat-view (caching) tests and multi-step scripts.",
     "Deep diagnosis when PageSpeed flags a problem: run a waterfall/filmstrip test to find the exact slow request, render-blocking script or oversized image."),
    ("GTmetrix", "https://gtmetrix.com/", "performance",
     "Free trial (5 tests/month for 3 months); paid from ~$14/month", "reference",
     "Page-speed testing using Lighthouse metrics and Web Vitals with its own grade, waterfall charts, history and scheduled monitoring from chosen test locations.",
     "Trend tracking: test the built page, keep the report, and re-test after fixes to prove load time actually improved rather than claiming it did."),
    ("WAVE", "https://wave.webaim.org/", "accessibility",
     "Free online tool and extensions; API paid in credits", "reference",
     "WebAIM's Web Accessibility Evaluation Tool: renders WCAG errors, alerts, structural elements and ARIA as icons directly on the tested page, plus browser extensions and an API.",
     "First accessibility judge: run WAVE on every built page, fix red-icon errors (missing alt, labels, contrast) in context, and use it as the visual check a non-expert user can follow."),
    ("axe DevTools", "https://www.deque.com/axe/", "accessibility",
     "Free extension; Pro ~$40-$50/user/month", "live",
     "Deque's browser extension built on the open-source axe-core rules engine; runs WCAG checks in-browser with low false positives and detailed remediation guidance, and feeds CI via axe-core.",
     "Developer-grade accessibility audit: run axe DevTools (and axe-core in CI) on built pages for WCAG-tagged violations with fix guidance, as the second opinion alongside WAVE."),
    ("Accessibility Insights for Web", "https://accessibilityinsights.io/", "accessibility",
     "Free, open source (MIT)", "reference",
     "Microsoft's free browser extension for guided manual accessibility testing: FastPass for quick checks and Assessment for full WCAG 2.1 AA verification, including keyboard and tab-order checks.",
     "Guided manual checks automation cannot do: walk the built site's keyboard flow, focus order and tab stops with its Assessment before signing off accessibility."),
    ("Pa11y", "https://pa11y.org/", "accessibility",
     "Free, open source", "reference",
     "Scriptable command-line accessibility tester: runs WCAG checks against URLs, produces reports, and is designed for scheduled sweeps and CI dashboards.",
     "Automated accessibility sweeps: run Pa11y over every page of a multi-page build in CI and fail the build on new WCAG violations."),
    ("WebAIM Contrast Checker", "https://webaim.org/resources/contrastchecker/", "accessibility",
     "Free", "live",
     "Reference WCAG contrast-ratio checker: enter foreground/background colours, get the ratio and AA/AAA pass-fail, with lighten/darken suggestions until a pair passes.",
     "Colour gate: check every text/background pair in the chosen palette against 4.5:1 (AA) before building, and fix failing pairs at the palette stage, not after launch."),
    ("Google Search Console", "https://search.google.com/search-console", "SEO",
     "Free", "reference",
     "Google's free first-party tool showing indexing coverage, search queries/clicks, Core Web Vitals and crawl errors for a verified site \u2014 the only source of what Google itself reports.",
     "Post-launch SEO truth: verify the built site in Search Console, check index coverage and Core Web Vitals, and fix excluded pages or errors it reports."),
    ("Bing Webmaster Tools", "https://www.bing.com/webmasters", "SEO",
     "Free", "reference",
     "Bing's free webmaster console with a Site Scan crawler, index data, keyword and SEO reports, and a monthly crawl quota.",
     "Free full-site crawl as a second SEO opinion: run Site Scan on the built site to catch technical issues Google Search Console does not surface."),
    ("Ahrefs Free", "https://ahrefs.com/webmaster-tools", "SEO",
     "Free for verified sites (5,000 crawl credits/project/month); Lite from $129/month", "reference",
     "Ahrefs' free tier for verified site owners: Site Audit checking 170+ technical/on-page issues, plus Site Explorer backlink/keyword data and web analytics.",
     "Technical SEO audit for owned sites: run Site Audit on the built site, fix the prioritised issues, and track the health score over time."),
    ("Screaming Frog SEO Spider", "https://www.screamingfrog.co.uk/seo-spider/", "SEO",
     "Free up to 500 URLs per crawl; licence $279/year", "reference",
     "Desktop site crawler for deep technical SEO audits: finds broken links, redirect chains, duplicate titles, missing meta descriptions, and more, on the user's own machine.",
     "Deep crawl audit: crawl the built site locally to find broken links, duplicate metadata and redirect chains that single-page checkers miss."),
    ("Semrush", "https://www.semrush.com/", "SEO",
     "Free account (limited, 100-page audit/month); Pro from ~$139/month", "reference",
     "All-in-one SEO/marketing suite: Site Audit (crawlability, speed, HTTPS, markup), keyword research, rank tracking, backlink analysis and an on-page SEO checker.",
     "Full SEO audit and competitor check: run Site Audit on the built site and compare its on-page SEO against competing sites in the same niche."),
    ("Percy", "https://percy.io/", "visual-diff",
     "Free tier (5,000 screenshots/month); paid from $199/month", "reference",
     "BrowserStack's cloud visual testing: captures DOM snapshots, re-renders them across browsers and responsive widths, diffs against baselines and gives a review UI with branch-aware baselines.",
     "Visual regression gate: snapshot the built site across browsers/widths on each change and approve or reject diffs before shipping, so a CSS change cannot silently break a layout."),
    ("Applitools Eyes", "https://applitools.com/", "visual-diff",
     "Trial; paid (contact sales / metered checkpoints)", "reference",
     "Enterprise visual testing with Visual AI: perceptually diffs screenshots, suppresses meaningless pixel noise, and renders across browsers/devices via the Ultrafast Grid, with DOM-level root-cause analysis.",
     "High-accuracy visual judging for large builds: use its AI diffing when pixel-diff noise would drown a big multi-page site's review, and let it flag only human-noticeable regressions."),
    ("Chromatic", "https://www.chromatic.com/", "visual-diff",
     "Free tier (5,000 snapshots/month, Chrome); paid from $149/month", "reference",
     "Visual, interaction and accessibility testing for Storybook design systems: screenshots every story per pull request, with TurboSnap testing only affected stories and axe checks on every plan.",
     "Component-level visual testing: when a build uses Storybook components, run Chromatic per change to catch component regressions and accessibility failures before they reach pages."),
    ("BackstopJS", "https://github.com/garris/BackstopJS", "visual-diff",
     "Free, open source (MIT)", "live",
     "Open-source visual regression tool (Node.js CLI): scriptable screenshot scenarios with configurable mismatch thresholds, running locally or in CI with self-hosted baselines.",
     "Free self-hosted visual regression: script screenshot scenarios for the built site's key pages/widths and diff them in CI with no per-screenshot cost."),
    ("Google Fonts", "https://fonts.google.com/", "fonts",
     "Free, open source (OFL/Apache)", "live",
     "Largest free web-font library (1,500+ open-source families), with variable-font and language filters, easy embedding and self-hosting via Fontsource.",
     "Default font source: pick heading/body fonts, embed or self-host them, and never ship a site with an unlicensed or system-only font stack."),
    ("Fontshare", "https://www.fontshare.com/", "fonts",
     "Free for commercial use", "live",
     "Free font service from the Indian Type Foundry offering high-quality, distinctive fonts for commercial use \u2014 a different flavour from Google Fonts defaults.",
     "Distinctive typography: choose Fontshare faces when the user wants a site that does not look like every Google-Fonts default (Inter/Roboto) build."),
    ("Fontjoy", "https://fontjoy.com/", "fonts",
     "Free", "reference",
     "AI-powered font-pairing tool: generates heading/body combinations from Google Fonts by typeface similarity, with lock/edit controls for each slot.",
     "Font pairing decisions: generate and lock a heading/body pairing that fits the brief's mood instead of guessing combinations, then apply it to the type scale."),
    ("Coolors", "https://coolors.co/", "colours",
     "Free; Pro paid (saves, unlimited palettes)", "reference",
     "Fast colour-palette generator (spacebar for a new palette, lock colours to refine), plus explore, image picker, contrast checker, palette visualiser and CSS/SVG/PDF export.",
     "Palette generation and validation: generate a harmonious palette for the brief, check every pair's contrast in its checker, and export the palette as CSS variables."),
    ("Adobe Color", "https://color.adobe.com/", "colours",
     "Free (Adobe account)", "reference",
     "Colour wheel with harmony rules (analogous, triad, complementary), accessibility checks and community palettes, built on colour theory.",
     "Theory-grounded palettes: build a palette from a harmony rule around the user's brand colour and verify accessibility before committing to it."),
    ("Realtime Colors", "https://www.realtimecolors.com/", "colours",
     "Free", "reference",
     "Previews a palette and font pairing on a realistic live mock website in light and dark mode, so colours are judged in context rather than as flat swatches.",
     "Palette-in-context judging: preview the chosen palette on a real layout before building, and catch combinations that look fine as swatches but fail on a real page."),
    ("Lucide", "https://lucide.dev/", "icons",
     "Free, ISC", "reference",
     "Open-source icon library (1,900+ icons) of clean outline icons on a 24px grid with consistent stroke, with packages for React, Vue, Svelte and plain SVG.",
     "Default icon set for generated sites: use Lucide SVGs for all UI icons so the icon style stays consistent and licence-clean across the build."),
    ("Heroicons", "https://heroicons.com/", "icons",
     "Free, MIT", "reference",
     "Small, opinionated SVG icon set (316 icons) from the Tailwind CSS team in outline, solid, mini and micro styles, designed to pair with Tailwind UI.",
     "Tailwind builds' icon set: use Heroicons when the build is Tailwind-based and needs the matching Tailwind-team icon style in multiple sizes."),
    ("Phosphor Icons", "https://phosphoricons.com/", "icons",
     "Free, MIT", "reference",
     "Large open-source icon family (9,000+ icons) where every icon ships in six weights \u2014 thin, light, regular, bold, fill and duotone \u2014 as SVG and framework packages.",
     "Weight-flexible icons: pick Phosphor when one product needs light navigation icons and heavy emphasis icons without mixing icon sets."),
    ("Font Awesome", "https://fontawesome.com/", "icons",
     "Free tier (icons CC BY 4.0, fonts SIL OFL, code MIT); Pro from $99/year", "reference",
     "Best-known icon library on the web: free solid, regular and brand icons (2,000+ free) as SVG, webfont or kits, with a large Pro catalogue.",
     "Brand/social icons and general UI icons: use the free set when a build needs brand logos (social icons) that the outline-only sets do not include."),
    ("Unsplash", "https://unsplash.com/", "imagery",
     "Free, Unsplash License (no attribution required)", "reference",
     "High-quality editorial photography library (5M+ photos) from professional photographers, searchable by keyword and colour, downloadable at full resolution.",
     "Hero and section photography: source high-quality, licence-clean photos matching the brief instead of shipping placeholder or AI-looking images."),
    ("Pexels", "https://www.pexels.com/", "imagery",
     "Free, Pexels License", "reference",
     "Free stock photos and videos (owned by Canva), searchable by colour and orientation, downloadable and modifiable without attribution.",
     "Photos plus video: use Pexels when a build needs free video backgrounds or b-roll as well as stills, under one licence."),
    ("unDraw", "https://undraw.co/", "imagery",
     "Free, unDraw licence (no attribution required)", "reference",
     "Open-source flat illustrations of people and ideas in a consistent style, customisable by accent colour, downloadable as SVG/PNG for web use.",
     "Illustrations for SaaS/product pages: use unDraw illustrations (recoloured to the brand palette) for empty states, features and hero art instead of stock photos."),
    ("Pixabay", "https://pixabay.com/", "imagery",
     "Free, Pixabay Content License", "reference",
     "Free media library (5M+ items) covering photos, illustrations, vectors, videos and music under one search, with a permissive content licence.",
     "One-search media fallback: use Pixabay when Unsplash/Pexels lack a subject, or when a build needs vectors, video and audio from a single source."),
    ("CSS Gradient", "https://cssgradient.io/", "CSS generators",
     "Free", "reference",
     "Most popular standalone gradient builder: create linear, radial and conic gradients with precise colour stops, angles and opacity, and copy the CSS.",
     "Gradient code generation: build hero/background gradients visually and paste the exact CSS into the generated stylesheet instead of hand-writing gradient syntax."),
    ("Smooth Shadow", "https://shadows.brumm.af/", "CSS generators",
     "Free", "reference",
     "Layered box-shadow generator producing smooth, natural-looking shadows with quadratic falloff via a multi-layer visual interface.",
     "Depth and elevation: generate realistic layered shadows for cards, modals and floating elements instead of flat single box-shadows."),
    ("Clippy", "https://bennettfeely.com/clippy/", "CSS generators",
     "Free", "reference",
     "Visual clip-path editor by Bennett Feely: clip elements into polygons, circles, ellipses and custom shapes by dragging points, and copy the clip-path CSS.",
     "Custom shapes: generate clip-path polygons for angled sections, hexagons, arrows and speech bubbles without hand-computing coordinates."),
    ("Layoutit Grid", "https://grid.layoutit.com/", "CSS generators",
     "Free", "reference",
     "Drag-and-drop CSS Grid layout builder: define columns, rows, areas and gaps visually and get the HTML/CSS code for the layout.",
     "Complex grid layouts: build dashboard/card-grid layouts visually and paste the generated grid CSS into the build instead of trial-and-error grid code."),
    ("Responsinator", "https://www.responsinator.com/", "responsiveness",
     "Free", "reference",
     "Free one-minute responsive check: loads a live URL inside a row of phone/tablet frames (portrait and landscape) on one page, no account or install.",
     "Quick responsive sanity check: paste the built site's URL and eyeball all device frames at once for layout breaks before deeper testing."),
    ("BrowserStack", "https://www.browserstack.com/", "responsiveness",
     "Paid, free trial; Live from $29/month", "reference",
     "Real-device cloud: live interactive testing on 3,500+ real mobile/desktop/browser combinations, plus Percy visual testing and responsive checks on genuine hardware.",
     "Real-device proof: test the built site on real iOS Safari/Android devices the team does not own, to catch engine-specific bugs emulators miss."),
    ("LambdaTest", "https://www.lambdatest.com/", "responsiveness",
     "Paid, free trial", "reference",
     "Cross-browser testing cloud: test websites across 3,000+ browser/OS combinations, with live, screenshot and responsive testing on real devices.",
     "Cross-browser alternative to BrowserStack: run the built site across browser/OS combinations to verify rendering where the user's audience actually browses."),
)

# Live-checker wiring per tool name. "checker" is the checker name in
# gateway/web_designer/judgers/checkers.py, except "html_parse" and
# "contrast", which are implemented via judgers.common (parse_html and
# contrast_ratio). "requires" names a host binary; None means pure local.
_LIVE: dict[str, dict[str, Any]] = {
    "W3C Markup Validation Service": {"checker": "html_parse"},
    "W3C CSS Validation Service": {"checker": "css_validity"},
    "Dead Link Checker": {"checker": "link_check"},
    "Lighthouse": {"checker": "lighthouse", "requires": "lighthouse binary on PATH"},
    "axe DevTools": {"checker": "axe", "requires": "axe binary on PATH"},
    "WebAIM Contrast Checker": {"checker": "contrast"},
    "BackstopJS": {"checker": "visual_diff", "live_via": "local visual diff (PIL)"},
    "Google Fonts": {"checker": "font_sources"},
    "Fontshare": {"checker": "font_sources"},
}

# Reference entries with a genuine live stand-in already in the registry.
_LIVE_EQUIVALENT: dict[str, str] = {
    "PageSpeed Insights": "Lighthouse",
    "WAVE": "axe DevTools",
    "Responsinator": "responsive judger (1440/768/375 sweep)",
}

#: Binaries a live slot needs on the host. Pure-local slots need none.
_LIVE_BINARIES: dict[str, tuple[str, ...]] = {
    "Lighthouse": ("lighthouse",),
    "axe DevTools": ("axe",),
}

def _build_tools() -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for name, url, bucket, license_, kind, what, hermes_use in _TOOLS:
        entry: dict[str, Any] = {
            "name": name, "url": url, "bucket": bucket,
            "license": license_, "kind": kind, "what": what,
            "hermes_use": hermes_use,
        }
        live = _LIVE.get(name)
        if live is not None:
            entry["checker"] = live["checker"]
            entry["requires"] = live.get("requires")
            if "live_via" in live:
                entry["live_via"] = live["live_via"]
        equiv = _LIVE_EQUIVALENT.get(name)
        if equiv is not None:
            entry["live_equivalent"] = equiv
        out.append(entry)
    return tuple(out)


TOOLS: tuple[dict[str, Any], ...] = _build_tools()


def get_tool(name: str) -> dict[str, Any] | None:
    """One registry entry by name (case-insensitive), or None."""
    wanted = name.strip().lower()
    for tool in TOOLS:
        if tool["name"].lower() == wanted:
            return dict(tool)
    return None


def tools_by_bucket(bucket: str) -> list[dict[str, Any]]:
    """Registry entries in one bucket, in audit order."""
    return [dict(t) for t in TOOLS if t["bucket"] == bucket]


def live_checkers() -> list[dict[str, Any]]:
    """The live slots: local checks, no network, no API key.

    Contrast math (WebAIM Contrast Checker slot), HTML structure parse
    (W3C validator slot), CSS validity, link resolution, font sources and
    the local PIL visual diff always run locally. The axe and Lighthouse
    slots run only when their binary exists on the host — see
    :func:`live_checker_available`.
    """
    return [dict(t) for t in TOOLS if t["kind"] == "live"]


def checker_for(name: str) -> str | None:
    """Checker name for a live tool, or None for a reference entry.

    ``html_parse`` and ``contrast`` are implemented via ``judgers.common``
    (``parse_html`` / ``contrast_ratio``); the rest live in
    ``judgers.checkers`` under the same name (``check_css_validity``,
    ``check_links``, ``check_font_sources``, ``diff_screenshots``,
    ``run_lighthouse``, ``run_axe``).
    """
    tool = get_tool(name)
    if tool is None:
        return None
    return tool.get("checker")


def tools_with_checkers() -> list[dict[str, Any]]:
    """Live entries that have a checker, in audit order."""
    return [dict(t) for t in TOOLS
            if t["kind"] == "live" and t.get("checker")]


def _pil_available() -> bool:
    try:
        import PIL
    except ImportError:
        return False
    return True


def live_checker_available(name: str) -> bool:
    """True when a live slot can run on this host right now.

    Pure-local slots are always available, except the visual-diff slot,
    which needs PIL. The axe / Lighthouse slots need their binary on PATH
    (checked at call time with ``shutil.which``); without it the tool
    stays a reference entry and must not be claimed as run.
    """
    tool = get_tool(name)
    if tool is None or tool["kind"] != "live":
        return False
    if tool.get("checker") == "visual_diff" and not _pil_available():
        return False
    binaries = _LIVE_BINARIES.get(tool["name"])
    if binaries is None:
        return True  # pure local slot
    return any(shutil.which(binary) is not None for binary in binaries)


def available_live_checkers() -> list[dict[str, Any]]:
    """Live slots that can actually run on this host right now."""
    return [t for t in live_checkers() if live_checker_available(t["name"])]


def registry_summary() -> dict[str, Any]:
    """Counts: total / live / reference, and per-bucket live/reference."""
    by_bucket: dict[str, dict[str, int]] = {}
    for bucket in BUCKETS:
        entries = [t for t in TOOLS if t["bucket"] == bucket]
        by_bucket[bucket] = {
            "live": sum(1 for t in entries if t["kind"] == "live"),
            "reference": sum(1 for t in entries if t["kind"] == "reference"),
        }
    live = sum(1 for t in TOOLS if t["kind"] == "live")
    return {
        "total": len(TOOLS),
        "live": live,
        "reference": len(TOOLS) - live,
        "by_bucket": by_bucket,
    }


__all__ = [
    "BUCKETS", "TOOLS", "available_live_checkers", "checker_for",
    "get_tool", "live_checker_available", "live_checkers",
    "registry_summary", "tools_by_bucket", "tools_with_checkers",
]
