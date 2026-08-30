/**
 * The per-line envelope and the combined informational total — Issue #4402 (U-5).
 *
 * This file is where the EPIC's central rule is rendered: **each line is checked
 * against its own cap, and no two lines are ever added into something presented as a
 * budget.** Enforcement evaluates the caller's `direct` line (keyed by Cognito sub)
 * and their `cloud` line (keyed by canonical `users.id`) separately, against separate
 * caps. There is no ledger row and no cap anywhere in the data model equal to
 * "everything this person set in motion", so a fused figure with a ceiling would be
 * governed by nothing — the screen would say "exhausted" while enforcement stopped
 * nothing. The fused envelope *with* a real cap is #4396 and is deliberately not here.
 *
 * Hence `CombinedTotal` below renders **no progress bar and no `x / y` denominator**.
 * That is not stylistic restraint: `CombinedInformational` carries no cap field on the
 * wire at all, so there is no denominator available to render even by accident.
 */

import { Card } from '@/components/ui';
import { describeBand, formatUtilization } from '@/utils/budgetBand';
import { SERVICE_PRINCIPAL_QUALIFIER } from '@/types/budget';
import type { BudgetLine, CombinedInformational } from '@/types/budget';

/**
 * Render a wire money string for display.
 *
 * Money arrives as a string at the column's own precision (caps 2dp, spend/headroom
 * 6dp) so sub-cent digits survive JSON. Display rounds to cents — but only for
 * display, and only after `Number` has parsed the full-precision value, so nothing
 * downstream compares a rounded figure against a cap.
 *
 * A malformed or absent value renders an em dash, never `$0.00`: this function is
 * called on `cap_usd`/`remaining_usd`, both of which are legitimately `null` on an
 * uncapped line, and "no cap configured" must not read as "no money left".
 */
function formatMoney(value: string | null | undefined): string {
  if (value == null) return '—';
  const amount = Number(value);
  if (Number.isNaN(amount)) return '—';
  const sign = amount < 0 ? '-' : '';
  return `${sign}$${Math.abs(amount).toFixed(2)}`;
}

/** True when a line's root principal is an unattended trigger rather than a person. */
function isServicePrincipal(line: BudgetLine): boolean {
  return line.principal_kind === 'service';
}

/**
 * The affordance distinguishing a service principal from a person.
 *
 * A `service:`-rooted line is CI, EventBridge or an alarm. The mockup's member table
 * rendered these as colleagues, which makes per-person cost truth wrong — someone
 * looking for who spent the money is shown a robot's name as a human's. Rendered from
 * `principal_kind`, which the server derives from the id qualifier, never from a
 * display name (a human can be *called* anything).
 */
function PrincipalAffordance({ line }: { line: BudgetLine }) {
  if (!isServicePrincipal(line)) return null;
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"
      data-testid="service-principal-affordance"
      data-principal-kind="service"
      title={`Automated trigger (${SERVICE_PRINCIPAL_QUALIFIER}…) — not a person. Excluded from per-person totals.`}
    >
      <span aria-hidden="true">⚙️</span>
      <span>Automation</span>
    </span>
  );
}

/**
 * One line's utilisation bar.
 *
 * A bar is legitimate **here** — a line has a real cap on the wire, so the bar has a
 * genuine denominator and `role="progressbar"` is an honest claim. This is exactly the
 * element that must not appear on the combined total.
 *
 * Rendered only for a capped line with a defined percentage. An uncapped line has no
 * ratio, and a `$0` cap has none either (`utilization_pct` is `null` for both), so
 * there is nothing to fill and a 0%-filled bar would read as "plenty of room".
 */
function LineBar({ line }: { line: BudgetLine }) {
  if (line.cap_status !== 'capped' || line.utilization_pct == null) return null;

  const presentation = describeBand(line.band);
  // The bar is clamped for *drawing* only. `utilization_pct` can exceed 100 when
  // settled spend has passed the cap, and the real figure stays in aria-valuenow and
  // in the printed percentage — clamping the reported number would hide the overage.
  const width = Math.min(100, Math.max(0, line.utilization_pct));

  return (
    <div
      role="progressbar"
      aria-valuenow={line.utilization_pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`${line.label} utilisation`}
      className="h-2 w-full rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden"
    >
      <div className={`h-full rounded-full ${presentation.barClass}`} style={{ width: `${width}%` }} />
    </div>
  );
}

