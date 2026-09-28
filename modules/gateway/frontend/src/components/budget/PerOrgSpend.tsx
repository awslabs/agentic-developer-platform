/**
 * Spend per workspace — Issue #4646 (C1-UI of #4620), demoted to a drill-down by #4685,
 * widened to both spend components by #4396.
 *
 * The card above presents ONE figure: the person's total across every workspace. This is
 * the breakdown *behind* it — one row per workspace, showing where those dollars actually
 * accrued. A person whose agent runs execute in a different tenant than their session
 * sees `$0` in the single-partition figures while real dollars accrue elsewhere; #4626
 * widened the read and these rows are the only place the result is legible.
 *
 * Three rules are load-bearing:
 *
 * 1. **No total here.** The cross-workspace sum is the card's headline figure (#4685).
 *    This component used to render it a second time as `PersonEnvelopeTotal`, and one
 *    figure with two renderings on one page is precisely the ambiguity the #4669 ruling
 *    removed — a reader could not tell whether the two numbers counted the same dollars.
 *    The card owns the total; these rows own the breakdown. That applies to a row's own
 *    two figures as well: they are shown side by side and never added, so no cell on this
 *    screen is a sum the server did not send.
 * 2. **The two spend cells are separate because their ledgers are** (#4396). Direct spend
 *    settles under `entity_type="user"` keyed by Cognito sub, agent spend under
 *    `root_user` keyed by canonical `users.id`. Showing them apart is what makes the
 *    headline total legible — a reader can see which half of their spend came from where,
 *    which is the actionable part.
 * 3. **A per-org line's cap is that tenant's own, and it governs the CLOUD figure.** Each
 *    org's `root_user` cap covers agent spend executing inside that org and nothing else,
 *    so the caps are reported per line and never folded together. A `null` cap renders as
 *    "No cap set" — a distinct statement from `$0.00`, which is a real cap nothing costing
 *    money can pass — unless the caller has a personal limit, in which case a workspace
 *    that authored no cap is not ungoverned and the cell says so instead.
 *
 * Kept out of `BudgetLines.tsx` deliberately: that component's contract is the caller's
 * separately-capped lines *within* the active partition. These rows are a different axis
 * (one tenant each), and merging them would invite exactly the summing this file must
 * not do.
 */

import { formatWireMoney } from '@/utils/cost';
import type { CapStatus, IdentityStatus, PerOrgLine } from '@/types/budget';

/**
 * The "this workspace" affordance.
 *
 * Read off the server's `is_active_partition` rather than re-derived from the token:
 * the flag exists so a client can mark the one partition that `lines`/`binding` above
 * describe without guessing, and a locally-derived guess could disagree with the
 * figures rendered beside it.
 */
function ActivePartitionBadge() {
  return (
    <span
      className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-primary-100 text-primary-700 dark:bg-primary-900 dark:text-primary-100"
      data-testid="per-org-active-badge"
    >
      This workspace
    </span>
  );
}

/**
 * One tenant's spend — agents and direct use side by side — with that tenant's own cap.
 *
 * No bar and no band: `PerOrgLine` carries neither a `band` nor a `utilization_pct` on
 * the wire. Both are server-derived elsewhere from the 80/95 thresholds enforcement
 * reads, and deriving them here would be this screen inventing a band the server never
 * stated — the failure `BudgetSpend.tsx` already refuses for the lines above it.
 *
 * The two spend cells keep the labels the ledgers actually mean ("Agents" / "Direct"),
 * not a single "Spend" cell holding their sum: the row's job is to say which half came
 * from where, and the sum already has exactly one home in the headline above.
 */
