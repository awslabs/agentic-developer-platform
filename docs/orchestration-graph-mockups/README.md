# Orchestration graph — UI mockups

Clickable HTML mockups for the orchestration graph UI of intent
[#4120](https://github.com/aws-e/adp/issues/4120) / EPIC
[#4191](https://github.com/aws-e/adp/issues/4191). Open
[`index.html`](index.html) to browse them.

These are static files with illustrative data. They are not production code and
nothing here is built or bundled.

## What was chosen

The human sponsor reviewed all five options and **chose two of them**. That
choice is recorded as ruling **D-R13** in
[`delivery-plan-amendment.md`](https://github.com/aws-e/adp/blob/agent/issue-4120/aidlc/spaces/issue-4120/inception/delivery-plan-amendment.md).

| File | Option | Status | Role |
|---|---|---|---|
| [`option-d-portfolio.html`](option-d-portfolio.html) | **D — Portfolio** | **chosen** | The **execution view**. How delivery in flight is read and operated: rollup, origin strip, engine visibility, the agreed-plan grid, per-EPIC journeys and story tables, and the run log drawer. |
| [`option-e-inception.html`](option-e-inception.html) | **E — Inception** | **chosen** | The **intake and review experience**. The front of the funnel: capturing an intent as a conversation with a live draft, then the per-stage review where decisions are made. |
| [`option-a-transit.html`](option-a-transit.html) | A — Transit | ingredient reference only | The loop as a metro line. Contributed the past/present/future-in-one-view idea. **Its transit metaphor is explicitly rejected** — see contract item 7. |
| [`option-b-mission-control.html`](option-b-mission-control.html) | B — Mission Control | ingredient reference only | Dark operator console. Contributed the inspector-drawer-with-transitions and per-node action pattern. |
| [`option-c-ledger.html`](option-c-ledger.html) | C — Ledger | ingredient reference only | The graph as a chronicle in plain sentences. Contributed the plain-language-throughout requirement and the future-in-future-tense framing. |
| [`index.html`](index.html) | — | index | Landing page linking all five. Its card borders already highlight D and E as the current direction. |

"Ingredient reference only" means: A, B and C may be cited to explain *why* a
requirement exists, and their ingredients survive inside D and E. They are
**not** implementation targets. Do not build A, B or C.

## The binding artifact is the contract, not these files

Implementing stories build against
**[`design-contract.md`](design-contract.md)**, not against a reading of the
HTML. The contract is normative; these files are the evidence it cites. Where
the contract and a mockup disagree, the contract wins — mockups carry
illustrative data and a few internal inconsistencies that the contract
deliberately resolves.

The contract is consumed by:

- [#4212](https://github.com/aws-e/adp/issues/4212) — graph view (wave 6)
- [#4213](https://github.com/aws-e/adp/issues/4213) — gate approval + resume controls (wave 6)
- [#4196](https://github.com/aws-e/adp/issues/4196) — orchestration graph store (wave 1), bound by the node taxonomy in contract item 8
- [#4208](https://github.com/aws-e/adp/issues/4208) — intent-intake chat (wave 5), bound by option E

## Do not regenerate these files

The sponsor reviewed **these specific bytes**. Regenerating them, "improving"
them, or substituting newly generated designs invalidates the review and is
forbidden by D-R13 and D-R19.

Related: ruling **D-R9** declares that shipping a **generic table-and-badges
UI** is a *failure outcome* for this EPIC, not a partial success. The mockups
and the contract exist precisely so that outcome is detectable at review time.

Landed on `main` in commit `238c49ae` (PR
[#4189](https://github.com/aws-e/adp/pull/4189)): six HTML files —
`index.html` plus options A–E.

## External references in these files (offline-review note)

The mockups are **self-contained for review purposes**: no CDN JavaScript, no
frameworks, no remote images, no analytics. All CSS and all JS is inline in each
file, and the only interactivity (option D's log drawer, option E's decision
cards) is vanilla DOM code with no dependencies.

`grep -c 'https://'` is non-zero on five of the six files. Every hit is one of
exactly two justified categories:

| Category | Count | Files | Why it does not break offline review |
|---|---|---|---|
| **Google Fonts** — one `preconnect` to `fonts.googleapis.com` plus one `css2?family=Hanken+Grotesk…&family=Spline+Sans+Mono…&display=swap` stylesheet link | 10 (2 per file) | A, B, C, D, E | Purely typographic. Every rule declares a local fallback (`system-ui, sans-serif` for body text, `ui-monospace, monospace` for the mono face), and `display=swap` means text paints immediately. Offline, the mockups render and remain fully interactive in system fonts — only the typeface differs. |
| **GitHub deep links** — the intent issue and four `aidlc/spaces/issue-4120/inception/*.md` stage documents | 6 | D only | These are the *content being demonstrated*: contract item 3 (deep links everywhere) and item 5 (stage documents as first-class linkable artifacts) cannot be shown without real link targets. Offline they simply do not resolve, exactly as any external link would. |

`index.html` has zero external URLs.

No hit is a script, style, or asset the layout depends on, so the offline-review
concern behind the check is satisfied. Any *future* edit that adds a remote
dependency the rendering depends on must be justified here or reverted.
