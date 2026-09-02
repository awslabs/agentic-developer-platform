/**
 * Budget & Spend — Issue #4402 (U-5 of EPIC #4324).
 *
 * The screen the EPIC is named for: the one that turns a silent, punitive cap into a
 * visible budget. A signed-in user sees, for a chosen calendar period, their cap,
 * settled spend, remaining headroom, the band they are in, and the runs that got them
 * there.
 *
 * Four things it deliberately does NOT do:
 *
 * 1. **No fused headline bar.** The headline is the *binding* line — the
 *    lowest-remaining capped entity, the line that will actually stop them first —
 *    never the sum of the lines. A summed headline is governed by no cap, so the screen
 *    would read "exhausted" while enforcement stopped nothing. The combined direct+cloud
 *    figure is an informational total with no bar and no denominator (`BudgetLines`).
 * 2. **No band computed here.** Every band is read off the response, derived server-side
 *    from the same thresholds enforcement reads (80/95). `BudgetManagement.tsx` hardcodes
 *    a different 50/80 band; copying it would tell a user they are fine at 79% while the
 *    server has already warned.
 * 3. **No claim that spend will be stopped** while `enforcement_mode` is `shadow`. Caps
 *    are advisory in shadow mode, so that copy would simply be false — and a screen that
 *    threatens a consequence it cannot deliver is worse than no screen.
 * 4. **No `change=` prop on any tile.** `StatCard`'s `change` hardcodes "from yesterday"
 *    and colours increases green; for spend, an increase is not good news.
 *
 * Also not built, per the frozen rulings: the per-client-tool cost table and
 * device/session column (no data exists), the in-flight/reserved band and per-run/chain
 * cap gauges (no read API), and the "Preview as" role switcher (implies impersonation).
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { StatCard } from '@/components/dashboard/StatCard';
import { Card } from '@/components/ui';
import { BudgetLines, BandBadge } from '@/components/budget/BudgetLines';
import { PerOrgSpend } from '@/components/budget/PerOrgSpend';
import { BudgetRunsTable } from '@/components/budget/BudgetRunsTable';
import { PersonSpendingLimit } from '@/components/budget/PersonSpendingLimit';
import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';
import { describeBand, formatUtilization } from '@/utils/budgetBand';
import { COST_SCOPE_LABEL } from '@/utils/cost';
import { BUDGET_PERIOD_TYPES } from '@/types/budget';
import type { BudgetEnvelopeResponse, BudgetPeriodType } from '@/types/budget';

/** Display labels for the three calendar periods. */
const PERIOD_LABELS: Record<BudgetPeriodType, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
};

function formatMoney(value: string | null | undefined): string {
  if (value == null) return '—';
  const amount = Number(value);
  if (Number.isNaN(amount)) return '—';
  const sign = amount < 0 ? '-' : '';
  return `${sign}$${Math.abs(amount).toFixed(2)}`;
}

/**
 * The period selector.
 *
 * **Exactly three options**, and they are the three the endpoint accepts. `run` and
 * `chain` caps are lifetime-scoped, have no calendar window at all, and the endpoint
 * rejects them with a `422` — offering them would be offering a query that cannot
 * succeed. The list is `BUDGET_PERIOD_TYPES`, so it cannot drift from the wire type.
 */
function PeriodSelector({ value, onChange }: { value: BudgetPeriodType; onChange: (period: BudgetPeriodType) => void }) {
  return (
    <div role="group" aria-label="Budget period" className="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden">
      {BUDGET_PERIOD_TYPES.map((period) => (
        <button
          key={period}
          type="button"
          onClick={() => onChange(period)}
          aria-pressed={value === period}
          data-testid={`period-option-${period}`}
          className={`px-3 py-1.5 text-sm ${
            value === period
              ? 'bg-primary-100 text-primary-700 dark:bg-primary-900 dark:text-primary-100 font-medium'
              : 'bg-white text-gray-700 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
          }`}
        >
          {PERIOD_LABELS[period]}
        </button>
      ))}
    </div>
  );
}