function PerOrgRow({ line, personCapApplies }: { line: PerOrgLine; personCapApplies: boolean }) {
  return (
    <div
      className="py-3 border-b border-gray-200 dark:border-gray-700 last:border-0 flex items-start justify-between gap-3 flex-wrap"
      data-testid="per-org-row"
      data-org-id={line.org_id}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-medium text-gray-900 dark:text-white">{line.org_name}</span>
        {line.is_active_partition && <ActivePartitionBadge />}
      </div>

      <dl className="flex items-start gap-6 text-sm">
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Agents</dt>
          {/* testid unchanged (`per-org-spend`): this cell is still the cloud figure the
              per-tenant cap governs, and renaming it would churn every existing
              assertion for no change in what is being asserted. */}
          <dd className="font-mono text-gray-900 dark:text-white" data-testid="per-org-spend">
            {formatWireMoney(line.cloud_spend_usd)}
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Direct</dt>
          {/* Through `formatWireMoney` like every other figure, so a response predating
              #4396 (or a stale cache mid-rollout) renders the no-data indicator rather
              than a fabricated `$0.00` — which here would claim the person never worked
              interactively in this workspace. */}
          <dd className="font-mono text-gray-900 dark:text-white" data-testid="per-org-direct-spend">
            {formatWireMoney(line.direct_spend_usd)}
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Cap here</dt>
          {/* Three distinct statements, and none of them is "$0.00" (a $0 cap is a real
              cap that nothing costing money can pass):
                - this tenant authored a cap → the figure;
                - it authored none AND the caller has a personal limit → that limit is
                  what governs this spend, so the row is not ungoverned (#4685);
                - it authored none and there is no personal limit → "No cap set", the
                  mis-partitioned-cap signature this screen exists to make legible. */}
          <dd className="text-gray-900 dark:text-white" data-testid="per-org-cap">
            {line.cap_usd != null ? (
              <span className="font-mono">{formatWireMoney(line.cap_usd)}</span>
            ) : personCapApplies ? (
              'your personal limit applies'
            ) : (
              'No cap set'
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export interface PerOrgSpendProps {
  /** Per-tenant lines (agent + direct spend), active partition first. `undefined` on a response predating #4640. */
  perOrg: PerOrgLine[] | undefined;
  /**
   * Whether the caller has a personal limit, which changes what a `null` workspace cap
   * *means* — a workspace that authored none is not ungoverned when a personal limit
   * covers it. `undefined` while the limit is still loading or unreadable, in which case
   * the row makes the weaker, always-true statement ("No cap set") rather than a claim
   * about a limit it has not read.
   */
  personCapStatus: CapStatus | undefined;
  /**
   * Whether the personal limit actually ENFORCES (`enforcement_mode === 'hard'`).
   * A row may only claim "your personal limit applies" when it does (review fix on
   * #4686): a soft pre-C4 limit governs nothing, and captioning uncapped spend with
   * it hides exactly the ungoverned-spend state "No cap set" exists to expose.
   */
  personCapEnforcing: boolean | undefined;
  /** The response's own identity verdict — the rows must not contradict the unresolved notice above them. */
  identityStatus: IdentityStatus | undefined;
}

/**
 * "By workspace": one row per member tenant, and no total.
 *
 * Renders **nothing at all** when there are no rows. `per_org` is optional on the wire —
 * a backend predating #4640 omits it — and a heading with no rows would assert the caller
 * has no cross-workspace spend when the truth is that this response never spoke to the
 * question. Same reason `per_org: []` (identity unresolved) draws nothing: the page
 * already renders the explicit "could not be looked up" notice for that case, and a
 * fabricated `$0` line here would contradict it.
 */
export function PerOrgSpend({ perOrg, personCapStatus, personCapEnforcing, identityStatus }: PerOrgSpendProps) {
  const lines = perOrg ?? [];
  // Client-side guards for invariants the backend promises but a bug, stale cache, or
  // partial rollout could break (review fix): an unresolved identity must never show
  // exact cloud figures beneath the "could not be looked up" notice.
  if (identityStatus === 'unresolved') return null;
  if (lines.length === 0) return null;

  return (
    // "per-org-section", not "per-org-spend": each row's spend cell already carries
    // that testid, and one id with two meanings lets an unscoped query match the
    // container and pass against the wrong element (review fix on #4686).
    <div data-testid="per-org-section">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Your agent runs are billed to the GitHub org they execute in, and each org sets its own cap on them. Spend in one org is not governed by
        another org's cap.
        {/* Only when a limit actually ENFORCES (review fix on #4689): asserting
            "your personal limit covers this" beside rows whose cap cell says
            "No cap set" claims governance that does not exist — the exact
            ungoverned-spend masking this component's #4686 gating rule forbids. */}
        {personCapStatus === 'capped' && personCapEnforcing === true && <> Your personal limit covers both columns, everywhere.</>}
      </p>

      <div className="mt-3">
        {lines.map((line) => (
          <PerOrgRow key={line.org_id} line={line} personCapApplies={personCapStatus === 'capped' && personCapEnforcing === true} />
        ))}
      </div>
    </div>
  );
}
