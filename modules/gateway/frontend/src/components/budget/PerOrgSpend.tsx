/**
 * Cloud-agent spend per workspace, and the cross-org person total — Issue #4646 (C1-UI of #4620).
 *
 * The screen above this component describes **one partition**: the tenant the caller's
 * session is attributed to, which is the partition enforcement reads. That is correct
 * and unchanged. But a person whose agent runs execute in a *different* tenant than
 * their session sees `$0` there while real dollars accrue elsewhere — the cap sits in
 * one partition and the spend in another, and `/me/budget` used to read only the first.
 * #4626 widened the read; this component is the only place that renders the result, so
 * until it exists the fix is invisible to the person it was filed for.
 *
 * Two rules are load-bearing, and both are enforced by the *shape* of the wire types
 * rather than by care:
 *
 * 1. **The person envelope is not a budget.** `PersonEnvelope` carries no `cap_usd`, no
 *    `remaining_usd`, no `utilization_pct` and no `band` — the fields do not exist on
 *    the wire, so there is no denominator available to render even by accident. Hence
 *    no `role="progressbar"`, no `x / y`, no band badge and no headroom below. That is
 *    not restraint for its own sake: no person-level cap table exists yet and whether
 *    one may ever *deny* is an open ruling, so a bar here would advertise a ceiling
 *    nothing enforces — which is #4620's own defect, inverted. Its caption is the
 *    server's own `note`, rendered verbatim, so the "this is not a budget" sentence is
 *    worded once server-side instead of re-invented per surface.
 * 2. **A per-org line's cap is that tenant's own.** Each org's `root_user` cap governs
 *    spend executing inside that org and nothing else, so the caps are reported per line
 *    and never folded together. A `null` cap on a line with real spend is precisely the
 *    mis-partitioned-cap signature #4620 is about, so it renders as "No cap set" — a
 *    distinct statement from `$0.00`, which is a real cap nothing costing money can pass.
 *
 * Kept out of `BudgetLines.tsx` deliberately: that component's contract is the caller's
 * separately-capped lines *within* the active partition, and its `combined` slot is the
 * single-partition direct+cloud total. These rows are a different axis (one tenant each,
 * cloud only), and merging them would invite exactly the summing this file must not do.
 */

import { Card } from '@/components/ui';
import type { IdentityStatus, PerOrgLine, PersonEnvelope } from '@/types/budget';

/**
 * Render a wire money string for display.
 *
 * Money arrives as a string at the column's own precision (caps 2dp, spend 6dp) so
 * sub-cent digits survive JSON. Display rounds to cents, but only after `Number` has
 * parsed the full-precision value, so nothing downstream compares a rounded figure
 * against a cap. A malformed or absent value renders an em dash, never `$0.00`.
 */
function formatMoney(value: string | null | undefined): string {
  // `Number('')`/`Number('  ')` are 0, not NaN, and 'Infinity' passes an isNaN
  // check — an empty or non-finite wire value must render as unknown, never as
  // `$0.00` (the exact misstatement #4620 was filed to eliminate) or `$Infinity`.
  if (value == null || value.trim() === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  // Real sub-cent spend must not read as $0.00 on the row whose purpose is
  // disproving a $0 reading — same 4dp convention as utils/cost.ts (#4207).
  if (amount !== 0 && Math.abs(amount) < 0.01) {
    const sign = amount < 0 ? '-' : '';
    return `${sign}$${Math.abs(amount).toFixed(4)}`;
  }
  // Sign from the ROUNDED value so '-0.004' cannot render '-$0.00'.
  const rounded = Number(amount.toFixed(2));
  const sign = rounded < 0 ? '-' : '';
  return `${sign}$${Math.abs(rounded).toFixed(2)}`;
}

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
function PerOrgRow({ line }: { line: PerOrgLine }) {
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
            {formatMoney(line.cloud_spend_usd)}
          </dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Cap here</dt>
          {/* "No cap set" is a distinct statement from "$0.00". A $0 cap is a real cap
              that nothing costing money can pass; a null cap means this tenant authored
              none — and a null cap on a line with real spend is the very
              mis-partitioned-cap signature this screen exists to make legible. */}
          <dd className="font-mono text-gray-900 dark:text-white" data-testid="per-org-cap">
            {line.cap_usd == null ? 'No cap set' : formatMoney(line.cap_usd)}
          </dd>
        </div>
      </dl>
    </div>
  );
}