/** A band badge, showing the position against the cap — never a consequence. */
export function BandBadge({ band }: { band: BudgetLine['band'] }) {
  const presentation = describeBand(band);
  const tone =
    presentation.variant === 'success'
      ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
      : presentation.variant === 'warning'
        ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'
        : presentation.variant === 'error'
          ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
          : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300';

  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${tone}`} data-testid="band-badge" data-band={band ?? 'uncapped'}>
      {presentation.label}
    </span>
  );
}

/**
 * One separately-capped line: its own cap, spend, headroom, utilisation and band.
 *
 * Every figure comes from this line's own fields. Nothing is derived from a sibling
 * line and no threshold is computed here — `band` is read straight off the response,
 * because a locally-derived band would disagree with the server's 80/95 thresholds.
 */
export function BudgetLineRow({ line }: { line: BudgetLine }) {
  const uncapped = line.cap_status === 'uncapped';

  return (
    <div className="py-4 border-b border-gray-200 dark:border-gray-700 last:border-0 space-y-2" data-testid="budget-line-row" data-source={line.source ?? 'shared'}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-gray-900 dark:text-white">{line.label}</span>
          <PrincipalAffordance line={line} />
        </div>
        <BandBadge band={line.band} />
      </div>

      <LineBar line={line} />

      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-sm">
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Spend</dt>
          <dd className="font-mono text-gray-900 dark:text-white">{formatMoney(line.spend_usd)}</dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Cap</dt>
          {/* "No cap set" is a distinct statement from "$0.00". A $0 cap is a real
              cap that nothing costing money can pass; an uncapped line has none. */}
          <dd className="font-mono text-gray-900 dark:text-white">{uncapped ? 'No cap set' : formatMoney(line.cap_usd)}</dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Headroom</dt>
          {/* Headroom may be negative — settled spend can pass a cap. It is shown as
              the true position rather than clamped to a flat "$0.00 left". */}
          <dd className="font-mono text-gray-900 dark:text-white">{uncapped ? '—' : formatMoney(line.remaining_usd)}</dd>
        </div>
        <div>
          <dt className="text-gray-500 dark:text-gray-400">Used</dt>
          <dd className="font-mono text-gray-900 dark:text-white">{formatUtilization(line.utilization_pct)}</dd>
        </div>
      </dl>
    </div>
  );
}

/**
 * The direct+cloud dollar total — **informational, and never presented as a budget**.
 *
 * Deliberately absent from this component, and each absence is a requirement:
 *
 * - **no `role="progressbar"`** — a bar implies a ceiling, and no cap governs this
 *   number;
 * - **no `x / y` denominator** — there is no `y`; the wire carries no cap field here;
 * - **no band, no headroom, no utilisation** — all of them presuppose a cap.
 *
 * What it does carry is the server's own `note`, so the caption saying this is not a
 * budget is worded once, server-side, rather than re-invented per surface.
 */
export function CombinedTotal({ combined }: { combined: CombinedInformational }) {
  return (
    <div className="pt-4 space-y-1" data-testid="combined-informational">
      <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Combined direct + cloud spend</p>
      <p className="text-2xl font-bold font-mono text-gray-900 dark:text-white" data-testid="combined-total-amount">
        {formatMoney(combined.spend_usd)}
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">{combined.note}</p>
    </div>
  );
}

export interface BudgetLinesProps {
  lines: BudgetLine[];
  combined: CombinedInformational | null;
}

/**
 * The caller's per-person lines, each with its own cap, plus the combined total.
 *
 * `lines` arrives most-specific-first and is rendered in that order. Shared ancestors
 * (team/department/org) are not in it even when one of them binds — when one does it
 * appears as the headline instead, which is the screen's job, not this component's.
 */
export function BudgetLines({ lines, combined }: BudgetLinesProps) {
  return (
    <Card>
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Your budget lines</h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
        Each line has its own cap and is checked separately — they are not added together into a single budget.
      </p>

      {lines.length === 0 ? (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400" data-testid="budget-lines-empty">
          No spend lines for this period yet.
        </p>
      ) : (
        <div className="mt-4">
          {lines.map((line) => (
            <BudgetLineRow key={`${line.entity_type}:${line.source ?? 'shared'}:${line.label}`} line={line} />
          ))}
        </div>
      )}

      {combined && <CombinedTotal combined={combined} />}
    </Card>
  );
}
