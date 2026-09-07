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
 * One member tenant's settled spend for the caller — Issue #4626 (C1 of #4620).
 *
 * Transcribed from `PerOrgLine` in `src/budget/schemas.py`. Carries **both** of the
 * caller's person-grain ledgers since #4396: `cloud_spend_usd` (their agents) and
 * `direct_spend_usd` (their own interactive use). The two are separate fields rather
 * than one pre-added figure because they are keyed in different namespaces — cloud by
 * canonical `users.id`, direct by Cognito sub — and a caller who wants the total has
 * `person_envelope.spend_usd`, which is the one the server enforces against.
 */
export interface PerOrgLine {
  /** The tenant this line's ledger rows live in. Derived server-side from the caller's memberships. */
  org_id: string;
  /** Display name from `organizations.name`, falling back to `org_id`. Server-supplied. */
  org_name: string;
  /** Settled `root_user` (cloud-agent) spend in this tenant for this period at 6dp. A true `'0.000000'` when no usage row exists. */
  cloud_spend_usd?: string;
  /**
   * Settled `user` (direct, interactive) spend in this tenant for this period at 6dp,
   * added by #4396. A true `'0.000000'` when no usage row exists. Disjoint from
   * `cloud_spend_usd` by ledger key, so the two may be added; adding either to an
   * org-grain figure is the #4322 double-count family.
   */
  direct_spend_usd?: string;
  /**
   * The cloud-agent cap **this tenant** authored for the caller, at 2dp, or `null` when
   * it authored none. Governs `cloud_spend_usd` only — a per-org `root_user` row — not
   * the line's total, and not the personal limit (`GET /me/budget/person-cap`). Not
   * clamped across tenants: each org's cap governs only spend executing inside it. A
   * `null` cap on a line with real spend is the mis-partitioned-cap signature #4620 was
   * filed for, so it must not render as `$0.00`.
   */
  cap_usd: string | null;
  /** `true` for the tenant the caller's session is attributed to — the one partition `lines`/`binding` describe. */
  is_active_partition: boolean;
}

/**
 * The caller's cross-org TOTAL spend — direct + cloud, and the figure their personal
 * limit is enforced against (Issue #4396).
 *
 * Transcribed from `PersonEnvelope` in `src/budget/schemas.py` (Issue #4626, design
 * note §7.1; widened by #4396). Originally the cloud-agent-only total that read `$0` on
 * the operator's own page while real dollars accrued in another tenant. Per the
 * operator ruling of 2026-09-05 it is now **one number**: everything the person spent,
 * their own interactive use plus the agents they triggered, across every workspace they
 * belong to — and the server enforces their personal limit against exactly this figure.
 *
 * **There is still no `cap_usd`, `remaining_usd`, `utilization_pct` or `band` field**,
 * and that is deliberate rather than left over. The ceiling is real now, but it has one
 * home: `GET /me/budget/person-cap`. Restating it here would give the UI two sources for
 * one limit that can disagree (#4322), and the absent fields keep a progress bar from
 * being bound to a denominator this payload does not carry. A client that wants the
 * `x / y` reading fetches the cap and renders the two together.
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
   * The person's TOTAL: `cloud_spend_usd + direct_spend_usd` at 6dp, i.e. the exact sum
   * of both components of every `per_org[]` line. This is the figure the personal limit
   * denies against, so it is the one to render as the headline. Still a LOWER BOUND —
   * `freshness.cost_backfill_lag` applies to it like every other settled figure.
   */
  spend_usd: string;
  /** The agent half of `spend_usd` at 6dp: settled `root_user` rows across all partitions. */
  cloud_spend_usd?: string;
  /** The interactive half of `spend_usd` at 6dp: settled `user` rows across all partitions. */
  direct_spend_usd?: string;
  /** How many tenants contributed to `spend_usd`. Distinguishes "one partition, genuinely $0" from "several, genuinely $0". */
  partition_count: number;
  /**
   * Always `false`, and typed so it cannot be anything else. It means THIS OBJECT
   * carries no denominator — not that the figure is ungoverned. Since #4396 a personal
   * limit IS enforced against `spend_usd`; the cap comes from the person-cap endpoint.
   */
  is_budget: false;
  /** Plain-language statement of what the figure covers and what governs it, for rendering as a caption. */
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
   * The caller's settled spend PER member tenant — cloud and direct, active partition
   * first — Issue #4626 (C1 of #4620), both components since #4396.
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
   * The caller's cross-org TOTAL — the sum of both components of every `per_org[]` line,
   * and since #4396 the figure their personal limit is enforced against. It still
   * carries no denominator field: the cap has one home (`GET /me/budget/person-cap`).
   * See `PersonEnvelope`.
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
  /**
   * Which rung of the #4690 ladder supplied the number: `own` (a pre-ruling
   * self-authored row), `admin` (a platform admin authored an individual row),
   * `team_default` / `org_default` / `platform_default` (no individual row; a
   * scope-wide rule governs). `null` when uncapped.
   */
  source: 'own' | 'admin' | 'team_default' | 'org_default' | 'platform_default' | null;
  /**
   * The server-composed human sentence naming that provenance (e.g. "org default
   * for acme"). Render it verbatim — only the ladder resolver knows which rung
   * won, and recomputing the label client-side is how it drifts from the rung
   * actually enforced (#4511).
   */
  source_label: string | null;
  /**
   * ISO-8601 instant the limit was last authored, or `null` when uncapped — and
   * `null` on default rungs, whose timestamps are withheld from person-facing
   * surfaces on purpose.
   */
  updated_at: string | null;
}

/**
 * The body for authoring a limit.
 *
 * One field, used only by the platform-admin write (`setPersonCapFor`) — the
 * self-service write routes were removed by the #4690 ruling. The target person
 * rides in the URL, not the body, and nothing here sets `enforcement_mode`,
 * which is not client-settable: admin-authored rows are always enforced (#4630),
 * so a mode parameter would only add a way to author a cap that does nothing.
 *
 * A string, not a number: money crosses the wire at the column's precision, and a
 * JS number would round `0.1 + 0.2`-style. Removing a limit is a DELETE, never a
 * `'0'` — `'0'` is a real ceiling of zero dollars and the server rejects it.
 */
export interface PersonCapRequest {
  budget_amount_usd: string;
}
