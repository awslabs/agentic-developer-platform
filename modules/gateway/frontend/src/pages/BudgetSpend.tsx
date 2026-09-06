/**
 * Budget & Spend — Issue #4402 (U-5 of EPIC #4324), restructured to two tiles by #4685.
 *
 * The screen the EPIC is named for: the one that turns a silent, punitive cap into a
 * visible budget. **It presents exactly ONE spend element** (the final #4669 ruling,
 * 2026-09-05, rendered in `components/budget/SpendTiles.tsx` as `MySpend`): "you can
 * spend $X; you've spent $Y" — the figure enforcement tracks, against the personal
 * limit. Direct use, the per-GitHub-org breakdown, other capped lines and the runs
 * list are drill-downs beneath it. That count is the contract: the page
 * previously carried five things that all read as "how much have I spent" — a binding-line
 * headline, a per-line list, a combined direct+cloud total, a per-workspace card with its own
 * cross-workspace total, and a separate personal-limit card — and the operator who designed
 * the underlying model could not tell which number governed them. Four were deleted rather
 * than hidden. Adding a third top-level figure here re-creates the ambiguity, whatever it is.
 *
 * What this page still owns is everything that is **not** a spend figure: the period
 * selector, and the three notices that qualify the figures in the tiles.
 *
 * Rules carried over unchanged from #4402, because each is a defect that shipped once:
 *
 * 1. **No band computed here.** Every band is read off the response, derived server-side
 *    from the same thresholds enforcement reads (80/95). `BudgetManagement.tsx` hardcodes
 *    a different 50/80 band; copying it would tell a user they are fine at 79% while the
 *    server has already warned.
 * 2. **No claim that spend will be stopped** while `enforcement_mode` is `shadow`. Caps
 *    are advisory in shadow mode, so that copy would simply be false — and a screen that
 *    threatens a consequence it cannot deliver is worse than no screen.
 * 3. **A backend failure is never rendered as a zero.** "We could not look" and "you spent
 *    nothing" are opposite claims, and the whole EPIC exists because they once rendered
 *    identically.
 *
 * Also not built, per the frozen rulings: the per-client-tool cost table and
 * device/session column (no data exists), the in-flight/reserved band and per-run/chain
 * cap gauges (no read API), and the "Preview as" role switcher (implies impersonation).
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@/components/ui';
import { MySpend } from '@/components/budget/SpendTiles';
import { getMyBudget } from '@/services/budgetSpend';
import { BUDGET_PERIOD_TYPES } from '@/types/budget';
import type { BudgetPeriodType } from '@/types/budget';

/** Display labels for the three calendar periods. */
const PERIOD_LABELS: Record<BudgetPeriodType, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
};

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

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Budget &amp; Spend</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Your spend for this period, and the limits it is measured against.</p>
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

          {/* `unresolved` means the cloud-agent ledger could not be looked up and is
              ABSENT from the figures — not that there is no cloud spend. Stated ABOVE the
              tiles, because it qualifies the Cloud spend figure inside one of them. */}
          {envelope.identity_status === 'unresolved' && (
            <p className="text-sm text-amber-800 dark:text-amber-300" data-testid="identity-unresolved">
              Your cloud-agent spend could not be looked up, so it is missing from the figures below. This is not a statement that it is zero.
            </p>
          )}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-gray-500 dark:text-gray-400">
            <span>
              {envelope.period.period_start} to {envelope.period.period_end}
            </span>
            <span>
              Resets in {envelope.period.resets_in_days} {envelope.period.resets_in_days === 1 ? 'day' : 'days'}
            </span>
          </div>

        </>
      )}

      {/* The page's entire spend surface: ONE card, mounted UNCONDITIONALLY (final
          #4669 ruling + review fix on #4686). Its own data — the personal limit —
          is a separate endpoint, and an outage on /me/budget must not hide the one
          control that can unblock a person whose hard limit is stopping runs. The
          envelope-dependent figure inside degrades to "could not be read". */}
      <MySpend envelope={envelope} period={period} />
    </div>
  );
}
