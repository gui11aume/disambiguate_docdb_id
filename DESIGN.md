# Design System — DOCDB Resolver

## Product Context
- **What this is:** A hosted web app where a user pastes text and gets informal
  patent citations replaced with canonical DOCDB IDs, via a real MCP client
  against the public `docdb.sarl-graip.fr/mcp` server plus a hosted LLM.
- **Who it's for:** Non-technical patent professionals (e.g. USPTO examiners,
  patent attorneys) who handle citations often and don't want to run an MCP
  client themselves.
- **Space/industry:** Patent/IP legal-tech, adjacent to patent search tools
  (PatSeer, AcclaimIP, IPRally) and legal SaaS.
- **Project type:** Single-page web utility — not a dashboard, not a
  marketing site.

## Aesthetic Direction
- **Direction:** Precision/Refined — reads like a well-typeset legal filing
  that happens to be running an LLM, not a startup SaaS dashboard.
- **Decoration level:** Minimal. No icons, no gradients, no shadow-cards. One
  hairline accent rule under the header is the only ornament — typography
  and whitespace carry the page.
- **Mood:** Serious, precise, trustworthy. A considered instrument for a
  professional, not a flashy tech product.
- **Reference sites:** General research into 2026 legal-tech design
  conventions (WebSearch); no live screenshots were captured (headless
  browser sandboxing unavailable in the build environment).

## Typography
- **Display/Hero:** IBM Plex Serif, weight 500 — sets the "law journal"
  register.
- **Body:** IBM Plex Sans, weight 400/500 — workhorse for forms and copy.
- **UI/Labels:** IBM Plex Sans, same as body.
- **Data (DOCDB IDs):** IBM Plex Mono, `font-variant-numeric: tabular-nums`
  — patent numbers get distinct, precise treatment; this doubles as the
  product's value made visible in the output.
- **Code:** IBM Plex Mono.
- **Loading:** Google Fonts —
  `family=IBM+Plex+Serif:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap`
- **Scale:**
  - Display / h1: 2.6rem (41.6px), line-height 1.15
  - h2 (screen headings): 1.5rem (24px)
  - Body: 1rem (16px), line-height 1.55
  - Small/labels: 0.9rem (14.4px)
  - Eyebrow/mono labels: 0.75rem (12px), letter-spacing 0.06–0.08em, uppercase

## Color
- **Approach:** Restrained — near-monochrome text/surfaces, a single accent.
- **Accent:** `#9C6B1F` (brass) — links, primary button, accent rule. Hover:
  `#7E561A`.
- **Secondary:** None, deliberately — a second brand color would dilute the
  "one considered accent" read.
- **Neutrals:** Paper `#FAF8F3` (background) → Paper Alt `#F3EFE6`
  (card/surface) → Border `#DCD5C7` → Muted `#6B6558` (secondary text) →
  Ink `#1C1B18` (primary text/headings).
- **Semantic:** success `#4B6F44` / tint `#E7EEE3`, warning `#B5541C` / tint
  `#F5E4D8`, error `#8C2F2F` / tint `#F2E1DF`, info `#4A5A6B` / tint
  `#E5E9EC`.
- **Dark mode:** Warm near-black surfaces, not blue-black — bg `#1A1815`,
  surface `#242019`, border `#38332A`, text `#F3EFE6`, muted `#A69C89`;
  accent brightened for contrast: `#C99A4A` (hover `#DCAF63`). Semantic
  colors lighten similarly (see the approved preview file for exact
  values). Respect `prefers-color-scheme` by default; an explicit toggle
  overrides it via `data-theme="dark"|"light"` on `<html>`.

## Spacing
- **Base unit:** 8px.
- **Density:** Comfortable — a page you read and trust, not a dashboard you
  scan fast.
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64)

## Layout
- **Approach:** Grid-disciplined, single column.
- **Max content width:** 780px (900px for the sticky header band).
- **Alignment:** Left-aligned content, not centered — centered-everything
  reads as a landing-page template; left-aligned reads as a document.
- **Border radius:** sm 3px (buttons, inputs, chips), md 5px (cards/panels).
  No large or pill radii — restraint extends to corners.

## Motion
- **Approach:** Minimal-functional only — nothing moves without a reason.
- **Easing:** enter(ease-out) exit(ease-in) move(ease-in-out)
- **Duration:** micro(50–100ms) short(150–250ms) medium(250–400ms)
- **Concretely:** a soft fade when the clean-text section swaps in after
  sign-in, a subtle pulse on the "Cleaning…" status text. No scroll-driven
  or decorative animation.

## Component Notes
- **DOCDB ID chips:** the agent does not replace the original citation — it
  appends the canonical ID in curly braces right after it (see
  `agent/default_prompt.py`, e.g. `US 8,000,000 (Greenberg) {US8000000B2}`),
  or `{not found}` when it can't resolve one. The `/clean` result view
  parses `{...}` and renders the captured content as an inline chip instead
  of literal braces: a resolved ID gets the mono, tabular, accent-tinted
  chip (`background: var(--accent-tint); border: 1px solid var(--accent)`);
  `not found` gets the same chip shape in the warning palette
  (`background: var(--warning-tint); border: 1px solid var(--warning)`)
  so an unresolved citation reads as "needs attention," not as a bug.
  Rendered via DOM text-node splitting, not `innerHTML`/string-regex-replace
  — the result text originates from user input transformed by an LLM, so it
  must never be interpreted as HTML.
- **Buttons:** primary (solid accent fill, paper text), secondary (border,
  ink text, accent border on hover), ghost (no border, muted text, ink on
  hover). No gradient buttons.

## Brand Mark
- **Favicon:** ink (`#1C1B18`) rounded square with a bold serif "D" in the
  dark-mode brass tint (`#C99A4A`, chosen over the light-mode `#9C6B1F` for
  legibility at 16px against the dark tile). Letterform set in Liberation
  Serif Bold (metric-compatible with Times New Roman) for the raster
  `favicon.ico`; the vector `favicon.svg` specifies a generic serif stack
  (`Georgia, 'Liberation Serif', 'Times New Roman', serif`) since favicons
  render without the page's webfont loading. Served at `/favicon.svg` and
  `/favicon.ico` (both proxied through nginx to the `web` service — see
  `deploy/nginx/default.conf`).

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-29 | Initial design system created | Created by `/design-consultation`. Memorable-thing brief from the user: "serious, precise, trustworthy tooling." Research (WebSearch) found 2026 legal-tech design converging on institutional weight/restraint, and that technical products (e.g. Stripe) build trust through typographic precision. Deliberate departures from category norms: IBM Plex superfamily instead of system-ui/Inter (nobody in patent-tech uses Plex; one family across serif/sans/mono reads as "engineered," not templated), and warm paper + brass accent instead of the white+blue every patent search tool (PatSeer, AcclaimIP, IPRally) defaults to. Approved via a self-contained HTML preview rendering the actual sign-in and clean-text screens (`gstack design`'s AI-mockup path was unavailable — no OpenAI key configured in this environment — so Path B, the HTML preview, was used instead of AI-generated mockups). User confirmed: "The design is good. I want to keep it." |
