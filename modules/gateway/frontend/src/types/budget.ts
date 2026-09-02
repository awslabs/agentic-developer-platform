/**
 * The budget read surface's wire contract — Issue #4402 (U-5 of EPIC #4324).
 *
 * **Every type here is transcribed from `modules/gateway/src/budget/schemas.py`**,
 * which is the contract of record. It is not designed here and it is not inferred
 * from what a screen happens to want: #3675 shipped a dashboard whose types,
 * fixtures, tests *and* eval all agreed on fields the backend never sent, and the
 * loop was closed because every artefact was derived from the same invented shape.
 * If a field below disagrees with `schemas.py`, `schemas.py` is right.
 *
 * Four rules from that file are load-bearing for anything rendering these types:
 *
 * 1. **Money is a string, never a number.** Caps settle at `NUMERIC(10,2)` and
 *    spend at `NUMERIC(14,6)`; a float round-trip loses sub-cent precision, which
 *    is the defect class that accrued real spend against a `$0.00` accumulator.
 *    Parse at the point of comparison, not at the point of receipt.
 * 2. **`cap_status: 'uncapped'` is not a cap of `$0`.** Uncapped means no budget row
 *    governs the line, and `cap_usd`/`remaining_usd`/`utilization_pct`/`band` are all
 *    `null`. A real zero cap is `capped` with `cap_usd: '0.00'`, and is always
 *    `exceeded`. Collapsing the two shows an uncapped user as exhausted, or a
 *    $0-capped user as unlimited.
 * 3. **`remaining_usd` may be negative.** Settled spend can pass a cap. The read
 *    surface shows the true position rather than clamping to a flat "$0.00 left".
 * 4. **`utilization_pct` is `null` for a `$0` cap** as well as for an uncapped line —
 *    no percentage is defined there, and `0.0` would read as "plenty of room".
 */

import type { CostFigure } from '@/utils/cost';

/**
 * The warning band a utilisation figure falls in.
 *
 * **Server-derived, from the 80/95 thresholds on the budget config.** The band is
 * read off the response, never recomputed on the client: `BudgetManagement.tsx`
 * hardcodes a *different* band (50/80), so a locally-derived band tells a user they
 * are fine at 79% while the server has already warned them. `95` is a critical
 * warning, not a stop — enforcement blocks at >= 100%.
 */
export type BudgetBand = 'none' | 'warning' | 'critical' | 'exceeded';

/** Whether a cap exists at all. Deliberately not collapsible into `cap_usd == null`. */
export type CapStatus = 'capped' | 'uncapped';

/**
 * Whether the caller's canonical `users.id` resolved.
 *
 * `unresolved` means the cloud-agent (`root_user`) ledger could NOT be looked up and
 * is therefore ABSENT from the figures — it must not be rendered as "no cloud spend".
 */
export type IdentityStatus = 'resolved' | 'unresolved' | 'not_applicable';

/**
 * Which of the caller's two spend paths a line describes.
 *
 * `direct` is traffic they originated themselves; `cloud` is the agent chains they
 * triggered. `null` for a shared ancestor (team/department/org), which is neither —
 * labelling one `direct` would attribute a colleague's spend to the caller's machine.
 */
export type BudgetSource = 'direct' | 'cloud';

/**
 * Whether a root principal is a person or an unattended trigger.
 *
 * Derived server-side from the `service:` id qualifier. A `service` line is CI, an
 * alarm or EventBridge — not a colleague — and rendering one as a person makes
 * per-person cost truth wrong.
 */
export type PrincipalKind = 'human' | 'service';

/** The `service:` namespace qualifier on a `root_user` entity id. */
export const SERVICE_PRINCIPAL_QUALIFIER = 'service:';

/** Which of the caller's lines a run counts against. Matches `BudgetLine.source`. */
export type RunAttribution = 'direct' | 'cloud';

