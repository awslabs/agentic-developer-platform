/**
 * The run drill-down — what actually spent the money — Issue #4402 (U-5).
 *
 * The envelope answers "how much have I spent". This answers "on what", from
 * `GET /me/budget/runs`.
 *
 * One idea here that the envelope does not have to carry: **a per-run cost may be
 * genuinely unknown**. A missing `budget_usage` row for a *period* is a true zero
 * (nothing settled yet). A missing `usage_logs` row for a *run* is different — the run
 * demonstrably exists and demonstrably did work, the ledger just has no row for it
 * yet, because cost back-fill is asynchronous. `$0.00` there says the work was free.
 * So every cost renders through `CostFigureBadge`/`formatCostFigure`, for which the
 * **status is authoritative over the number**: even a malformed
 * `{status: 'unknown', amount_usd: '0'}` renders `—`.
 *
 * `subtotal` and `total_run_count` describe **this page**, not the period, and are
 * labelled that way. A period-wide total is not available without walking every page,
 * and a page figure captioned as a period total is the class of wrong number this EPIC
 * exists to eliminate.
 */

import { Card } from '@/components/ui';
import { CostFigureBadge } from '@/components/shared/CostBadge';
import { describeStatus } from '@/utils/status';
import { formatRelativeTime } from '@/utils/format';
import type { BudgetRunItem, BudgetRunsResponse } from '@/types/budget';

/** How a run's attribution reads: which of the caller's lines it counts against. */
function AttributionBadge({ attribution }: { attribution: BudgetRunItem['attribution'] }) {
  const isCloud = attribution === 'cloud';
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
        isCloud
          ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200'
          : 'bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200'
      }`}
      data-testid="run-attribution"
      data-attribution={attribution}
    >
      {isCloud ? 'Cloud agent' : 'Direct'}
    </span>
  );
}

export interface BudgetRunsTableProps {
  data: BudgetRunsResponse | undefined;
  isLoading?: boolean;
  error?: unknown;
  /** Fetch the next page; omitted when there is nothing to page to. */
  onLoadMore?: () => void;
  isLoadingMore?: boolean;
}

export function BudgetRunsTable({ data, isLoading, error, onLoadMore, isLoadingMore }: BudgetRunsTableProps) {
  if (isLoading) {
    return (
      <Card>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Agent runs</h2>
        <div className="mt-4 space-y-2" data-testid="runs-loading">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-8 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
          ))}
        </div>
      </Card>
    );
  }

  if (error) {
    // A failed read is reported as a failure, never as an empty list: "we could not
    // look" and "you ran nothing" are opposite claims about the same screen.
    return (
      <Card>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Agent runs</h2>
        <p className="mt-4 text-sm text-red-700 dark:text-red-400" role="alert" data-testid="runs-error">
          Could not load your runs. This is not a statement that you have none — the list could not be read.
        </p>
      </Card>
    );
  }

  if (!data) return null;

  return (
    <Card>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Agent runs</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">The runs that contributed to the spend above, newest first.</p>
        </div>
        {/* Page-scoped, and captioned as such. */}
        <div className="text-right">
          <p className="text-xs text-gray-500 dark:text-gray-400">Subtotal for these {data.total_run_count} runs</p>
          <CostFigureBadge figure={data.subtotal} className="text-lg font-mono text-gray-900 dark:text-white" />
        </div>
      </div>

      {/* `unresolved` identity means chain-attributed runs could NOT be looked up and
          are ABSENT from the list — it must not be read as "no cloud runs". */}
      {data.identity_status === 'unresolved' && (
        <p className="mt-3 text-sm text-amber-800 dark:text-amber-300" data-testid="runs-identity-unresolved">
          Your cloud-agent runs could not be looked up, so they are missing from this list. This is not a statement that you have none.
        </p>
      )}

      {data.items.length === 0 ? (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400" data-testid="runs-empty">
          No agent runs recorded in this period.
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-800">
              <tr>
                <th scope="col" className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                  Started
                </th>
                <th scope="col" className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                  Persona
                </th>
                <th scope="col" className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                  Status
                </th>
                <th scope="col" className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                  Counts against
                </th>
                <th scope="col" className="px-3 py-2 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {data.items.map((item) => {
                const status = describeStatus(item.status);
                return (
                  <tr key={item.run_id} data-testid="budget-run-row">
                    <td className="px-3 py-2 text-sm text-gray-900 dark:text-white whitespace-nowrap">
                      {item.started_at ? formatRelativeTime(item.started_at) : '—'}
                    </td>
                    <td className="px-3 py-2 text-sm text-gray-900 dark:text-white">{item.persona ?? '—'}</td>
                    <td className={`px-3 py-2 text-sm whitespace-nowrap ${status.colorClass}`}>
                      <span aria-hidden="true">{status.glyph}</span> {status.label}
                    </td>
                    <td className="px-3 py-2 text-sm">
                      <AttributionBadge attribution={item.attribution} />
                    </td>
                    <td className="px-3 py-2 text-sm text-right">
                      <CostFigureBadge figure={item.cost} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* A non-null cursor with few or zero items is normal — filters are applied
          after the page read — so the control is offered whenever a cursor exists. */}
      {data.next_cursor && onLoadMore && (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={isLoadingMore}
          className="mt-4 px-3 py-1.5 text-sm rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
        >
          {isLoadingMore ? 'Loading…' : 'Load more runs'}
        </button>
      )}
    </Card>
  );
}
