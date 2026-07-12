# Design & UX Review: Agent Run Dashboard and Agent Activity Screens

**Issue**: #3754 | **Parent EPIC**: #3753
**Date**: 2026-07-12
**Reviewer**: @agent-pm
**Governing Principle**: C7 — simplicity and intuitiveness first; fewer/clearer beats exhaustive; if an element needs a tooltip to explain itself, cut or reword.

---

## Table of Contents

1. [Task Walkthroughs](#1-task-walkthroughs)
2. [Findings Inventory](#2-findings-inventory)
3. [Consistency Audit](#3-consistency-audit)
4. [Terminology Glossary Proposal](#4-terminology-glossary-proposal)
5. [Information Hierarchy Assessment](#5-information-hierarchy-assessment)
6. [State Coverage Review](#6-state-coverage-review)
7. [Filter & Deep-Link UX](#7-filter--deep-link-ux)
8. [Accessibility Pass](#8-accessibility-pass)
9. [Draft Child Stories](#9-draft-child-stories)
10. [Scripted First-Session Walkthrough](#10-scripted-first-session-walkthrough)

---

## 1. Task Walkthroughs

### Task A: "What did my agents do today?"

**Steps**:
1. User lands on `/runs` (home/landing page)
2. Sees 4 stat tiles: Running now, Failed today, Spend today, Succeeded today
3. Sees "Recent Failures" list below tiles (capped at 5)
4. Clicks "Agent Activity" in nav to see the full list

**Friction points**:
- **F-A1 (major)**: The dashboard shows counts (failed: 3, succeeded: 12) but there is no "total runs today" tile. User must mentally sum Failed + Succeeded + Running to get their total. The stat card has `today.total` in the API response but it's unused.
- **F-A2 (minor)**: "Recent Failures" only shows 5 items with no "View all" link — dead end for users with >5 failures.
- **F-A3 (minor)**: The "Succeeded today" tile has no subtitle (unlike Running Now which shows "N active runs"). Inconsistent tile density.
- **F-A4 (major)**: No clear visual connection between the dashboard and Activity page. The dashboard's heading says "Agent Runs" and nav says "Dashboard"; the list page's heading says "Agent Activity" and nav says "Agent Activity". A user looking for "my runs" may not realize Activity IS the detail view.
- **F-A5 (polish)**: Dashboard empty state CTA says "View Activity" — navigates to `/activity` which may also be empty and has no guidance for triggering a first run. Circular dead end.

### Task B: "Why did this run fail?" (Dashboard failure row -> Activity detail -> Transcript)

**Steps**:
1. User sees red-bordered "Failed today: 3" tile on dashboard
2. Scrolls to "Recent Failures" list
3. Clicks a failure row → navigates to `/activity?id=<invocation_id>`
4. Deep-link auto-opens InvocationDetail modal
5. In modal, sees Status: Failed, Error section (truncated at 200 chars, expandable)
6. Clicks "View full transcript" link in detail modal
7. TranscriptViewer modal opens atop the InvocationDetail modal

**Friction points**:
- **F-B1 (blocker)**: Clicking the "Failed today" tile navigates to `/activity?status=failed&since=today` — this opens the FLAT LIST view (correct per #3736) but does NOT auto-open any detail. User sees a list of failures but must click again to see WHY. Compare: clicking a specific failure in "Recent Failures" does auto-open detail via `?id=`. Inconsistent interaction model.
- **F-B2 (major)**: Nested modals — TranscriptViewer opens ON TOP of InvocationDetail. Two overlapping modals with independent close buttons. Escape closes the top one (correct), but the UX of stacked modals is disorienting. The transcript should replace the detail content or open as a slide-panel/page.
- **F-B3 (major)**: Error message truncation at 200 chars in the detail modal is too aggressive for stack traces. "Show more" expands in place but the modal's `max-h-[calc(90vh-8rem)]` means long errors push other detail rows out of view. The error is the most important field for a failed run but appears LAST in the detail list (below timing, IDs, channel, topic, summary, duration, Bedrock usage, source, run log, transcript, lineage).
- **F-B4 (minor)**: The "Recent Failures" list on the dashboard shows `topic || 'Untitled run'` + persona + relative time. No status glyph or error preview — user must click to discover the failure reason. A one-line error preview (first 80 chars) would reduce clicks.

### Task C: "What is this costing me?" (Tile -> Per-run costs -> Reconciliation)

**Steps**:
1. User sees "Spend today" tile on dashboard
2. Notes the dollar amount (or "—" if unavailable)
3. Navigates to Agent Activity to see per-run costs
4. In flat view, "Cost" column shows per-run spend
5. In chain view, chain-level total is shown at row end
6. Clicks a row → detail modal shows "Bedrock usage" (calls, cost, tokens)

**Friction points**:
- **F-C1 (major)**: "Spend today" tile is NOT clickable (no navigation, no hover cursor change). User expects tile-click behavior (the other 3 tiles are clickable). The non-interactive nature is discoverable only by trial — violates consistency.
- **F-C2 (major)**: The dashboard spend uses a separate `useRunStats(1)` call for "today's" spend, but the Activity list's cost column shows per-run cost. There is no way to reconcile: dashboard says "$47.23 today" but summing visible activity rows might not match because (a) pagination hides older rows, (b) the spend API includes bot-attributed runs that may not appear in "mine" view depending on the root-human-index merge.
- **F-C3 (minor)**: Cost display format inconsistency: `$0.0034` (4 decimals <$0.01) vs `$1.23` (2 decimals) across tiles, flat list, chain list, and detail modal — same logic duplicated in 4 places (CostBadge, ChainCostBadge, ChainRow inline, InvocationDetail's formatCost). Should be one shared utility.
- **F-C4 (minor)**: "Spend today" subtitle shows "Cost data temporarily unavailable" when null, but doesn't explain WHY (is it a backend lag? a missing integration? a new account?). Compare: the CostBadge in the activity table shows "pending" for in-progress runs — different wording, different semantic.
- **F-C5 (polish)**: No total or subtotal row in the activity table. Users wanting "total spend this page" must mentally sum 20 rows.

---

## 2. Findings Inventory

| # | Severity | Dimension | Component | Finding | Proposed Fix |
|---|----------|-----------|-----------|---------|--------------|
| 1 | Blocker | Info hierarchy | InvocationDetail | Error message rendered LAST in detail modal for failed runs; most important info below fold | Move error to immediately after Status row for failed invocations |
| 2 | Major | Consistency | SpendTodayTile | Not clickable unlike other 3 tiles; no cursor/hover/keyboard handling | Add click-through to `/activity` with cost column visible, or link to a cost-detail view |
| 3 | Major | Task flow | FailedTodayTile | Tile click lands on flat list but doesn't open detail — breaks "why did it fail?" flow | Either auto-highlight first row, or add a summary panel above the table showing aggregate failure reasons |
| 4 | Major | Terminology | Nav + headers | "Dashboard" (nav) vs "Agent Runs" (page heading) vs "Agent Activity" (different page) — unclear relationship | Rename: nav shows "Runs" for dashboard, "Activity" for list. Or unify to single page with tab. |
| 5 | Major | UX | TranscriptViewer | Nested modal (transcript atop detail) — disorienting, stacked close targets | Transcript replaces modal content (back arrow to return to detail) or opens as full-page slide-out |
| 6 | Major | Info hierarchy | AgentRunDashboard | `today.total` available in API but no "Total today" tile; user must mentally sum 3 values | Replace "Succeeded today" with "Completed today" (total) — succeeded is derivable from total minus failed |
| 7 | Major | Filter UX | AgentActivity | Active filters not visually indicated; no "clear filters" affordance | Add filter chips above table showing active filters with X to remove; "Clear all" button |
| 8 | Major | Responsive | AgentActivity flat table | 9-column table has only `overflow-x-auto`; narrow viewport requires horizontal scroll with no indication | Collapse to card layout on narrow viewport; or hide lower-priority columns (Summary, Link) behind "more" |
| 9 | Minor | Consistency | Status labels | "Webhook recv" (activity table) vs "Webhook received" (detail modal) for same status | Standardize to "Received" (shorter) or "Webhook received" (full) everywhere |
| 10 | Minor | Consistency | Trigger badge | "Agent-triggered" vs "Agent-initiated" (bot) — distinction unclear without domain knowledge | Rename: "Chained run" (agent→agent) vs "Automated" (bot/scheduled) — plain language per C7 |
| 11 | Minor | Info hierarchy | AgentRunDashboard | "Recent Failures" list caps at 5 with no "View all" link | Add "View all failures →" link navigating to `/activity?status=failed&since=today` |
| 12 | Minor | Discoverability | AgentActivity | "By chain / By run" toggle small (text-xs), located in header right side — easy to miss | Make slightly larger, add brief description text ("Group runs by their trigger chain") |
| 13 | Minor | State | AgentActivity | Empty state for mid-pagination ("No matching results on this page. More results may exist.") is confusing | Auto-advance to next page, or show "Skipped empty pages — showing results from page N" |
| 14 | Minor | Filter UX | AgentActivity | PERSONA_OPTIONS is hardcoded to 4 values (Developer, Architect, Reviewer, Ops) — new personas won't appear | Fetch persona list from the API or make it dynamic from seen data |
| 15 | Minor | Accessibility | AgentActivity chain row | Chain expand/collapse arrow (`▶`/`▼`) has no aria-label; screen readers see just a triangle character | Add `aria-expanded` and `aria-label="Expand chain"` |
| 16 | Minor | Accessibility | All tiles | Emoji icons (🔄, ❌, 💰) used as visual indicators — not reliably described by screen readers | Add sr-only text labels or use proper SVG icons with aria-label |
| 17 | Minor | Consistency | Timestamps | Dashboard failure list uses `formatRelativeTime` only; activity table uses relative with absolute on hover (title attr); detail modal shows both inline. Three different treatments. | Standardize: relative displayed, absolute on hover everywhere; detail modal alone shows both inline |
| 18 | Polish | Terminology | InvocationDetail modal | Title says "Invocation Detail" — internal jargon. User thinks "run" not "invocation" | Rename to "Run Detail" (or even just show the topic as the title) |
| 19 | Polish | State | SpendTodayTile | Shows "—" when cost null but subtitle says "Cost data temporarily unavailable" — unclear if this is normal or broken | Show "Not yet available" for new accounts; "Updating..." for temporary backend lag |
| 20 | Polish | Accessibility | Modal focus trap | Modal uses `tabIndex={-1}` and setTimeout focus — no true focus trap (Tab can escape to browser chrome) | Implement focus-trap-react or manual focus cycling on Tab/Shift+Tab |
| 21 | Polish | Visual | ChainRow expanded descendants | Uses Unicode `└─` for tree connector — fragile across fonts, no dark-mode contrast check | Use CSS borders/lines or SVG connectors |
| 22 | Polish | Filter UX | AgentActivity | Start/End date inputs have no placeholder or format hint | Add placeholder="YYYY-MM-DD" or switch to a date picker component |
| 23 | Polish | DX | Cost formatting | Same cost-formatting logic duplicated in 4 places across 3 files | Extract to `formatCostUsd()` in `utils/format.ts`, import everywhere |

---

## 3. Consistency Audit

### Terminology Inconsistencies

| Term in UI | Where used | What it actually means | Proposed standard |
|------------|------------|----------------------|-------------------|
| Run | Dashboard heading, tile subtitles, activity "By run" toggle | A single agent execution | **Run** (user-facing) |
| Invocation | Detail modal title, types, API paths | Same as "run" (technical name) | Drop from UI; use only in API/code |
| Chain | Activity "By chain" toggle, chain view header | A sequence of causally-linked runs | **Chain** |
| Correlation | Chain header ("Correlation ID"), types | Same as "chain" (technical name) | Drop from UI; show only in detail for debugging |
| Event | "Show all events" toggle, "event source" filter | A run (confusingly), or a non-triggering status record | Rename toggle: "Show intermediate statuses" |
| Topic | Table column, detail row | The issue/PR title that triggered the run | **Topic** (OK as-is) |
| Persona | Filter, badge, table column | The agent role (developer/reviewer/architect/ops) | **Agent type** (more intuitive per C7) |
| Channel | Filter ("Source"), detail ("Channel") | How the run was triggered (GitHub/Slack/API/manual) | **Source** (match the filter label) |
| Trigger kind | Trigger badge (human/agent/bot) | Who/what started this run | **Started by** |

### Status Vocabulary & Colors

Current status vocabulary is consistent across all surfaces (7 statuses, same glyphs and colors in STATUS_CONFIG, STATUS_GLYPHS, and InvocationDetail's STATUS_CONFIG). However:

- Three separate `STATUS_CONFIG` definitions exist (AgentActivity.tsx, InvocationDetail.tsx, InvocationChain.tsx) — any drift would be silent.
- "Webhook recv" (truncated) appears only in AgentActivity's STATUS_OPTIONS and STATUS_CONFIG; the detail modal uses "Webhook received" (full).
- Glyph choice: `✗` is used for failed, rejected, rate_limited, AND no_op — four different failure modes share one glyph. Recommend: `✗` (failed), `⊘` (rejected), `◷` (rate limited), `∘` (no_op — already uses this in some places).

### Timestamp Formats

| Surface | Format | Tooltip |
|---------|--------|---------|
| Dashboard failure list | Relative only ("3h ago") | None |
| Activity table (Date column) | Relative ("3h ago") | Absolute on hover (title attr) |
| Chain node | Relative ("3h ago") | Absolute on hover |
| Detail modal - Invoked at | Relative + absolute inline | Additional absolute in title |
| Detail modal - Last transition | Relative | Absolute on hover |

**Recommendation**: Standardize to relative-displayed + absolute-on-hover for all compact views; detail modal shows both inline (already correct).

---

## 4. Terminology Glossary Proposal

This glossary defines the canonical user-facing terms for the EPIC. All UI text, labels, and documentation should use these terms exclusively.

| Canonical term | Definition | NOT to use (internal/deprecated) |
|----------------|-----------|----------------------------------|
| **Run** | A single execution of an agent on a task | invocation, event, job |
| **Chain** | A sequence of runs triggered by each other (parent→child) | correlation, chain group |
| **Status** | The lifecycle state of a run: Received → In progress → Complete/Failed/Rejected/Rate limited/No-op | state, phase |
| **Source** | How the run was triggered: GitHub, Slack, API, Manual | channel, event source |
| **Agent type** | The role of the agent: Developer, Architect, Reviewer, Ops | persona |
| **Topic** | What the run is working on (issue/PR title or task description) | — |
| **Transcript** | The full conversation log of a run | — |
| **Cost** | The Bedrock API spend attributable to a run | spend, usage |
| **Started by** | Who/what triggered this run: You, Another run, Automated | trigger kind, trigger badge |

### Adoption plan
Phase 1: Update all user-visible labels in the six affected components.
Phase 2: Update filter labels and options text.
Phase 3: Update page headings and nav items.
Phase 4: Update types and API paths (non-breaking; old terms remain as aliases).

---

## 5. Information Hierarchy Assessment

### Dashboard (`/runs`) — Are the 4 tiles the right 4?

**Current tiles**: Running now, Failed today, Spend today, Succeeded today.

**Assessment**: "Succeeded today" is low-value — it's always total minus failed minus active (derivable). It doesn't prompt action. The high-value signal missing is **total runs today** (the first thing a user wants to know to assess "are my agents working?").

**Recommendation**:
| Position | Current | Proposed | Rationale |
|----------|---------|----------|-----------|
| 1 | Running now | **Running now** (keep) | Most urgent — are things happening? |
| 2 | Failed today | **Failed today** (keep) | Requires action |
| 3 | Spend today | **Total today** (new) | Context for the other numbers |
| 4 | Succeeded today | **Spend today** (move here, make clickable) | Financial awareness |

This drops "Succeeded today" (low-signal, derivable) in favor of "Total today" (high-signal, contextual). The "Recent Failures" list below the tiles remains.

### Activity (`/activity`) — Chain toggle discoverability

The "By chain / By run" toggle is:
- text-xs (12px)
- Located in the page header's right side
- Only visible when `viewMode === 'mine'`
- Uses a pill-style toggle with no label or description

**Problem**: First-time users don't know what "chain" means in this context. The toggle is small enough to miss entirely.

**Recommendation**: Add a one-line description below or as tooltip: "Group related runs together" for chain mode.

### Detail modal — Content ordering

**Current order**: Status → IDs → Timing → Channel → Topic → Summary → Duration → Bedrock usage → Source → Run log → Transcript → Lineage → Error.

**Problem**: Error appears LAST. For failed runs (the primary reason users open the detail), the most actionable field requires scrolling past 12 other rows.

**Recommended order for failed runs**: Status → Error → Duration → Cost → Topic → Transcript → Source → Lineage → IDs → Timing → Channel → Summary.

---

## 6. State Coverage Review

| State | Dashboard | Activity (flat) | Activity (chain) | Detail Modal | Transcript |
|-------|-----------|-----------------|-------------------|--------------|------------|
| **Empty (guided)** | Yes (EmptyState component with CTA) | Yes ("No agent activity yet") | Yes (same message) | n/a (not shown when null) | Yes ("Transcript is empty") |
| **Loading (skeleton)** | Yes (LoadingSkeleton, 4 tile blocks + 3 text lines) | Yes (TableSkeleton rows=10) | Yes (same skeleton) | n/a (instant from local data) | Yes (spinner + "Loading transcript...") |
| **Error** | Yes (red banner + Retry button) | Yes (Alert + Retry) | Yes (Alert + Retry) | n/a | Yes (user-friendly message + raw error) |
| **Partial (cost "—")** | Yes (SpendTodayTile shows "—" + subtitle) | Yes (CostBadge shows "—") | Yes (ChainCostBadge shows "—") | Yes (omits Bedrock row if null) | n/a |
| **Stale (24h guard)** | Backend handles (in_progress > 24h excluded from active count) | No UI indicator | No UI indicator | No indicator that run may be stale | n/a |
| **Paginated empty** | n/a | Yes ("No matching results... More results may exist" + Load next) | Yes (same) | n/a | n/a |

**Gaps identified**:
- **G1 (minor)**: No stale-run indicator in Activity. A run showing "In progress" for 24+ hours has no visual hint that it might be orphaned. Dashboard excludes it from count, but Activity still shows it with a live blue dot.
- **G2 (minor)**: Empty state on Activity offers no guidance (just "No agent activity yet"). The Dashboard's EmptyState is richer with a rocket emoji, explanation, and CTA. Apply similar guidance to Activity's empty state.
- **G3 (polish)**: Loading skeleton for chain view uses `TableSkeleton` (renders rows) but chains don't look like table rows — mismatch between skeleton shape and loaded content shape.

---

## 7. Filter & Deep-Link UX

### Tile → Filtered List Flow

| Tile | Navigates to | Result |
|------|-------------|--------|
| Running now | `/activity?status=in_progress` | Flat list, status filter = In progress. Count matches tile. |
| Failed today | `/activity?status=failed&since=today` | Flat list, status + date filter. Count matches tile. |
| Succeeded today | `/activity?status=complete&since=today` | Flat list, status + date filter. Count matches tile. |
| Spend today | (not clickable) | Nothing happens. |
| Recent failure row | `/activity?id=<invocation_id>` | Flat list + auto-opens detail modal. |

**Issues**:
- Spend tile inconsistency (Finding #2).
- No visual indication on the Activity page that filters are applied from the URL. The filter dropdowns DO show the correct selection, but there's no banner or chip saying "Showing: Failed runs from today" — user may not notice the pre-selected filter.

### Filter Visibility & Clearability

- Filters are in a collapsible panel (always expanded currently — no collapse mechanism).
- Active filter state is indicated only by the dropdown's selected value — easy to miss.
- No "Clear all" button to reset to defaults.
- Changing any filter resets pagination (correct).
- The "Show all events" checkbox is the only filter with explanatory text.

**Recommendations**:
1. Add filter chips above the result list (e.g., `[Status: Failed ×] [Since: Today ×] [Clear all]`).
2. When URL-applied filters are active, show a subtle banner: "Filtered view — [Clear filters]".
3. Add a "Reset" button to the filter panel.

### Pagination Behavior

- Cursor-based (correct for DDB).
- "Page N" displayed but no total page count (expected — cursor pagination can't know total).
- Previous/Next buttons, disabled at boundaries.
- Empty intermediate pages show confusing message (Finding #13).
- No "jump to page" or page-size control (acceptable for cursor-based).

---

## 8. Accessibility Pass

### Keyboard Navigation

| Element | Keyboard accessible? | Notes |
|---------|---------------------|-------|
| Dashboard tiles | Yes | `role="button"`, `tabIndex={0}`, Enter/Space handlers |
| Dashboard failure list rows | Partial | `<button>` wrapper — accessible, but no focus ring style visible |
| Activity table rows | No | `<tr onClick>` — not keyboard-focusable, no tabIndex, no role |
| Activity filter dropdowns | Yes | `<Select>` with aria-label |
| Activity "By chain/By run" toggle | Yes | `role="tab"`, `aria-selected` |
| Activity "Mine/All" toggle | Partial | `role="tab"`, `aria-selected`, but no `role="tablist"` label |
| Chain row expand/collapse | Partial | Clickable div, but no `aria-expanded`, no keyboard handler for Enter |
| Chain descendant buttons | Yes | `<button>` elements |
| Detail modal close | Yes | Button with aria-label |
| Transcript modal close | Yes | Same Modal component |
| "Show all events" checkbox | Yes | Label wraps input |
| Pagination buttons | Yes | `<Button>` with disabled state |

### Critical Accessibility Gaps

1. **Activity table rows not keyboard-navigable**: The entire `<tr>` relies on `onClick` with no `tabIndex` or `role="button"`. Keyboard users cannot reach or activate rows.
2. **Chain row expand/collapse**: The chain row header is a clickable `<div>` with no `role="button"`, no `tabIndex`, no `onKeyDown`. Keyboard users cannot expand chains.
3. **Emoji icons as meaningful indicators**: 🔄, ❌, 💰 in tiles, and 👤, 🤖, ⚙️ in trigger badges convey meaning but have inconsistent screen reader representation. Some have sr-only text (tile aria-labels include the meaning), trigger badge icons are `aria-hidden="true"` (correct) with adjacent text labels (correct).
4. **No focus trap in modal**: Modal sets `tabIndex={-1}` and focuses on open, but doesn't trap focus — Tab key can navigate behind the modal overlay.
5. **Color-only status differentiation**: Status is communicated via color AND glyph — good. But the chain row's expanded descendants section uses only the glyph character for status with no text label — screen readers get "check mark" or "cross" without "Complete" or "Failed" context.

### Contrast

- Dark mode uses `dark:text-gray-400` extensively — needs verification against WCAG 2.1 AA (4.5:1 for normal text). `gray-400` on `gray-800` background is approximately 4.2:1 — borderline FAIL for body text.
- The "pending" italic text for in-progress cost (`text-gray-400 dark:text-gray-500`) is likely below contrast threshold.

---

## 9. Draft Child Stories

### Story 1: Error-first detail layout for failed runs (Blocker — Finding #1)

#### Description
When a run fails, the error message is the single most actionable piece of information in the detail modal. Currently it renders last (after 12 other rows), requiring users to scroll past identifiers, timing, and metadata to find why their run failed. Reorder the detail modal layout to surface the error immediately after status for failed runs.

#### Impact analysis
- **Who benefits**: Every user investigating a failure (the primary use case for opening detail).
- **Who's impacted**: No other flows change — reorder is status-conditional (only failed runs).
- **What breaks if this ships with a bug**: Non-failed runs could accidentally show the error row (empty). Mitigated by conditional rendering already present.
- **Cost / quota footprint**: None (frontend-only layout change).

#### Design
- In `InvocationDetail.tsx`, move the error `<DetailRow>` block from the end of the `<dl>` to immediately after the Status row, wrapped in the existing `item.status === 'failed'` guard.
- Additionally, for failed runs, reorder to: Status → Error → Duration → Cost → Topic → Source → Transcript → Lineage → IDs → Timing → Channel → Summary.
- Non-failed runs keep current order (Error row hidden, no reorder).

#### Deployment
Frontend-only change. Merges via `gateway-deploy.yml` which rebuilds the SPA and syncs to S3 + CloudFront invalidation. No backend changes.

#### Validation
- Unit test: render InvocationDetail with `status: 'failed'` + `error_message` → assert error row appears before the Duration row.
- Visual: confirm error is visible without scrolling for a typical 3-line error message.
- Regression: non-failed run detail modal order unchanged.

---

### Story 2: Make Spend tile clickable (Major — Finding #2)

#### Description
The "Spend today" tile is the only non-interactive tile on the dashboard. Users expect all tiles to be clickable (the other three navigate to filtered views). Make the spend tile navigate to the activity list sorted by cost or filtered to show cost-bearing runs.

#### Impact analysis
- **Who benefits**: Users tracking cost attribution.
- **Who's impacted**: None — adding interactivity to a previously static element.
- **What breaks if this ships with a bug**: Worst case: navigation to wrong URL. Low risk.
- **Cost / quota footprint**: None.

#### Design
- Add click handler to `SpendTodayTile.tsx` navigating to `/activity?view=runs` (flat view where the Cost column is visible).
- Add `role="button"`, `tabIndex={0}`, `cursor-pointer`, `hover:shadow-md`, and keyboard handlers matching the pattern in `ActiveRunsTile.tsx`.
- Update `aria-label` to include "Click to view run costs".

#### Deployment
Frontend-only. Standard gateway-deploy.yml SPA rebuild.

#### Validation
- Unit test: clicking SpendTodayTile navigates to `/activity?view=runs`.
- Accessibility: keyboard Enter/Space triggers navigation.
- Regression: other tiles unchanged.

---

### Story 3: Replace nested transcript modal with inline content swap (Major — Finding #5)

#### Description
Viewing a transcript currently opens a second modal on top of the InvocationDetail modal — stacked modals are disorienting (two overlays, two close buttons, ambiguous Escape target). Replace with an inline content swap: clicking "View transcript" replaces the detail modal's body with transcript content + a "Back to detail" button.

#### Impact analysis
- **Who benefits**: All users viewing transcripts from the detail modal. Also benefits the standalone transcript links (those remain as single-modal opens — unaffected).
- **Who's impacted**: Users who had developed muscle memory for the nested modal pattern (unlikely — it's confusing enough that users avoid it).
- **What breaks if this ships with a bug**: Transcript view might not properly return to detail state. Mitigated by a simple boolean state toggle.
- **Cost / quota footprint**: None.

#### Design
- In `InvocationDetail.tsx`: add state `showTranscript: boolean`. When true, render `<TranscriptViewer>` content (not modal-wrapped) inside the existing detail modal body. Add a "← Back to detail" button at the top.
- `TranscriptViewer.tsx`: export a `TranscriptContent` component (the inner content without the Modal wrapper) for embedding. Keep the full `TranscriptViewer` (with Modal) for standalone use from the table.
- Remove the nested `<TranscriptViewer>` modal render from InvocationDetail.

#### Deployment
Frontend-only. Standard gateway-deploy.yml.

#### Validation
- Manual: open detail → click transcript → see transcript inline → click back → see detail again.
- Unit test: verify no nested modal rendered (no double `role="dialog"`).
- Regression: standalone transcript links from activity table still open their own modal.

---

### Story 4: Active filter indication and clear affordance (Major — Finding #7)

#### Description
When a user clicks a dashboard tile (e.g., "Failed today"), they land on the activity page with filters pre-applied from the URL. But there's no prominent indicator that filters are active, and no one-click way to clear them. Add filter chips and a "Clear all" button.

#### Impact analysis
- **Who benefits**: All users navigating from dashboard tiles or bookmarked filtered URLs.
- **Who's impacted**: Layout shift — filter chips add a row between the filter panel and the table.
- **What breaks if this ships with a bug**: Chips could show stale filter state or fail to clear. Mitigated by deriving chips from the same state variables that drive the queries.
- **Cost / quota footprint**: None.

#### Design
- Add a `<FilterChips>` component rendered between the filter panel and the table/chain list.
- Each active filter renders as a chip: `[Status: Failed ×] [Since: 2026-07-12 ×]`
- A "Clear all" button appears when any filter is active.
- Clicking `×` on a chip clears that single filter and resets pagination.
- When no filters are active, the chip row is hidden (no empty space).

#### Deployment
Frontend-only. Standard gateway-deploy.yml.

#### Validation
- Unit test: render with `?status=failed&since=today` → chips visible for both → click × on status → status cleared, chip removed.
- Regression: filter dropdowns still work independently.

---

### Story 5: Make activity table rows keyboard-accessible (Major — Finding from Accessibility #1)

#### Description
Activity table rows in flat view use `<tr onClick>` which is not keyboard-navigable. Keyboard users cannot reach or activate rows to open the detail modal. Add proper keyboard interaction.

#### Impact analysis
- **Who benefits**: Keyboard-only users, screen reader users.
- **Who's impacted**: None negatively — adds capability without removing mouse behavior.
- **What breaks if this ships with a bug**: Double-firing of click handler (keyboard + mouse). Mitigated by event handling best practices.
- **Cost / quota footprint**: None.

#### Design
- Add `tabIndex={0}` and `role="row"` (already semantic as `<tr>`) to each table row.
- Add `onKeyDown` handler: Enter opens the detail modal (same as click).
- Add visible focus ring style: `focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2`.
- Consider adding `aria-label` describing the row content (e.g., "Run: Fix login bug, Status: Failed, 3 hours ago").

#### Deployment
Frontend-only. Standard gateway-deploy.yml.

#### Validation
- Manual: Tab through table rows → focus ring visible → Enter opens detail modal.
- Unit test: simulate keyDown Enter on row → detail modal opens.
- axe-core audit: no violations on the table structure.

---

### Story 6: Narrow viewport responsive layout for Activity table (Major — Finding #8)

#### Description
The flat activity table has 9 columns. On viewports below ~1024px, the table overflows horizontally with only an `overflow-x-auto` wrapper — no scroll indicator, no responsive collapse. Implement a card-based layout for narrow viewports.

#### Impact analysis
- **Who benefits**: Mobile users, users with narrow browser windows, users on tablets.
- **Who's impacted**: None — behavior unchanged above breakpoint.
- **What breaks if this ships with a bug**: Layout might break at boundary widths. Mitigated by standard Tailwind responsive classes.
- **Cost / quota footprint**: None.

#### Design
- Below `lg` breakpoint (1024px): replace the table with a stacked card layout.
- Each card shows: Topic (heading), Status badge, Relative time, Source link, Cost.
- Secondary info (Trigger, Channel, Summary, Transcript link) in a collapsed "More" section or second line.
- Pagination controls unchanged (work at any width).
- Chain view already uses cards — no change needed there.

#### Deployment
Frontend-only. Standard gateway-deploy.yml.

#### Validation
- Manual: resize viewport to 768px → cards render instead of table.
- Visual regression test at 375px, 768px, 1024px, 1440px viewports.
- Regression: table renders correctly above 1024px.

---

## 10. Scripted First-Session Walkthrough

This walkthrough becomes the EPIC's acceptance test. A new user who has triggered at least 3 agent runs (including 1 failure) should be able to complete all tasks without hesitation or dead ends.

### Prerequisites
- User is authenticated.
- At least 3 agent runs exist: 1 active (in_progress), 1 completed, 1 failed (with error_message and transcript).
- At least 1 chain exists (parent→child trigger).

### Walkthrough Script

**Step 1: Landing orientation (< 5 seconds to orient)**
- [ ] User lands on `/runs`. Sees page title, 4 stat tiles, and recent failures.
- [ ] User can answer "how many runs happened today?" from tiles alone (requires Total tile — Finding #6).
- [ ] User can answer "is anything broken?" from the Failed tile + red border.
- [ ] User can answer "how much did it cost?" from Spend tile.

**Step 2: Investigate a failure (< 3 clicks to root cause)**
- [ ] User clicks a failure in the "Recent Failures" list.
- [ ] Detail modal opens with error message visible WITHOUT scrolling (requires Error-first layout — Story 1).
- [ ] User reads error message. If they need more context, clicks "View transcript" — transcript appears inline (requires Story 3).
- [ ] User clicks "← Back to detail" to return to the detail modal.
- [ ] User closes the modal.

**Step 3: See all recent activity (< 2 clicks from landing)**
- [ ] User clicks "Activity" in the nav (or the Activity link visible on dashboard).
- [ ] Activity page opens in chain view (default for unfiltered browsing).
- [ ] User sees chains grouped with expand arrows for multi-run chains.
- [ ] User clicks a chain to expand and see children.

**Step 4: Filter to failures (< 1 click + confirmation)**
- [ ] User selects "Failed" in the Status filter dropdown.
- [ ] View switches to flat (per #3736 rule: status filter → flat view).
- [ ] Filter chip appears above table: `[Status: Failed ×]` (requires Story 4).
- [ ] Table shows only failed runs. Count matches "Failed today" tile.
- [ ] User clicks `×` on the chip — filter clears, view returns to chain mode.

**Step 5: Check cost (< 2 clicks)**
- [ ] User clicks "Spend today" tile on dashboard (requires Story 2).
- [ ] Navigates to activity flat view. Cost column visible.
- [ ] User can see per-run cost for each row.

**Step 6: Keyboard-only navigation (Section 508 compliance)**
- [ ] User Tabs through dashboard tiles — focus ring visible on each.
- [ ] User presses Enter on "Failed today" tile — navigates to filtered activity.
- [ ] User Tabs through activity table rows — focus ring visible (requires Story 5).
- [ ] User presses Enter on a row — detail modal opens.
- [ ] User presses Escape — modal closes.

### Pass Criteria
All checkboxes above pass without the user needing to ask "what does this mean?" or "how do I...?" for any step. Any step that requires scrolling, guessing, or more than the stated number of clicks is a regression from the target UX.

---

## Appendix: Regression Contracts

Changes proposed in this review MUST NOT break:

1. **Tile-to-list count parity**: The number shown on a dashboard tile must exactly match the count of rows visible when clicking through to the filtered activity list (established in #3736).
2. **Deep-link behavior**: `/activity?id=<uuid>` must auto-open the detail modal for that run (established in #3632).
3. **"Show all events" toggle behavior**: When off, no_op and webhook_received rows are hidden from both flat and chain views (established in #1658).
4. **Chain view correctness**: Chains show only real descendants, not phantom children (established in #3723).
5. **Admin/non-admin view separation**: Non-admin users never see the "All (Admin)" toggle (established in #1457).
6. **Cost graceful degradation**: When cost data is null, UI shows "—" not "$0.00" (established in #3633 REQ-A5).
7. **Status filter → flat view rule**: A status filter in the URL forces flat (run) view, not chain view (established in #3736).
