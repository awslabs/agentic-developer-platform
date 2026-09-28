/**
 * The one spend element — Issue #4685 (final #4669 ruling, 2026-09-05).
 *
 * `/budget` used to present five things that all looked like "how much have I spent",
 * then briefly two. The operator's final ruling is stricter than both: **a person sees
 * ONE number — "you can spend $X; you've spent $Y" — and enforcement tracks the same
 * number they see.** One card, one figure, one optional bar. Everything else — the
 * direct-use line, the per-GitHub-org breakdown, the runs list, whatever other limits
 * exist — is a drill-down beneath it, never a sibling.
 *
 * **The displayed number and the enforced number are the same number, at all times.**
 * Since #4396 that number is the person's TOTAL — their own direct use plus the agents
 * they triggered, across every workspace — carried by the SAME `person_envelope.spend_usd`
 * field and enforced against by the person layer. The fusion is entirely server-side, so
 * this card upgraded **by copy change only**: the caption now names both halves, because
 * a total described as "what your agents have spent" understates what the reader is
 * looking at. Nothing is summed here — a client-side total would be a number the server
 * does not enforce (the #4322 family, on the headline).
 *
 * The direct-use drill-down below stays, and stays labelled as GitHub-org-budget
 * territory: those lines are the per-org caps that govern that spend *within* one
 * workspace, which is a different denominator from the personal limit above and not the
 * same claim restated.
 *
 * Load-bearing rules, each a defect that has already shipped once on this page:
 *
 * 1. **This card mounts INDEPENDENT of the envelope query.** The personal limit is its
 *    own endpoint; an outage on `/me/budget` must never hide the one control that can
 *    unblock a person whose hard limit is stopping their runs (review fix on #4686).
 * 2. **Readability is `parseWireMoney`, never `== null`.** An empty-string figure must
 *    render the unreadable caveat and NO bar — not a dash beside "0% used".
 * 3. **A bar only when something enforces the denominator** (`enforcement_mode='hard'`),
 *    and the bar carries `aria-valuetext` with the true percentage: AT clamps
 *    `aria-valuenow` to `aria-valuemax`, which would hide exactly the overage the
 *    unclamped value exists to show.
 * 4. **No band is computed here.** The server publishes no cross-org band; inventing one
 *    from the two figures beside it would assert a threshold the server never stated.
 * 5. Absence is never `$0.00` — every figure goes through `formatWireMoney`.
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@/components/ui';
import { BudgetLines } from '@/components/budget/BudgetLines';
import { PerOrgSpend } from '@/components/budget/PerOrgSpend';
import { BudgetRunsTable } from '@/components/budget/BudgetRunsTable';
import { PersonSpendingLimit } from '@/components/budget/PersonSpendingLimit';
import { usePersonCap } from '@/hooks/usePersonCap';
import { getMyBudgetRuns } from '@/services/budgetSpend';
import { formatWireMoney, parseWireMoney } from '@/utils/cost';
import { WORKSPACE_TERM, WORKSPACE_TERM_PLURAL } from '@/utils/budgetVocabulary';
import type { BudgetEnvelopeResponse, BudgetPeriodType, PersonCapResponse } from '@/types/budget';

/** How the limit noun reads per period, matching `PersonSpendingLimit`. */
const PERIOD_NOUN: Record<BudgetPeriodType, string> = {
  daily: 'day',
  weekly: 'week',
  monthly: 'month',
};

/**
 * One collapsed drill-down beneath the headline.
 *
 * A native `<details>`, so "collapsed by default" is the element's own behaviour, and
 * the content stays in the accessibility tree while closed. `onToggle` exists for the
 * runs drill-down, whose query is deliberately lazy (a table nobody expanded is a
 * query nobody needed).
 */