/**
 * The calendar window a set of figures describes.
 *
 * Only calendar periods appear — `run` and `chain` caps are lifetime-scoped, have no
 * calendar window, and the endpoint rejects them with a `422`.
 */
export interface BudgetPeriod {
  period_type: BudgetPeriodType;
  /** First day of the period, inclusive (ISO date). */
  period_start: string;
  /** Last day of the period, inclusive (ISO date). */
  period_end: string;
  /** Whole days until `period_end`. `0` means the counter resets tomorrow. */
  resets_in_days: number;
}

/**
 * The three calendar periods the endpoint accepts — and the only three the selector
 * may offer. `run`/`chain` are not calendar periods and must never appear as options.
 */
export type BudgetPeriodType = 'daily' | 'weekly' | 'monthly';

/** The period selector's options, in the order they are offered. */
export const BUDGET_PERIOD_TYPES: readonly BudgetPeriodType[] = ['daily', 'weekly', 'monthly'] as const;

/**
 * One separately-capped line in the caller's envelope.
 *
 * A line is **one ledger row's worth of truth**: a single entity+period read, its own
 * cap, and the headroom that follows. Two lines are never added together to produce
 * anything presentable as a budget — the caller's `direct` and `cloud` lines are keyed
 * differently (Cognito sub vs canonical `users.id`) and no cap anywhere governs their
 * sum. The fused envelope with a real cap is #4396 and is not built here.
 */
export interface BudgetLine {
  /** Which ledger this line was read from — `user`, `root_user`, `team`, `department`, `org`, `service_account`. */
  entity_type: string;
  /** Server-supplied line name, so two surfaces cannot word it differently. */
  label: string;
  source: BudgetSource | null;
  principal_kind: PrincipalKind;
  /** The EFFECTIVE cap after the platform-ceiling clamp, at 2dp. `null` when uncapped. */
  cap_usd: string | null;
  /** Settled spend at 6dp. Always present; no usage row is a true `'0.000000'`. */
  spend_usd: string;
  /** Headroom (`cap - spend`) at 6dp. May be NEGATIVE. `null` when uncapped. */
  remaining_usd: string | null;
  /** Spend as a percentage of cap, to 1dp. `null` when uncapped AND for a `$0` cap. */
  utilization_pct: number | null;
  /** Server-derived band. `null` when uncapped. A `$0` cap is always `exceeded`. */
  band: BudgetBand | null;
  cap_status: CapStatus;
  /**
   * How this cap behaves when exceeded, off the budget config row. `null` when
   * uncapped. **This is the field to consult before telling a user their spend will
   * be stopped** — under `shadow` it will not be.
   *
   * Typed as a string rather than a closed union because the backend types it `str`:
   * a value the SPA has not seen must not become `undefined` on the wire.
   */
  enforcement_mode: string | null;
}

/**
 * The direct+cloud dollar total — **informational only, never a budget**.
 *
 * The **shape** is the guarantee. There is deliberately no `cap_usd`, no
 * `remaining_usd`, no `utilization_pct` and no `band` field anywhere on this type,
 * because none exists on the wire — so no progress bar can be bound to a denominator
 * and the "no `x / y` bar" rule is enforced by the type rather than by reviewer
 * vigilance. `is_budget` is an unsettable `false` for the same reason.
 *
 * No cap governs this number and no ledger row contains it. Shared ancestors and
 * `service:`-rooted lines are excluded from the sum server-side.
 */
export interface CombinedInformational {
  /** Sum of the caller's own per-person lines at 6dp. NOT a budget and NOT enforced. */
  spend_usd: string;
  /** Always `false`, and typed so it cannot be anything else. */
  is_budget: false;
  /** Plain-language restatement of `is_budget`, for rendering as a caption. */
  note: string;
}

/**
 * One member tenant's settled cloud-agent spend for the caller — Issue #4626 (C1 of #4620).
 *
 * Transcribed from `PerOrgLine` in `src/budget/schemas.py`. `root_user` (cloud) spend
 * only: the caller's `direct` ledger is keyed by Cognito sub and only ever lands in the
 * tenant they signed into, so there is nothing cross-partition about it, and mixing the
 * two entity types into one figure is the #4322 double-count family.
 */
