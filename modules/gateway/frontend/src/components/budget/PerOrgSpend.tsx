/**
 * Cloud-agent spend per workspace — Issue #4646 (C1-UI of #4620), demoted to a
 * drill-down by #4685.
 *
 * The tiles above this component present two figures: direct use in this workspace, and
 * cloud spend everywhere. This is the breakdown *behind* the second one — one row per
 * workspace, showing where those dollars actually accrued. A person whose agent runs
 * execute in a different tenant than their session sees `$0` in the single-partition
 * figures while real dollars accrue elsewhere; #4626 widened the read and these rows are
 * the only place the result is legible.
 *
 * Two rules are load-bearing:
 *
 * 1. **No total here.** The cross-workspace sum is the Cloud spend tile's numerator
 *    (#4685). This component used to render it a second time as `PersonEnvelopeTotal`,
 *    and one figure with two renderings on one page is precisely the ambiguity the #4669
 *    ruling removed — a reader could not tell whether the two numbers counted the same
 *    dollars. The tile owns the total; these rows own the breakdown.
 * 2. **A per-org line's cap is that tenant's own.** Each org's `root_user` cap governs
 *    spend executing inside that org and nothing else, so the caps are reported per line
 *    and never folded together. A `null` cap renders as "No cap set" — a distinct
 *    statement from `$0.00`, which is a real cap nothing costing money can pass — unless
 *    the caller has a personal limit, in which case a workspace that authored no cap is
 *    not ungoverned and the cell says so instead.
 *
 * Kept out of `BudgetLines.tsx` deliberately: that component's contract is the caller's
 * separately-capped lines *within* the active partition. These rows are a different axis
 * (one tenant each, cloud only), and merging them would invite exactly the summing this
 * file must not do.
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
 * One tenant's cloud-agent spend, with that tenant's own cap.
 *
 * No bar and no band: `PerOrgLine` carries neither a `band` nor a `utilization_pct` on
 * the wire. Both are server-derived elsewhere from the 80/95 thresholds enforcement
 * reads, and deriving them here would be this screen inventing a band the server never
 * stated — the failure `BudgetSpend.tsx` already refuses for the lines above it.
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
          <dt className="text-gray-500 dark:text-gray-400">Spend</dt>
          <dd className="font-mono text-gray-900 dark:text-white" data-testid="per-org-spend">
            {formatWireMoney(line.cloud_spend_usd)}
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
  /** Per-tenant cloud lines, active partition first. `undefined` on a response predating #4640. */
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
        Your agent runs are billed to the workspace they execute in, and each workspace sets its own cap. Spend in one workspace is not governed by
        another workspace's cap.
      </p>

      <div className="mt-3">
        {lines.map((line) => (
          <PerOrgRow key={line.org_id} line={line} personCapApplies={personCapStatus === 'capped' && personCapEnforcing === true} />
        ))}
      </div>
    </div>
  );
}