function DrillDown({
  testId,
  label,
  children,
  onToggle,
}: {
  testId: string;
  label: string;
  children: React.ReactNode;
  onToggle?: (open: boolean) => void;
}) {
  return (
    <details className="group" data-testid={testId} onToggle={(e) => onToggle?.((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer text-sm font-medium text-primary-700 dark:text-primary-300 hover:underline">
        <span aria-hidden="true" className="inline-block mr-1 group-open:rotate-90 transition-transform">
          ▸
        </span>
        {label}
      </summary>
      <div className="mt-3">{children}</div>
    </details>
  );
}

/**
 * The denominator line and its bar.
 *
 * Four cap states, each a different claim:
 *
 *   - **`hard`** — the limit denies across every GitHub org: denominator + bar.
 *   - **`soft`** — a pre-C4 row: denominator, NO bar, "not enforced yet" caption
 *     (`PersonSpendingLimit` below carries the re-save instruction).
 *   - **uncapped** — figure stands alone; the affordance to set a limit is the editor's.
 *   - **unreadable spend** — no bar and no percentage: a position cannot be drawn for a
 *     figure that could not be read.
 */
function LimitDenominator({ cap, spendAmount, period }: { cap: PersonCapResponse; spendAmount: number | null; period: BudgetPeriodType }) {
  if (cap.cap_status !== 'capped' || cap.cap_usd == null) return null;

  const enforcing = cap.enforcement_mode === 'hard';
  const capAmount = parseWireMoney(cap.cap_usd);
  // A bar needs a positive denominator and a READABLE numerator (rule 2: parse
  // result, never `== null` — `Number('') === 0` would draw an honest-looking 0%).
  const drawable = enforcing && capAmount != null && capAmount > 0 && spendAmount != null;
  const pct = drawable ? (spendAmount / capAmount) * 100 : null;

  return (
    <>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400" data-testid="my-spend-limit">
        of {formatWireMoney(cap.cap_usd)} my limit
      </p>

      {pct != null && (
        <div
          role="progressbar"
          // Clamped for the ARIA numeric contract (AT stacks clamp out-of-range
          // values silently), with the TRUE figure in `aria-valuetext` so a
          // screen-reader user hears "166% of your limit", not a flattened 100%
          // (review fix on #4686). Sighted users read the same truth from the
          // figures; the bar fill is clamped because a track cannot overflow.
          aria-valuenow={Number(Math.min(100, Math.max(0, pct)).toFixed(1))}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuetext={`${pct.toFixed(1)}% of your personal limit used`}
          aria-label="Spend against my personal limit"
          className="mt-2 h-2 w-full rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden"
          data-testid="my-spend-bar"
        >
          <div className="h-full rounded-full bg-primary-500" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
      )}

      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400" data-testid="my-spend-caption">
        all {WORKSPACE_TERM_PLURAL} · {enforcing ? `enforcing, per ${PERIOD_NOUN[period]}` : 'not enforced yet'}
      </p>
    </>
  );
}

export interface MySpendProps {
  /** `undefined` while the envelope is loading or failed — the card still renders. */
  envelope: BudgetEnvelopeResponse | undefined;
  period: BudgetPeriodType;
}

/**
 * The page's entire spend surface: one card.
 *
 * The count is the contract — the test suite asserts on the NUMBER of headline spend
 * figures. A second figure added at this level re-creates the ambiguity the ruling
 * removed, whatever it is.
 */
export function MySpend({ envelope, period }: MySpendProps) {
  // Rule 1: this card's own data is the personal limit, fetched through the ONE
  // shared query identity — never a hand-copied queryKey (review fix on #4686).
  const { data: cap } = usePersonCap(period);

  const spendRaw = envelope?.person_envelope?.spend_usd ?? null;
  const spendAmount = parseWireMoney(spendRaw);

  // Lazy runs (review follow-up on #4686): the table sits behind a collapsed
  // drill-down, so its query fires on first expand, not on page load.
  const [runsOpen, setRunsOpen] = useState(false);
  const {
    data: runs,
    isLoading: runsLoading,
    error: runsError,
  } = useQuery({
    queryKey: ['myBudgetRuns', period],
    queryFn: () => getMyBudgetRuns({ period }),
    enabled: runsOpen,
  });

  // The by-GitHub-org drill-down never mounts onto nothing (review fix on #4686):
  // an expander opening an empty pane reads as "no cross-org spend" when the truth
  // may be "the response never spoke to it".
  const orgRows = envelope?.identity_status === 'unresolved' ? [] : (envelope?.per_org ?? []);

  return (
    <section aria-label="My spend" data-testid="my-spend">
      <Card>
        <h2 className="text-sm font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">My spend</h2>

        <p className="mt-2 text-3xl font-bold font-mono text-gray-900 dark:text-white" data-testid="my-spend-amount">
          {formatWireMoney(spendRaw)}
        </p>

        {/* Rule 2: gated on the PARSE result, so an empty-string figure gets this
            caveat (and no bar) rather than a dash beside a confident 0%. */}
        {spendAmount == null && (
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400" data-testid="my-spend-unreported">
            Your spend could not be read, so there is no figure to show. This is not a statement that it is zero.
          </p>
        )}

        {spendAmount != null && (
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            what you and your agents have spent, across all {WORKSPACE_TERM_PLURAL}
          </p>
        )}

        {cap && <LimitDenominator cap={cap} spendAmount={spendAmount} period={period} />}

        {/* The limit's editor and states. Mounted UNCONDITIONALLY (rule 1): its data
            is its own endpoint, and it must survive an envelope outage — it is the
            control that can unblock a person whose hard limit is stopping runs. */}
        <div className="mt-4">
          <PersonSpendingLimit period={period} />
        </div>

        {envelope && (
          <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 space-y-3">
            {orgRows.length > 0 && (
              <DrillDown testId="my-spend-drilldown-orgs" label={`by ${WORKSPACE_TERM}`}>
                <PerOrgSpend
                  perOrg={envelope.per_org}
                  personCapStatus={cap?.cap_status}
                  personCapEnforcing={cap?.enforcement_mode === 'hard'}
                  identityStatus={envelope.identity_status}
                />
              </DrillDown>
            )}

            {/* Direct use and any other capped lines (service principals included) —
                real money, governed by GitHub-org budgets rather than the personal
                limit, which is why it is a drill-down and not part of the headline
                figure (review fix on #4686: these lines can be enforced against the
                caller and must be findable somewhere). */}
            {envelope.lines.length > 0 && (
              <DrillDown testId="my-spend-drilldown-lines" label={`direct use & other lines (this ${WORKSPACE_TERM})`}>
                {/* An ancestor cap (team/department/org) can bind without appearing
                    in `lines` — when it does, the figure that will actually stop the
                    caller is stated here rather than nowhere (review fix on #4686). */}
                {envelope.binding && !envelope.lines.some((line) => line.entity_type === envelope.binding?.entity_type) && (
                  <p className="mb-3 text-xs text-amber-800 dark:text-amber-300" data-testid="binding-ancestor-note">
                    A shared {envelope.binding.label ?? envelope.binding.entity_type} budget also applies here
                    {envelope.binding.remaining_usd != null && <> — {formatWireMoney(envelope.binding.remaining_usd)} of it remains</>}. It can stop
                    requests in this {WORKSPACE_TERM} even when the figures above have headroom.
                  </p>
                )}
                <BudgetLines lines={envelope.lines} />
              </DrillDown>
            )}

            <DrillDown testId="my-spend-drilldown-runs" label="agent runs" onToggle={setRunsOpen}>
              <p className="mb-3 text-xs text-gray-500 dark:text-gray-400" data-testid="runs-scope-note">
                This list covers this {WORKSPACE_TERM} only. Runs billed to your other {WORKSPACE_TERM_PLURAL} are counted in the figure above but
                are not listed here.
              </p>
              <BudgetRunsTable data={runs} isLoading={runsOpen && runsLoading} error={runsError} />
            </DrillDown>
          </div>
        )}
      </Card>
    </section>
  );
}