export interface PerOrgLine {
  /** The tenant this line's ledger rows live in. Derived server-side from the caller's memberships. */
  org_id: string;
  /** Display name from `organizations.name`, falling back to `org_id`. Server-supplied. */
  org_name: string;
  /** Settled `root_user` spend in this tenant for this period at 6dp. A true `'0.000000'` when no usage row exists. */
  cloud_spend_usd: string;
  /**
   * The cloud-agent cap **this tenant** authored for the caller, at 2dp, or `null` when
   * it authored none. Not clamped across tenants — each org's cap governs only spend
   * executing inside it. A `null` cap on a line with real spend is the
   * mis-partitioned-cap signature #4620 was filed for, so it must not render as `$0.00`.
   */
  cap_usd: string | null;
  /** `true` for the tenant the caller's session is attributed to — the one partition `lines`/`binding` describe. */
  is_active_partition: boolean;
}

/**
 * The caller's cross-org cloud-agent total — **informational, never a budget**.
 *
 * Transcribed from `PersonEnvelope` in `src/budget/schemas.py` (Issue #4626, design
 * note §7.1). This is the figure that reads `$0` on the operator's own page today while
 * real dollars accrue in another tenant's partition.
 *
 * **The shape is the guarantee, exactly as for `CombinedInformational`.** There is no
 * `cap_usd`, no `remaining_usd`, no `utilization_pct` and no `band` field anywhere on
 * this type because none exists on the wire — so no progress bar can be bound to a
 * denominator, and the "no `x / y` bar" rule is enforced by the type rather than by
 * reviewer vigilance. The reason is stronger here than for the combined total: a
 * person-level cap is a table that does not exist yet, and whether one may ever *deny*
 * is an open ruling. A denominator now would advertise a ceiling nothing enforces —
 * #4620's own defect, inverted.
 */
export interface PersonEnvelope {
  /**
   * The cross-org identity the total was fused on — `github:<numeric id>` when a GitHub
   * identity is linked, else `users:<canonical id>`. One person can hold a different
   * `users.id` per tenant, so summing by canonical id alone under-reports for exactly
   * the multi-org population this figure exists for.
   */
  anchor: string;
  /**
   * Exact sum of every `per_org[].cloud_spend_usd` at 6dp. NOT a budget and NOT
   * enforced. As much a LOWER BOUND as every other figure — `freshness.cost_backfill_lag`
   * applies to this total too.
   */
  spend_usd: string;
  /** How many tenants contributed to `spend_usd`. Distinguishes "one partition, genuinely $0" from "several, genuinely $0". */
  partition_count: number;
  /** Always `false`, and typed so it cannot be anything else. */
  is_budget: false;
  /** Plain-language restatement of `is_budget`, for rendering as a caption. */
  note: string;
}

/**
 * How complete the settled figures are.
 *
 * **An object, not a boolean** — matching the wire exactly. Flattening it to
 * `freshness: true` would make this read `undefined`, the affordance would silently
 * never render, and mock-backed tests would still pass: the #3675 failure again.
 */
export interface Freshness {
  /**
   * `true` when a recent request is logged but not yet priced, so every `spend_usd`
   * alongside it is a LOWER BOUND and real spend is higher. `false` is a positive
   * statement that the figures are complete, not merely absence of evidence.
   */
  cost_backfill_lag: boolean;
}

/**
 * The signed-in caller's own cap, settled spend and headroom for one period.
 * `GET /me/budget`.
 *
 * The top-level cap/spend/headroom fields are the **headline**, and the headline is
 * the *binding* line — the lowest-remaining capped entity, the line that will
 * actually stop them first. **The headline is never the sum of the lines**:
 * enforcement evaluates each entity against its own cap, so a summed headline would
 * be governed by no cap and the screen would say "exhausted" while enforcement
 * stopped nothing.
 */