/**
 * The cross-org person total — **informational, and never presented as a budget**.
 *
 * Each absence below is a requirement, not an omission:
 *
 * - **no `role="progressbar"`** — a bar implies a ceiling, and no cap governs this number;
 * - **no `x / y` denominator** — there is no `y`; the wire carries no cap field here;
 * - **no band, no headroom, no utilisation** — all three presuppose a cap.
 *
 * `partition_count` is stated alongside the figure so the total cannot be misread as
 * single-tenant, and so "one workspace, genuinely $0" is distinguishable from "several
 * workspaces, genuinely $0".
 */
function PersonEnvelopeTotal({ envelope }: { envelope: PersonEnvelope }) {
  return (
    <div className="pt-4 space-y-1" data-testid="person-envelope">
      <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
        Total cloud-agent spend across {envelope.partition_count} {envelope.partition_count === 1 ? 'workspace' : 'workspaces'}
      </p>
      <p className="text-2xl font-bold font-mono text-gray-900 dark:text-white" data-testid="person-envelope-amount">
        {formatMoney(envelope.spend_usd)}
      </p>
      {/* The server's own wording, verbatim: the "not a budget" caption is authored once,
          server-side, so two surfaces cannot describe the same figure differently. */}
      <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="person-envelope-note">
        {envelope.note}
      </p>
    </div>
  );
}

export interface PerOrgSpendProps {
  /** Per-tenant cloud lines, active partition first. `undefined` on a response predating #4640. */
  perOrg: PerOrgLine[] | undefined;
  /** The cross-org total. `null` when identity did not resolve; `undefined` on an older response. */
  personEnvelope: PersonEnvelope | null | undefined;
  /** The response's own identity verdict — the card must not contradict the unresolved notice above it. */
  identityStatus: IdentityStatus | undefined;
}

/**
 * "Cloud agents by workspace": one row per member tenant, plus the cross-org total.
 *
 * Renders **nothing at all** when the response carries neither field. Both are optional
 * on the wire — a backend predating #4640 omits them — and an empty card captioned
 * "by workspace" would assert the caller has no cross-org spend when the truth is that
 * this response never spoke to the question. Same reason `per_org: []` (identity
 * unresolved) draws no rows: `BudgetSpend.tsx` already renders the explicit
 * "could not be looked up" notice for that case, and a fabricated `$0` line here would
 * contradict it.
 */
export function PerOrgSpend({ perOrg, personEnvelope, identityStatus }: PerOrgSpendProps) {
  const lines = perOrg ?? [];
  // Client-side guards for invariants the backend promises but a bug, stale
  // cache, or partial rollout could break (review fix): an unresolved identity
  // must never show exact cloud figures beneath the "could not be looked up"
  // notice, and an envelope without its per-line breakdown would assert a total
  // no visible row supports.
  if (identityStatus === 'unresolved') return null;
  if (lines.length === 0) return null;

  return (
    <Card>
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Cloud agents by workspace</h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
        Your agent runs are billed to the workspace they execute in, and each workspace sets its own cap. Spend in one workspace is not governed by
        another workspace's cap.
      </p>

      {lines.length > 0 && (
        <div className="mt-4">
          {lines.map((line) => (
            <PerOrgRow key={line.org_id} line={line} />
          ))}
        </div>
      )}

      {/* Only beside its breakdown — a total with zero visible rows is unverifiable. */}
      {personEnvelope && <PersonEnvelopeTotal envelope={personEnvelope} />}
    </Card>
  );
}