/**
 * The shadow-mode banner.
 *
 * Rendered while the binding cap's `enforcement_mode` is `shadow`. The copy states the
 * cap is being *measured*, not enforced — no sentence on this path may say spend will
 * be stopped, blocked, or halted, because in shadow mode it will not be. Getting this
 * wrong does not merely mislead; it trains users to disbelieve the screen.
 */
function ShadowModeBanner() {
  return (
    <div
      className="rounded-lg border border-blue-200 bg-blue-50 p-4 dark:border-blue-800 dark:bg-blue-950"
      role="status"
      data-testid="shadow-mode-banner"
    >
      <p className="text-sm font-medium text-blue-900 dark:text-blue-100">These caps are advisory right now</p>
      <p className="mt-1 text-sm text-blue-800 dark:text-blue-200">
        Budget enforcement is in shadow mode: your usage is measured and reported against these caps, but requests continue as normal when a cap
        is passed. Use these figures to plan.
      </p>
    </div>
  );
}

/**
 * The freshness affordance.
 *
 * Spend figures are **settled** totals and settlement is asynchronous: a request is
 * logged the moment it finishes, but its price and the accumulator this screen reads
 * are written later. For the minutes in between, real spend is genuinely higher than
 * the figures say. Someone who reads an understated figure as final keeps working under
 * a cap they have already passed — the same screen-vs-reality disagreement this EPIC
 * exists to eliminate, displaced in time rather than in scope. So the gap is stated
 * rather than smoothed over.
 */
function FreshnessNotice() {
  return (
    <p className="text-sm text-amber-800 dark:text-amber-300" data-testid="freshness-notice">
      Some recent usage has not been priced yet, so the figures below are a lower bound — your actual spend may be higher.
    </p>
  );
}

/**
 * The headline tile: the binding line.
 *
 * `binding` is `null` when nothing in the caller's hierarchy is capped. That is not an
 * error and not a `$0` cap — it means no budget row governs them, so there is no
 * headroom to report and nothing that will stop them. Rendering `$0.00` there would
 * show an uncapped user as exhausted.
 */