export interface BudgetEnvelopeResponse {
  period: BudgetPeriod;

  /** Which entity in the hierarchy the headline figures describe. */
  entity_type: string;
  /** The effective cap at 2dp. `null` when uncapped. */
  cap_usd: string | null;
  /** Settled spend at 6dp. Always present. */
  spend_usd: string;
  /** Headroom at 6dp. May be NEGATIVE. `null` when uncapped. */
  remaining_usd: string | null;
  /** To 1dp. `null` when uncapped and for a `$0` cap. */
  utilization_pct: number | null;
  band: BudgetBand | null;
  cap_status: CapStatus;
  /** `hard`, `soft`, or `shadow` while enforcement is advisory. `null` when uncapped. */
  enforcement_mode: string | null;
  identity_status: IdentityStatus;

  /**
   * The line that will stop the caller first — the lowest-remaining CAPPED line.
   * `null` when nothing in the hierarchy is capped, since an uncapped line cannot
   * bind. Not an error, and not the same as a `$0` cap.
   */
  binding: BudgetLine | null;

  /**
   * The caller's per-person lines, most specific first. Each carries its OWN cap,
   * spend, headroom, utilisation and band, because enforcement checks each
   * separately. Shared ancestors are not listed here even though they can bind; when
   * one binds it appears as `binding`.
   */
  lines: BudgetLine[];

  /** `null` when there is nothing to combine (fewer than two per-person lines). */
  combined_informational: CombinedInformational | null;

  /**
   * The caller's settled cloud-agent spend PER member tenant, active partition first —
   * Issue #4626 (C1 of #4620).
   *
   * Everything above this field describes ONE partition (the attributed tenant
   * enforcement reads); this describes all of them, because a person whose runs execute
   * outside their session's tenant sees `$0` above while real dollars accrue elsewhere.
   * Empty when the caller's canonical identity did not resolve — never a fabricated
   * single-tenant `$0` line.
   *
   * **Optional here, though the backend always sends it** (a `default_factory=list`
   * field): a response predating #4640 omits it entirely, and that must render the
   * screen exactly as it rendered before rather than throwing on `.map`.
   */
  per_org?: PerOrgLine[];

  /**
   * The cross-org sum of `per_org[].cloud_spend_usd` — INFORMATIONAL ONLY. No cap
   * governs it and no ledger row equals it, so it carries no denominator field by
   * design (see `PersonEnvelope`).
   *
   * `null` when there are no per-org lines to sum, i.e. when the caller's identity did
   * not resolve. Present even for a single partition, unlike `combined_informational`:
   * "this is your total everywhere" is a distinct, useful claim when the count is one.
   * `undefined` on a response predating #4640.
   */
  person_envelope?: PersonEnvelope | null;

  /** ALWAYS present and never `null`, so it needs no null guard. */
  freshness: Freshness;
}

/**
 * One agent run that contributed to the caller's spend.
 *
 * `cost` is three-valued and `unknown` is the *common* case for a recent run, because
 * back-fill is asynchronous — never `$0.00` for a missing row.
 */
export interface BudgetRunItem {
  /** The run's id (the lineage `event_id`, which is the cost-table join key). */
  run_id: string;
  /** The chain this run belongs to; `null` for a run with no chain context. */
  correlation_id: string | null;
  /** Which persona ran, e.g. `developer`. `null` when the lineage row records none. */
  persona: string | null;
  /** ISO-8601 instant the run arrived. */
  started_at: string | null;
  /** Last known status, e.g. `in_progress`, `complete`, `budget_stopped`. */
  status: string | null;
  cost: CostFigure;
  attribution: RunAttribution;
}

