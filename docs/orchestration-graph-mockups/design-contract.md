# Orchestration graph — UI design contract

**Status:** normative. **Binding on:** [#4212](https://github.com/aws-e/adp/issues/4212)
(graph view), [#4213](https://github.com/aws-e/adp/issues/4213) (gate + resume
controls), [#4196](https://github.com/aws-e/adp/issues/4196) (store schema —
item 8 only), [#4208](https://github.com/aws-e/adp/issues/4208) (intent-intake
chat — items 2, 5, 7 and §E).
**Source ruling:** D-R13. **Chosen mockups:** [`option-d-portfolio.html`](option-d-portfolio.html)
(execution view) and [`option-e-inception.html`](option-e-inception.html)
(intake/review). **Extracted by:** [#4192](https://github.com/aws-e/adp/issues/4192).

## How to read this document

Each numbered section below is one of the eight requirements enumerated in
D-R13. Within each section:

- **MUST / MUST NOT / SHOULD** carry their RFC-2119 force. A **MUST** that ships
  unimplemented is a story defect, not a follow-up.
- **Traced to** names the mockup the requirement comes from, with the exact copy
  or token quoted. The quote is the evidence; it is not decoration. Where an
  implementer thinks the contract is ambiguous, the quoted mockup breaks the tie
  — not a fresh design judgement.
- **Why it is a requirement** states the failure this prevents. D-R9 declares a
  **generic table-and-badges UI a failure outcome** for this EPIC, so every
  requirement here is written to be *checkable*, not merely agreeable.

Two standing rules apply to everything below.

**Illustrative data is not contract.** The mockups show a fabricated
mid-wave-2 snapshot. Counts, dollar amounts, names (`Pranav`), dates
(`Wed 02 Sep 2026`), issue numbers and run ids are examples. What binds is
*structure, vocabulary, state semantics, information ordering, and controls*.

**Where the mockups disagree with themselves, this contract decides.** Three
internal inconsistencies in option D are resolved explicitly, in
[§9.3](#93-mockup-inconsistencies-resolved-by-this-contract). Do not
re-litigate them and do not copy the inconsistency.

---

## 1. One-shot rollup — all EPICs, one segmented bar

**Traced to** option D, section 2, eyebrow `Where things are — all EPICs, one
view`.

The top of the execution view MUST answer "where is everything" without
scrolling and without drilling in.

**1.1** A rollup band MUST show, as headline figures: **EPIC count**, **story
count**, and story counts **by state**. Mockup D's `.rollup-top` shows
`EPICs 2`, `Stories 12`, `Complete 4`, `In progress 2`,
`Waiting 5 (1 on you)`, `Stalled 1`, and `Cost to date $107.55`.

**1.2** The by-state breakdown MUST be rendered as **one segmented horizontal
bar**, not as N separate badges, chips, meters, or a table row. This is the
single most literal requirement in D-R13 ("as **one segmented bar**") and the
sharpest available discriminator against the declared table-and-badges failure
outcome. In mockup D it is `.allbar`, a `display:flex` strip of segments each
sized `flex:<count>` and labelled with its count.

**1.3** The bar MUST carry exactly these five user-facing states, with these
labels and glyphs. This vocabulary is closed: no sixth segment, no renaming.

| State | Label (verbatim) | Glyph | Fill | Text on fill |
|---|---|---|---|---|
| complete | `Complete` | `✓` | `#0ca30c` | `#fff` |
| in progress | `In progress` | `▶` | `#2a78d6` | `#fff` |
| waiting on a gate | `Waiting on a gate` | `🚦` | `#fab219` | `#5c4200` |
| stalled | `Stalled — needs help` | `⚠` | `#ec835a` | `#fff` |
| queued | `Queued (waiting on dependencies)` | `○` | `#c3c2b7` | `#52514e` |

Two of the five MUST use dark text on their fill (amber and grey), as shown —
white on `#fab219` or on `#c3c2b7` fails contrast.

**1.4** The bar MUST be exposed to assistive technology as a single labelled
image: `role="img"` plus an `aria-label` enumerating every state and count in
prose. Segment counts alone are not an accessible substitute for the whole. In
mockup D: `aria-label="12 stories: 4 complete, 2 in progress, 1 waiting on a
gate, 4 queued, 1 stalled"`.

**1.5** A legend MUST accompany the bar mapping every swatch to its full label.
Glyph-only or colour-only encoding is prohibited — colour MUST never be the sole
carrier of state anywhere in this UI. Note that mockup D's legend swatch
(`.kdot`) is a **rounded square**, `border-radius:3px`, deliberately not a dot,
so it is distinguishable from the engine's round health indicator.

**1.6** Where a waiting count includes items awaiting *this viewer*, that
sub-count MUST be surfaced inline, in second person. Mockup D:
`Waiting 5 (1 on you)`.

**1.7** Every monetary figure in the rollup MUST carry the cost-scope label
required by **AC-11** and item 7.4 below. A figure that silently excludes
build/CI/infra cost reads as a total and is a defect.

**Why it is a requirement.** The intent behind #4120 is "I lose the plot — I
can't tell what's running, what's stuck, or what it's costing." N badges make
the reader do arithmetic to recover the proportion; one bar *is* the
proportion. A conventional status table is the specific thing D-R9 names as
failure.

---

## 2. Origin strip — the flow ran once, outputs are each at their own stage

**Traced to** option D, section 1, eyebrow `How this work came to be — one flow,
run once`, and option D's header sub-line `one AIDLC flow, run once → 2 EPICs ·
12 stories, each at its own stage`.

**2.1** The view MUST open with a provenance strip reading left to right:
**intent → inception → fan-out to EPICs**, stages joined by an explicit
directional connector (mockup D uses a `→` glyph, `.arrow`). The strip states
*how this work came to be* before showing its current state.

**2.2** The strip MUST make it unmistakable that the AIDLC flow **ran once** and
that its outputs have since **diverged** — each produced EPIC is at its own
stage, independently. Mockup D renders the fan-out as a distinct visual branch
(`.fan`, set off by `border-left:2px solid var(--grid)`) whose key reads
`Produced — now each at its own stage`, with per-EPIC live state on each row:
`▶ building, wave 2 of 3` and `🚦 waiting on your design review`.

**2.3 Inception cost MUST attribute to the flow, never to any EPIC.** This is a
hard modelling constraint, not a layout preference. In mockup D the inception
node carries `5 stages · 4 approvals by Pranav · $31.50`, and that `$31.50` sits
*outside* both EPIC subtotals; the rollup total is inception plus per-EPIC
spend. Consequently:

- Inception spend MUST NOT be divided, apportioned, or amortised across EPICs.
- Per-EPIC totals MUST NOT include it.
- Summing EPIC totals therefore MUST NOT equal the flow total, and the UI MUST
  NOT present the two as if they should reconcile.

Option E states the same rule from the intake side: its only cost line is
`this stage: 48 min · $7.90 · flow so far: $28.30` — cost is attributed to the
**stage** and to the **flow**, and EPICs appear there only as an output count
(`2 EPICs, 12 stories`), never as cost owners.

**2.4** Each stage in the strip MUST carry its own state and attribution — who
acted and when. Mockup D: intent shows `filed by Pranav · 24 Aug`; inception
shows `✓ Completed 26 Aug`.

**Why it is a requirement.** Without it, readers assume one flow equals one
deliverable and read a lagging EPIC as a lagging *flow*. And inception cost
apportioned across EPICs would make every per-EPIC figure a fiction that cannot
be reconciled against `usage_logs`.

---

## 3. Deep links everywhere, and a log drawer for runs

**Traced to** option D, sections 5–6 (`.stories` tables, `td.links`) and its
`.drawer`.

### 3.1 Deep links

**3.1.1** Every entity rendered anywhere in this UI MUST be a real link to its
canonical artifact:

| Rendered entity | Links to |
|---|---|
| intent | its GitHub issue |
| EPIC | its GitHub issue |
| story (table row, plan chip, journey stage) | its GitHub issue |
| a story's PR, where one exists | that PR |
| stage document (item 5) | the document itself |
| bootstrap orchestrator (item 6) | that orchestrator issue |
| run | the log drawer (§3.2) |

**3.1.2** Links MUST be real anchors with real `href`s — navigable by
middle-click, "open in new tab", and browser back. Synthetic `onClick`
navigation is prohibited. This is also the load-bearing reason the
build-vs-adopt recommendation in §8 lands on *build*: canvas node
implementations cannot offer this.

**3.1.3** Links that leave the app MUST carry a visible affordance. Mockup D
uses a trailing `↗` inside the link text on every external link, with
`a.ext{color:#2a78d6; font-weight:600}` and underline on hover.

**3.1.4** Focus MUST be visible on every interactive element. Mockup D:
`outline:2px solid var(--blue); outline-offset:2px` on `:focus-visible`, applied
to both links and buttons.

**3.1.5** The view MUST be deep-linkable *into*, not merely *out of*. Mockup D
opens a specific run's drawer straight from the URL fragment
(`option-d-portfolio.html#s4207`). The shipped implementation MUST support the
equivalent via `react-router-dom` search params, following the established
precedent in `AgentActivity.tsx` (`?id=` auto-opens the detail view, issue
#3632). A shared link MUST reopen the same drawer.

### 3.2 The run log drawer

**3.2.1** A run's detail MUST open **in place**, as a right-hand drawer over the
graph — not a full-page navigation. The reader MUST NOT lose their position in
the graph to inspect a run. Mockup D: `.drawer` is `position:fixed`, pinned
right, `width:min(520px,94vw)`, over a `rgba(11,11,11,.42)` scrim.

**3.2.2** The drawer MUST contain, in this order:

1. **Header** — story/node identity, persona, state and duration
   (`Story #4206 · developer · complete`); a title; and a monospace meta line
   carrying run id(s), branch, and cost (`run_88ab02 + run_91cc07 ·
   agent/issue-4206 · $19.50 total`).
2. **Streaming log** — timestamped lines, monospace, on a dark surface
   (`#141413`), scrolled to the newest line, in an `aria-live="polite"` region.
   Warning and success lines MUST be visually distinguished (mockup D: `#ec835a`
   and `#3fbf3f`) **and** carry a glyph (`⚠`, `✓`) so the distinction is not
   colour-only. While a run is live the log MUST be labelled as such — mockup D
   appends `· streaming` to the section heading.
3. **Engine transition record** — the same node's state history as plain
   sentences with a fixed timestamp gutter, e.g. `11:01 Started by the engine as
   wave 2 opened` / `12:22 Check failed → defect cycle 1 of 3` / `13:04 Complete
   — all checks passed; no human vouching needed`. This is the engine's own
   account of the node, distinct from the agent's log, and MUST be a **separate
   block** — never interleaved into the log stream. It MUST render the
   nine-state vocabulary of #4193 in prose, never as raw literals (item 7.5).
4. **Actions footer** — see §3.2.3.

**3.2.3** Recovery controls MUST appear **in the drawer**, on the run they act
on. A reader MUST NOT have to leave for another surface to resume or abort.

- Actions MUST be **state-dependent**. In mockup D only the *stalled* run offers
  `Resume from checkpoint (safe to repeat)` (the single primary/dark button) and
  `Abort run`; the complete and in-progress runs offer neither.
- The resume control MUST state its idempotency in its own label, as shown.
- Every drawer MUST offer `Open issue ↗`, `Open PR ↗` where a PR exists, and
  `Open in Agent Activity ↗`.
- Controls MUST be **hidden**, not merely disabled, when the viewer lacks the
  permission (#4213). A disabled control the viewer can never enable is noise.
- Per **R-O3f**, per-run *pause* is out of v1 scope and ships as a **declared
  seam with an explicit not-implemented response**. It therefore MUST NOT appear
  in the drawer at all: *"a half-built control that appears in the UI but does
  nothing is worse than its absence."*

**3.2.4** The drawer MUST behave as a modal dialog: `role="dialog"`,
`aria-modal="true"`, an accessible label, close on `Escape`, close on scrim
click, and a visible close button. It MUST additionally trap focus and restore
focus to the invoking element on close — mockup D omits both, and the shipped
implementation MUST NOT copy that omission.

**3.2.5** All motion — drawer transform, scrim fade, the engine's pulse, and log
streaming itself — MUST be suppressed under
`@media (prefers-reduced-motion: reduce)`. Mockup D does this in CSS *and* in
JS (it does not start the stream interval when the query matches).

### 3.3 Reuse — what exists and what does not

This is the reuse table D-R13 requires for the drawer. It is deliberately
honest: the Agent Activity **presentation** is the model to follow, but its
streaming log **does not exist yet**.

| Need | Existing component to follow | Status |
|---|---|---|
| Run-detail field layout, ordering, label/value pairs | `modules/gateway/frontend/src/components/InvocationDetail.tsx` | **Reuse the presentation.** Note it is a `<Modal size="lg">`, not a drawer. Follow its `<dl>`/`DetailRow` field ordering and its error-first reordering for failed runs; re-shell it as a drawer. |
| Status glyph + colour mapping | `InvocationDetail.tsx` / `pages/AgentActivity.tsx` | **Follow, do not copy.** Both files re-declare a private `StatusBadge` and a 9-status glyph map. Do not add a third copy; and per **AC-23** the new map MUST NOT contain `rejected` or `skipped`. |
| Log/transcript rendering | `components/TranscriptViewer.tsx` (`TranscriptContent`) | **Partial reuse.** Fetches a complete immutable markdown transcript once and renders it. It is not incremental and does not tail. |
| **Streaming log** | — | **Net-new.** No streaming log viewer exists. `run_log_url` is rendered as a plain external `<a>` (`InvocationDetail.tsx`); `pages/LogViewer.tsx` is an explicit stub. There is no `EventSource`/SSE consumer anywhere in the frontend. |
| Liveness / freshness captions | `components/activity/LivenessBadge.tsx`, `components/LastUpdated.tsx`, `utils/liveness.ts` | Reuse directly. |
| Drawer/modal shell, buttons, badges, tables, cards | `components/ui/` (barrel `@/components/ui`) — `Modal`, `ModalFooter`, `Button`, `Badge`, `Table`, `Card`, `Spinner`, `Tabs`, `Alert`, `CopyButton` | Reuse. Do not introduce a component library. |
| Loading states | `components/LoadingScreen.tsx` — `TableSkeleton`, `CardSkeleton` | Reuse (`AgentActivity` uses `TableSkeleton rows={10}`). |

**3.3.1 Live-ness is polling, not push.** Per **R-O1b** no WebSocket or SSE
surface is introduced for this UI. Freshness MUST come from react-query
`refetchInterval`, set **explicitly** on the query, following
`AgentActivity.tsx` (`refetchInterval: 30_000`, `refetchOnWindowFocus:
'always'`, `placeholderData: keepPreviousData`). Two binding negatives:

- MUST NOT use `hooks/usePollingStatus.ts` — it is asset-coupled (imports
  `getAssetStatus`, typed to `AssetStatusResponse`), uses raw `setInterval`, and
  computes its interval once so it is not adaptive.
- MUST NOT rely on the global `staleTime: 5 * 60 * 1000` in `main.tsx`. That
  omission is exactly why `InvocationChain` is static for five minutes inside a
  parent that polls at 30 s. **AC-2** requires a state change to appear within
  one polling interval with no manual refresh.

**Why it is a requirement.** The second half of the intent is "restarting a
stalled loop is archaeology." Archaeology is what happens when the log lives on
another surface, the engine's account of the node lives nowhere, and the resume
button is a third place. Item 3 collapses all three onto the run.

---

## 4. "The agreed plan" card — EPICs × waves, coloured by live status

**Traced to** option D, section 4, eyebrow `The agreed plan — how this gets
built, in one view`; heading `Loop proposal, as approved`.

**4.1** The approved plan MUST be rendered as a **grid: EPICs as rows, waves as
columns**, each cell holding the story chips planned for that EPIC in that wave.

**4.2 The approved proposal and plan-vs-actual MUST be one view, not two.** The
grid *is* the plan of record, and its chips are coloured by **live status**.
There MUST NOT be a separate "planned" view alongside an "actual" view, and no
diff screen. Mockup D states the rule in the card itself:
`Colors show today's progress against this plan`.

**4.3** Chip status MUST use the same five-state vocabulary as item 1.3, with
the same colours, and MUST encode state **redundantly** — a 4px coloured left
border **plus** a short uppercase status tag (`✓ done`, `▶ 38m`, `⚠ stalled`,
`🚦 on you`, `○ queued`), plus a tinted background for the two attention states
(`#fdf6f2` stalled, `#fffcf3` gate). Never colour alone.

**4.4** Each chip MUST show the story's issue number and a short human title
(`#4206 · run-ledger schema`) and MUST link to that issue.

**4.5** Wave **gates** MUST be rendered as their own interstitial column between
wave columns — a gate is a first-class node (item 8), not a chip inside a wave.
Each gate MUST show its state and *whose* it is: mockup D shows `🚦 approved
30 Aug` for a passed gate and `🚦 yours after checks` / `🚦 your review` for a
pending one, second person, warn-coloured.

**4.6** Planned-but-not-yet-run work MUST be visible as such. Mockup D draws
every cell with `border:1px dashed` and renders unrun stories as `○ queued`
chips. **AC-1** requires the view to render `pending` nodes never yet executed;
this grid is where the look-ahead lives.

**4.7** The card MUST carry **gate attribution for the plan itself** —
who approved it, when, and a link to the proposal. Mockup D:
`approved by Pranav · 26 Aug 10:12 · view proposal ↗`.

**4.8** The card MUST state that the plan is the **plan of record** and that
**off-plan work is visible as such**. Mockup D's `.plan-note`, which the
implementation SHOULD render in substance:

> This map is the plan of record — the engine dispatches, watches, and gates
> each wave itself; **waves have no orchestrator issues**. Waves that ran before
> the engine existed link their temporary bootstrap orchestrator for the record.
> Hand-edits to issues don't change this map; a story running outside it shows up
> flagged as off-plan.

That last clause is the UI surface of the deviation detection in
[#4204](https://github.com/aws-e/adp/issues/4204) and of the honest v1 guarantee
in **D-R8**: *an agent cannot fake having been approved, and off-plan activity is
visible on the graph.* An off-plan story MUST therefore be renderable — flagged,
not hidden, and not silently absent because the plan has no cell for it.

**4.9 Accessibility.** The grid MUST expose row and column semantics, so a
screen reader announces the EPIC and the wave along with the chip — a `<table>`
with `<th scope>`, or `role="grid"` with `aria-rowindex`/`aria-colindex`.
Absolutely-positioned nodes on a transformed canvas cannot express this; see §8.
CSS Grid over table semantics is the intended shape (mockup D:
`grid-template-columns:150px 1fr 34px 1fr 34px 1fr`), and the grid MUST remain
horizontally scrollable rather than reflow into illegibility on narrow
viewports.

**Why it is a requirement.** Two views — the approved plan and today's reality —
guarantee they drift and force the reader to diff them by eye. One view makes
"we are behind on wave 2 of EPIC 4121" a glance instead of an investigation.

---

## 5. Stage documents as first-class linkable artifacts with gate attribution

**Traced to** option D's `.docsrow`, key `Documents, by stage`; option E's stage
rail.

**5.1** The five AIDLC stage documents MUST be surfaced as first-class,
individually linkable artifacts. They are the record of how the plan was
decided — not an appendix, and not something reachable only by knowing a branch
path.

**5.2** Their names are **fixed by this contract**, in this order. Note the
naming is deliberately plain: the survey document is `Code survey`, not
`reverse-engineering`, even though the underlying file is
`inception/reverse-engineering.md`. Display names MUST NOT leak file names.

| # | Display name | Underlying artifact |
|---|---|---|
| 1 | `Problem frame` | `inception/problem-frame.md` |
| 2 | `Code survey` | `inception/reverse-engineering.md` |
| 3 | `Requirements` | `inception/requirements.md` |
| 4 | `Delivery plan` | `inception/delivery-plan.md` |
| 5 | `Loop proposal` (option E's rail calls the stage `Proposal`) | the compiled loop proposal |

**5.3** Each document MUST carry **gate attribution**: who approved it and when,
in the form `approved by <person> · <date>`, optionally with time
(`approved by Pranav · 24 Aug 20:24`). An unapproved or pending document MUST be
visually distinct from an approved one — mockup D reserves `.doc.pending`
(dashed border, reduced opacity) for exactly this, and the shipped
implementation MUST use it rather than rendering pending as approved.

**5.4** Documents MUST be presented as a compact row of card links with a
document glyph, not as a bare bullet list of URLs.

**5.5** In the intake/review experience (option E) the same five documents MUST
appear as a **horizontal stage rail** showing overall position: completed stages
solid with `✓`, the current stage highlighted, later stages dashed and muted.
Each completed stage carries a `read ↗` link plus `you approved`; the current
stage says whose it is (`your review`); the final stage says what it does
(`creates the work`).

**5.6** In the review experience each stage MUST present a **summary with a link
to the full document**, and MUST label the relationship honestly. Option E:
`Read the full delivery plan ↗ · 15 min read · everything above is the summary
of it`. Reading time SHOULD be shown; the summary MUST NOT be presented as the
document.

**5.7** Decisions belonging to a gate MUST be rendered as **pickable option
cards, not comment threads** — the review's central move. Per option E each
decision card carries the question, the context, two or more options each with a
label and a consequence, a recommendation badge (`agent recommends`) on the
suggested option, and an escape hatch (`or write your own ruling…`). Options MUST
be keyboard-operable radios (`role="radio"`, `aria-checked`, `Enter`/`Space`),
and the group MUST be wrapped in `role="radiogroup"` — option E omits the
wrapper and the implementation MUST NOT copy that omission.

**5.8** Gate approval MUST record the decisions **in the same act** as the
approval. Option E bakes the count into the button — `Approve with these 2
decisions` — and states the consequence beneath it:

> Approving creates the work items (2 EPICs, 12 stories) as a proposal — nothing
> is dispatched until you approve the final proposal at stage 5. Your decisions
> are recorded with your name and become binding for every later stage.

A "send back with notes" path MUST exist alongside approval. Per **AC-5** the
approval MUST write a decision record carrying actor **role** and actor **kind**.

**5.9** A stage-history log MUST show the back-and-forth in plain sentences with
actor and timestamp: `yesterday 09:31 · you · approved requirements with 4
decisions`, `Sun 20:24 · you · approved the problem frame with 3 rulings`. Gates
MUST report the *number of decisions* attributed to them.

**Why it is a requirement.** Gate attribution is what makes the graph auditable:
every promotion traces to a named human act. The `decided_by` column in the
existing schema is a bare string mixing real user ids with
`"system:org-member-match"`; item 5 is the UI half of fixing that, and the store
half is `orchestration_decisions`.

---

## 6. Engine visibility — orchestration is built in, not an issue anyone runs

**Traced to** option D, section 3, eyebrow `The engine — orchestration is built
into the system, not an issue anyone runs`.

**6.1** The engine MUST be a **visible, first-class object in the UI** with its
own section. This is the UI expression of ruling **D-R10**: the deterministic
engine replaces the orchestrator-persona pattern; no agent persona drives the
loop. If the engine is invisible, readers will keep looking for the issue that
drives the loop.

**6.2** The engine section MUST show all four of:

| Element | Requirement | Mockup D |
|---|---|---|
| **Health + last tick** | Health state, **recency** of the last tick, and the **cadence** — both, not one | `Engine healthy` / `last tick 12 s ago · runs every 60 s` |
| **Watch list** | What it is watching right now, in one plain sentence, counted | `2 live runs (1 healthy · 1 stalled — alert sent), 1 automatic check queued, 1 review waiting on you` |
| **Next planned actions** | An **ordered** list of what it will do next, **each with its reason or trigger condition** | see 6.3 |
| **Plan reference** | That its next actions come from the agreed plan | key `What it does next, per the agreed plan` |

**6.3 Next actions MUST state reasons, not just intentions.** Every entry MUST
carry the condition that will fire it. From mockup D:

1. `Start the wave-2 checks the moment #4207 and #4208 both land`
2. `When checks pass, open your W2→W3 approval — nothing proceeds without it`
3. `Keep escalating #4207's stall until resumed, aborted, or recovered`

A bare list (`start checks`, `open approval`) does not satisfy this. Entries
that block on the viewer MUST be highlighted and phrased in second person
(mockup D: warn-coloured `.you` span, `your W2→W3 approval`).

**6.4** The engine surface MUST be **queryable**, i.e. backed by real state read
from the store, not a static banner and not a value computed only in the
frontend. It is the answer to "is the loop still running" and MUST be trustworthy
when the tick has stopped: a stale tick MUST read as *stale*, not as healthy.

**6.5** The health indicator MUST NOT rely on animation to convey state. Mockup
D's pulse (`.dotc`, `@keyframes eblink`) is decorative and is suppressed under
reduced-motion; the *text* carries the state.

**6.6 Bootstrap scaffolding MUST be labelled as scaffolding.** Waves that ran
before the engine existed were driven by a temporary orchestrator issue. Those
waves MUST link that issue for the record **and** mark it as scaffolding, so no
later reader or agent cites it as the intended architecture (**D-C2**). Mockup D
does this with a de-emphasised dashed pill, visually subordinate to the story
chips:

> `ran via bootstrap orchestrator #4217 ↗` — with the hover title *"This wave ran
> before the engine existed, so a temporary orchestrator issue drove it — kept
> for the record"*

Post-bootstrap waves MUST NOT display an orchestrator, because they have none:
`waves have no orchestrator issues`.

**6.7** No surface in this UI may present an agent persona as the loop driver
(**D-C1**, **AC-26**). The engine dispatches, watches, and gates. Personas are
workers at nodes. At most, an engine-summoned exception-diagnoser
([#4214](https://github.com/aws-e/adp/issues/4214)) may **propose, never
dispose** — and if its advisory output is rendered, it MUST be visibly advisory
and MUST NOT be presented as a state change.

**Why it is a requirement.** This EPIC's whole purpose is that orchestration
becomes a built-in engine rather than an issue someone runs. A UI that shows
nodes but not the thing moving them leaves the reader unable to distinguish "the
plan is waiting on a dependency" from "nothing is running at all" — which is
precisely the invisible-stall class the EPIC exists to eliminate.

---

## 7. Plain language throughout

**Traced to** option C's central idea, carried into D and E; enforced by
**R-O2a–d**.

**Requiring ADP-internals knowledge to read the graph is a defect, not a docs
gap.** A story MUST NOT be closed by adding a glossary.

**7.1 The transit metaphor is rejected.** No stations, lines, departures boards,
interchanges, platforms, or track. Option A is an ingredient reference only.
Mockup D's own note records the provenance: *"A's view without the transit
metaphor."*

**7.2 Prohibited vocabulary.** These MUST NOT appear in user-facing copy, labels,
headings, tooltips, or empty states:

- Graph internals: `node`, `vertex`, `edge`, `DAG`, `graph address`, `traversal`,
  `topological`. Mockup D uses `node` **only** as an internal CSS class name and
  never in visible copy. (`graph` is acceptable in the feature's own name; the
  internals are not.)
- Machine states as literals: `awaiting_gate`, `rejected_at_gate`, `superseded`,
  `no_op`, `webhook_received`, or any snake-case state string. See 7.5.
- Process jargon: `AIDLC` (option E contains it nowhere), `unit`, `emission`,
  `intake`, `artifact`, `orchestrator` **except** in the scaffolding label of 6.6.
- Infrastructure: SQS, KEDA, EventBridge, Lambda, alembic, correlation id,
  invocation id, ARN, `event_id`, `agent_run_id`.

**7.3 Required plain-language substitutions.** Where a concept must be shown, use
the mockups' words:

| Concept | Say | Not |
|---|---|---|
| `awaiting_gate` | `Waiting on a gate` / `waiting on your design review` | the literal |
| eval node | `check` / `wave-2 checks` / `1 automatic check queued` | `evaluation`, `eval` |
| bounded retry | `defect cycle 1 of 3` | `retry budget exhausted` |
| `halted` | the bound that was hit **and** what a human can do | `halted` |
| `superseded` | plan changed — this was replaced | the literal |
| stalled run | `Stalled — needs help` / `no activity for 15 min` | `heartbeat timeout` |
| queued story | `Queued (waiting on dependencies)` | `pending` |

**7.4 Every node MUST answer four questions without drilling into logs**
(R-O2a): **what it is**, **its state**, **why it is in that state**, and **its
cost with a scope label**. Mockup D's story rows carry a `why` line beside the
state for exactly this. Two specific cases:

- A **queued/pending** node MUST state *what specifically* would make it ready —
  which predecessor or gate blocks it (**AC-4**), not merely that it is blocked.
- A **halted** node MUST state the bound that was hit and the human action
  available (R-O2c).

**7.5 No raw status literal from the DynamoDB run-status vocabulary may be
displayed** (R-O2d). The engine's nine states from
[#4193](https://github.com/aws-e/adp/issues/4193) — `pending`, `ready`,
`running`, `awaiting_gate`, `passed`, `rejected_at_gate`, `failed`, `halted`,
`superseded` — are internal. The UI MUST map them to the item-1.3 vocabulary and
to the prose of 7.3. Per **AC-23** and the wave-6 evaluation, **no glyph or
label map in the frontend may contain `rejected` or `skipped`** — they are not
in the vocabulary; `rejected` is a frontend-only phantom with no backend writer.

**7.6 Cost is three-valued and MUST NOT be flattened.** Per **R-N5a** and
**AC-22**: `known` (with amount), `none_incurred` (verified zero), and `unknown`
(no row, or not costable). A node with no cost row MUST render as **unknown,
never `$0.00`** — mockup D shows `⌀ unknown` / `—` in muted grey and its closing
note states the rule. `unknown` MUST surface its reason (R-N5b: a non-gateway
Bedrock path, or chat logging disabled). Any aggregate containing an `unknown`
node, or reaching past the retention horizon, MUST be labelled **partial**
(**AC-21**). Every figure MUST carry the scope label *"agent run costs
only; excludes build/infra"* (**AC-11**, **R-N5c**) — a figure that silently
excludes non-run cost reads as a total.

**7.7** Copy MUST address the reader in **second person** and the agent in
**first person**, as both mockups do throughout: `1 on you`,
`waiting on your design review`, `nothing proceeds without it`,
`2 decisions only you can make`, `I'd cut this first under pressure`.

**7.8** Empty, loading, and error states are in scope for plain language. "No
data" MUST say what would put data there.

**Why it is a requirement.** The sponsor is the primary reader and does not hold
ADP internals in their head. Every leaked literal transfers work from the
implementer to every future reader, forever.

---

## 8. Node taxonomy — binding on the store schema

**Traced to** D-R13 item 8. This section binds
[#4196](https://github.com/aws-e/adp/issues/4196) (store schema) as well as the
dashboard stories, and it is the only item here with force over the database.

**8.1 Executable nodes are exactly three kinds: `story`, `eval`, `gate`.** These
are the only things that get a row, a state, and a transition. The set is closed;
adding a fourth executable kind is a schema change requiring its own ruling.

**8.2 Containers are `wave`, `EPIC`, `flow`. Container state is derived, never
stored.** Containers MUST NOT be rows in the node table, and MUST NOT be
rendered as executable nodes. A wave's state is a function of the nodes it
contains; an EPIC's of its waves; a flow's of its EPICs. Two consequences:

- No container may be transitioned, dispatched, resumed, or gated directly.
- A container's displayed state MUST be recomputed from its children, never
  cached as authoritative. Item 1's rollup and item 4's grid are both **derived**
  views by construction.

**8.3 The graph address is `flow/epic/wave/node`.** Four components, that order.
It is the join key for cost attribution (the `usage_logs` graph-address column)
and the stable identity for a position in the plan. Per item 7.2 the address is
**internal** — it MUST NOT be displayed as a path string in user-facing copy.
Mockup D shows this discipline: "graph address" appears only inside a *story
title* naming the concept, never as a rendered address.

**8.4 The story is the graph floor. Runs attach via the ledger, not as nodes.**
The lowest executable unit is a story. Individual agent runs are **not** graph
vertices:

- Runs MUST NOT appear in the node table, get a graph address of their own, or
  be transitioned by the engine's state machine.
- Runs MUST be rendered as **run detail beneath a story** — the drawer of item 3
  — never as vertices in the plan grid, the journey line, or the rollup.
- A story MAY own **several** runs. Mockup D shows this directly:
  `run_88ab02 + run_91cc07` for one story, after a defect cycle. The UI MUST
  handle N runs per story without inventing N nodes.
- Because runs are ledger rows, per-story cost is an **aggregate over runs**,
  which is why cost is three-valued (7.6): a story with no run rows is `unknown`,
  not zero.

**8.5** The five states of item 1.3 are the **user-facing projection** of the
nine engine states. Both layers exist; the mapping is declared once, in the
frontend, over the vocabulary owned by #4193. The UI MUST NOT define a second
state vocabulary of its own, and MUST NOT extend any existing run-status literal
set (per #4193: *"Do NOT extend any existing run-status literal set."*).

**Why it is a requirement.** Left open, an implementer would model runs as nodes
— it is the intuitive move — and the graph would then grow a vertex for every
retry, making the plan unreadable and cost double-counted. Fixing that after
migration `026` ships is a migration, not a refactor. Hence: binding on the
schema, decided here, before the store lands.

---

## 9. Build vs adopt — how the graph gets rendered

### 9.1 Recommendation: **BUILD.** Add no graph library.

**Decision: hand-roll the rendering with CSS Grid, Flexbox, semantic HTML and
Tailwind 4, adding zero new runtime dependencies.**

Verified baseline as of 2026-08-27, from
`modules/gateway/frontend/package.json` **and** `package-lock.json`: React
19.1.0, TypeScript 5.7.3, Vite 6.4.3, Tailwind CSS 4.0.0,
`@tanstack/react-query` 5.62.0, `react-router-dom` ^7.18.2, recharts 2.15.0,
vitest ^4.1.10. **No graph, DAG, flow, or diagram library is present** — no
`@xyflow/react`, `reactflow`, `dagre`, `elkjs`, `cytoscape`, `mermaid`,
`vis-network`, and no direct `d3`. The only d3 in the tree is recharts'
transitive `victory-vendor` copy (`d3-shape`, `d3-scale`, `d3-array`, …). So this
is a genuine decision, correctly flagged by D-R13.

**The decisive fact is topology.** Everything the chosen mockups require has a
**fixed, known shape**; nothing needs computed layout:

| Element | What it actually is |
|---|---|
| Rollup segmented bar (item 1) | flex row, `flex:<count>` per segment, `role="img"` |
| Origin strip (item 2) | flex row of 3 stages + `aria-hidden` arrow glyphs |
| **Agreed-plan grid (item 4)** | **CSS Grid; semantically a table / `role="grid"`** |
| Stage rail (item 5) | flex row of 5 cells + absolutely-positioned connector bars |
| Story tables, journey lines | `<table>`, flex |
| Log drawer (item 3) | fixed-position `aside` + scrim + focus trap |

Every candidate library exists primarily to solve **auto-layout** and/or an
**infinite pan/zoom canvas**. Neither is needed. Adopting one would mean paying
its full cost and then switching off most of what was paid for.

### 9.2 Why not adopt — the three requirements that decide it

1. **Accessibility (item 4.9).** The EPIC×wave matrix needs *row and column*
   semantics so a screen reader announces "EPIC 4121, wave 2, stalled". Canvas
   libraries ship a canvas a11y model — `aria-roledescription` plus live-region
   announcements over absolutely-positioned divs on a CSS-transformed plane.
   That cannot express `<th scope>` / `aria-rowindex`, and faking it means
   fighting the library.
2. **Deep links (item 3.1.2).** `react-router-dom` ^7.18.2 is already present.
   Hand-rolled cells are real `<a href>`s: middle-clickable, back-button-safe,
   refresh-safe. Canvas nodes force synthetic click handlers plus imperative
   viewport restoration on load.
3. **Tailwind 4 consistency.** Hand-rolled is one styling system. Adopting means
   importing a second stylesheet and overriding its class/CSS-variable
   conventions — precisely the seam where light/dark drift appears.

**Precedent, in this codebase:**
`modules/gateway/frontend/src/components/InvocationChain.tsx` already hand-rolls
a recursive indented tree in ~262 lines, including `aria-hidden` connector
glyphs and depth-based indentation, with a test beside it. The mockup elements
above are *structurally simpler* than that recursive tree — a data-driven grid is
less work than arbitrary-depth recursion.

**The honest counter-argument.** If a free-form node editor — user-dragged nodes,
arbitrary edges, auto-layout — were on the roadmap within roughly two quarters,
buying `@xyflow/react` now and eating the mismatch would be right, because
retrofitting a canvas later means rewriting all six elements. On the chosen
mockups that is not the case: D and E contain no pan/zoom canvas and no
user-positioned nodes. The escape hatch below is cheap precisely because layout
math is separable from rendering.

### 9.3 Escape-hatch pins (not to be installed now)

These are recorded so a later story has a decided answer rather than a fresh
search. All were verified against the npm registry on **2026-08-27**; sizes were
measured from the published tarballs (`gzip -9`). Use **exact pins, no `^`**,
matching the existing convention in `package.json` (`react`, `tailwindcss`,
`recharts`, `typescript` are all exactly pinned).

| Trigger | Pin | Verified facts |
|---|---|---|
| Topology stops being a regular grid and needs **data-driven layout** | **`"@dagrejs/dagre": "3.1.1"`** | Published 2026-08-08. MIT. Sole dep `@dagrejs/graphlib@4.0.5`; **no lodash**. 16,897 B gzip measured. Computes **positions only** — drops into hand-rolled rendering. ⚠️ Never pin bare `dagre`: `dagre@0.8.5` has not published since **2019-12-03** and upstream's own README says only the DagreJs-org fork is maintained. |
| Tree/partition layout **math only**, no canvas | **`"d3-hierarchy": "3.1.2"`** | ISC, zero runtime deps, ESM, `sideEffects:false`, 10,142 B gzip measured. Its 2022 publish date means feature-complete, not abandoned (repo active). Not currently in the tree, unlike `d3-shape`. |
| The UI genuinely becomes a **free-form node-editor canvas** | **`"@xyflow/react": "12.11.5"`** | Published 2026-08-25 (two days before this contract). MIT. Peer `react >=17`; **verified to resolve clean against this exact `package.json`** — 0 vulnerabilities, no peer warnings, `react@19.1.0` intact. 51,810 B gzip for the React package alone, plus `style.css`, plus the separate `@xyflow/system` runtime dep (which pulls `d3-drag`/`d3-zoom`/`d3-selection`/`d3-interpolate`) — realistic all-in cost is meaningfully above 52 KB. Core is fully functional under MIT; what is paywalled is example/template source and the remove-attribution entitlement (`proOptions={{hideAttribution:true}}` works but logs a dev-console notice). |

Because `@dagrejs/dagre` and `d3-hierarchy` compute **positions only** and do
not dictate the DOM, adopting either later does **not** invalidate the build
decision. That asymmetry is the core reason to build now: the layout-engine
escape hatch stays open, whereas a canvas-library decision is expensive to
reverse.

**Explicitly rejected for this UI:**

- **`elkjs` 0.12.0** (2026-07-17) — licensed **`EPL-2.0 OR GPL-3.0-or-later`**,
  not MIT, so it carries a legal-review step the MIT candidates do not. And it is
  genuinely large: `elk-worker.js` measures **732,332 B gzip**;
  `elk.bundled.js` 466,978 B gzip. Layout engine only, no rendering.
- **`cytoscape` 3.34.2** (2026-08-25, MIT, well maintained) — 254,206 B gzip, and
  its React wrapper `react-cytoscapejs@2.0.0` last published **2022-09-02**,
  still ships `prop-types`, with no React-19-aware release. Integration would be
  hand-rolled imperative refs anyway.
- **`mermaid` 11.17.2** (2026-08-25, MIT, very active) — wrong tool. 1,418,774 B
  gzip for `dist/mermaid.js`; dependency surface includes `d3`, `cytoscape`,
  `katex`, `dompurify`, `roughjs`, `dagre-d3-es`. It compiles text to **opaque
  SVG** with no React component identity, so deep links (item 3), drawer wiring,
  focus management and Tailwind theming all become DOM-scraping hacks.

**Charting** stays on the already-present **`recharts` 2.15.0** (sole current
consumer `components/org/UsageChart.tsx`). Do not add a second charting library.

**Stated uncertainty.** React Flow Pro *pricing tiers* could not be verified —
`reactflow.dev/pro` is client-rendered and returned no body content. The MIT
licence of the core package and the mechanics of `hideAttribution` were verified
directly from the published tarball and changelog. This does not affect the
recommendation, which is not to adopt it.

### 9.4 Mockup inconsistencies resolved by this contract

The mockups are illustrative and contain three internal inconsistencies. The
resolutions below bind; do not reproduce the inconsistency.

1. **Rollup `Waiting 5` vs the bar's gate segment `1`.** The headline aggregates
   gate-waiting **and** queued; the bar splits them. **Resolution:** the bar's
   five segments are canonical and MUST be disjoint. A headline "waiting" figure
   MAY aggregate, but MUST make the aggregation legible (mockup D's
   `(1 on you)` is the mechanism).
2. **The bar's `aria-label` order differs from its DOM order** (label says
   "…4 queued, 1 stalled"; DOM renders stalled before queued). **Resolution:**
   the accessible label MUST enumerate states in the same order they render.
3. **Option E's plan summary says `ten stories in six waves` while its verdict
   bar says `2 EPICs, 12 stories`.** **Resolution:** illustrative slack. Counts
   MUST be derived from one source at render time, never authored twice.

Also noted: option D's inception node reads `5 stages · 4 approvals`, which is
consistent (the fifth stage's approval is the gate being described), and
`.doc.pending` is defined but unused there — §5.3 requires the shipped
implementation to use it.

---

## 10. Traceability

| Contract item | Ruling / AC | Consumed by |
|---|---|---|
| 1 Rollup | D-R13 §1, AC-11 | #4212 |
| 2 Origin strip | D-R13 §2 | #4212, #4208 |
| 3 Deep links + log drawer | D-R13 §3, AC-2, AC-3, R-O1b, R-O3f | #4212, #4213 |
| 4 Agreed plan | D-R13 §4, AC-1, AC-4, D-R8 | #4212 |
| 5 Stage documents | D-R13 §5, AC-5 | #4212, #4208, #4213 |
| 6 Engine visibility | D-R13 §6, D-R10, D-C1, D-C2, AC-26 | #4212 |
| 7 Plain language | D-R13 §7, R-O2a–d, AC-11, AC-21, AC-22, AC-23 | #4212, #4213, #4208 |
| 8 Node taxonomy | D-R13 §8 | **#4196 (schema)**, #4212 |
| 9 Build vs adopt | D-R13 closing, emission-lint Rule 3 | #4212 |

**Human review gate.** Per **D-R9** this contract requires sponsor sign-off
before the wave-6 dashboard stories ([#4212](https://github.com/aws-e/adp/issues/4212),
[#4213](https://github.com/aws-e/adp/issues/4213)) are started. That review is
tracked on [#4192](https://github.com/aws-e/adp/issues/4192).

**Amending this contract.** It is normative, so a dashboard story MUST NOT
silently deviate. If an item proves unimplementable, raise it on #4192, get a
ruling, and amend this file in the same PR as the deviation — the amendment is
the record.