function HeadlineTiles({ envelope }: { envelope: BudgetEnvelopeResponse }) {
  const binding = envelope.binding;

  if (!binding) {
    return (
      // The testid is on a wrapper, not on `Card`: `Card` accepts only
      // children/className/padding and silently drops anything else, so a
      // `data-testid` passed to it never reaches the DOM.
      <div data-testid="headline-uncapped">
        <Card>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">No cap is set for you</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Nothing in your hierarchy has a budget configured, so there is no limit to report. Your settled spend for this period is{' '}
            <span className="font-mono">{formatMoney(envelope.spend_usd)}</span>.
          </p>
        </Card>
      </div>
    );
  }

  const presentation = describeBand(binding.band);

  return (
    <div className="space-y-3" data-testid="headline-binding">
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">{binding.label}</h2>
        <BandBadge band={binding.band} />
        <span className="text-sm text-gray-500 dark:text-gray-400">
          — the line that will reach its cap first
        </span>
      </div>

      {/* No `change` prop on any of these: it hardcodes "from yesterday" and colours
          increases green, and for spend an increase is not good news. */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard title="Cap" value={formatMoney(binding.cap_usd)} subtitle={`${PERIOD_LABELS[envelope.period.period_type]} limit`} />
        <StatCard title="Spend" value={formatMoney(binding.spend_usd)} subtitle={COST_SCOPE_LABEL} />
        <StatCard
          title="Headroom"
          value={formatMoney(binding.remaining_usd)}
          subtitle={`${formatUtilization(binding.utilization_pct)} of cap used`}
          className={presentation.colorClass}
        />
      </div>
    </div>
  );
}

export default function BudgetSpend() {
  const [period, setPeriod] = useState<BudgetPeriodType>('monthly');

  const {
    data: envelope,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['myBudget', period],
    queryFn: () => getMyBudget(period),
  });

  const {
    data: runs,
    isLoading: runsLoading,
    error: runsError,
  } = useQuery({
    queryKey: ['myBudgetRuns', period],
    queryFn: () => getMyBudgetRuns({ period }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Budget &amp; Spend</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Your caps, settled spend and remaining headroom.</p>
        </div>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      {isLoading && (
        <div className="space-y-4" data-testid="budget-loading">
          <div className="h-24 bg-gray-200 dark:bg-gray-700 rounded-lg animate-pulse" />
          <div className="h-40 bg-gray-200 dark:bg-gray-700 rounded-lg animate-pulse" />
        </div>
      )}

      {/* A backend failure is never rendered as a zero: the endpoint raises rather than
          returning zeroed figures precisely so "the database was unreachable" cannot
          reach this screen as "$0.00 spent". */}
      {!isLoading && error && (
        <Card>
          <p className="text-sm text-red-700 dark:text-red-400" role="alert" data-testid="budget-error">
            Could not load your budget. These figures are unavailable — this is not a statement that your spend is zero.
          </p>
          <button
            type="button"
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            Retry
          </button>
        </Card>
      )}

      {!isLoading && !error && envelope && (
        <>
          {/* Read off the binding line, which is the cap that governs the caller. */}
          {envelope.binding?.enforcement_mode === 'shadow' && <ShadowModeBanner />}

          {/* `freshness` is always present on the wire, so it needs no null guard —
              but it is read defensively here because an older backend that omitted it
              would otherwise throw on the whole screen. */}
          {envelope.freshness?.cost_backfill_lag && <FreshnessNotice />}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-gray-500 dark:text-gray-400">
            <span>
              {envelope.period.period_start} to {envelope.period.period_end}
            </span>
            <span>
              Resets in {envelope.period.resets_in_days} {envelope.period.resets_in_days === 1 ? 'day' : 'days'}
            </span>
          </div>

          <HeadlineTiles envelope={envelope} />

          {/* `unresolved` means the cloud-agent ledger could not be looked up and is
              ABSENT from these figures — not that there is no cloud spend. */}
          {envelope.identity_status === 'unresolved' && (
            <p className="text-sm text-amber-800 dark:text-amber-300" data-testid="identity-unresolved">
              Your cloud-agent spend could not be looked up, so it is missing from the figures above. This is not a statement that it is zero.
            </p>
          )}

          <BudgetLines lines={envelope.lines} combined={envelope.combined_informational} />

          {/* Everything above describes ONE partition — the tenant this session is
              attributed to, which is the partition enforcement reads. These are all of
              them (#4626/#4646): a person whose runs execute outside their session's
              tenant reads `$0` above while real dollars accrue elsewhere. Both props are
              optional on the wire, and the component renders nothing when neither is
              present, so a response predating #4640 leaves this screen unchanged. */}
          <PerOrgSpend perOrg={envelope.per_org} personEnvelope={envelope.person_envelope} identityStatus={envelope.identity_status} />

          {/* Scope boundary, stated where the two surfaces meet (review fix): the
              runs endpoint is single-partition, so a reader of the cross-org card
              above must not go hunting for foreign-workspace runs below it. */}
          {(envelope.per_org?.length ?? 0) > 1 && (
            <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="runs-scope-note">
              The run list below covers this workspace only. Runs billed to your other workspaces are counted in the card above but are not listed
              here.
            </p>
          )}

          <BudgetRunsTable data={runs} isLoading={runsLoading} error={runsError} />
        </>
      )}

      {/* Issue #4629: the caller's own cross-org ceiling.
          Rendered OUTSIDE the envelope block on purpose, for two reasons. It is a
          separate fetch with its own loading and error states, so an outage on the
          org-scoped envelope must not hide the control a person uses to set their
          own limit. And it comes AFTER the figures above rather than before them:
          a pre-C4 `soft` limit is informational while the binding line
          above is what will actually stop them, so giving the soft figure visual
          primacy over the enforcing one would invert what a reader should act on. */}
      <PersonSpendingLimit period={period} />
    </div>
  );
}