/**
 * The runs that contributed to the caller's spend in one period.
 * `GET /me/budget/runs`.
 *
 * **`subtotal` and `total_run_count` describe THIS PAGE, not the period.** A period
 * total is not available without reading every page, and a page figure labelled as a
 * period total is exactly the class of wrong number this EPIC exists to eliminate —
 * the period-wide settled total is what `GET /me/budget` reports.
 */
export interface BudgetRunsResponse {
  /** The runs on this page, newest first. */
  items: BudgetRunItem[];
  /**
   * Total cost of the runs ON THIS PAGE. `partial` is `true` when any run on the page
   * is `unknown`, making this a LOWER BOUND.
   */
  subtotal: CostFigure;
  /** Number of runs on this page. Not a period-wide count. */
  total_run_count: number;
  /**
   * Opaque cursor for the next page; `null` means no more pages. A non-null cursor
   * with few or zero items is normal — filters are applied after the page read.
   */
  next_cursor: string | null;
  period: BudgetPeriod;
  identity_status: IdentityStatus;
}

// ---------------------------------------------------------------------------
// The person-level cap — Issue #4629 (#4620 · C3)
// ---------------------------------------------------------------------------
//
// A person's own ceiling on their total agent spend, across EVERY organization.
// Distinct from every type above, which describes a cap belonging to one org:
// this one belongs to a person and has no org at all, which is exactly why it can
// express "my total" when the org-scoped ones cannot (#4620).
//
// **Enforcing since C4 (#4630).** Every save writes `enforcement_mode: 'hard'`:
// the limit DENIES attributed agent requests across every organization once the
// settled cross-org total passes it. Rows authored before C4 remain `'soft'`
// (reported, nothing denied) until re-saved. Copy rendering these types must
// track the mode: a `soft` row must not threaten a stop it cannot deliver, and a
// `hard` row must not stay silent about the stop it WILL — either direction is
// the screen/behavior disagreement #4620 exists to close.
//
// Deliberately absent: spend, headroom, utilisation and band. Those need a
// cross-org denominator, which is #4626. A spend figure derived alongside this cap
// would be a second accumulator of the same dollars.

/**
 * The caller's (or, for a platform admin, a named person's) platform-wide limit.
 * `GET|PUT /me/budget/person-cap`, `GET|PUT /budget/person-cap/{anchor}`.
 */
export interface PersonCapResponse {
  /**
   * The cross-org person key the limit is stored against, `github:<numeric_id>`.
   * Not a `users.id`: a person onboarded into two orgs has two of those, so a cap
   * keyed on one would miss their spend in the other.
   */
  person_anchor: string;
  period_type: BudgetPeriodType;
  /** The authored limit at 2dp, or `null` when none is set. `null` is NOT `'0.00'`. */
  cap_usd: string | null;
  /** `capped` when a limit exists, `uncapped` when none does — read this, never a zero. */
  cap_status: CapStatus;
  /**
   * `'hard'` — the limit DENIES the person's agent runs across every org (#4630).
   * `'soft'` — informational only, the C3-era mode: the figure is reported and
   * nothing is blocked. `null` when uncapped.
   *
   * Read it; never assume either. A surface must not tell the user their spend
   * will be stopped while this is `'soft'`, and must not stay silent about it
   * while it is `'hard'`. Both mistakes are the same one.
   */
  enforcement_mode: string | null;
  /** ISO-8601 instant the limit was last authored, or `null` when uncapped. */
  updated_at: string | null;
}

/**
 * The body for authoring a limit.
 *
 * One field. The person is derived server-side on the self path, so there is no
 * target to send — and nothing here sets `enforcement_mode`, which is not
 * client-settable: authoring your own limit IS the choice to be enforced (#4630),
 * so a mode parameter would only add a way to author a cap that does nothing.
 *
 * A string, not a number: money crosses the wire at the column's precision, and a
 * JS number would round `0.1 + 0.2`-style. Removing a limit is a DELETE, never a
 * `'0'` — `'0'` is a real ceiling of zero dollars and the server rejects it.
 */
export interface PersonCapRequest {
  budget_amount_usd: string;
}
