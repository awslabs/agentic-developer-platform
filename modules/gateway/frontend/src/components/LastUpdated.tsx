/**
 * Freshness caption for auto-polling views.
 *
 * Issue #4022: Agent Activity polls every 30 s, but a poll with no visible
 * tick reads as a static page — which is the very complaint the polling fix
 * addresses. This renders the "when did the data last change" signal plus a
 * subtle in-flight spinner.
 *
 * Shared rather than inline because it is a new pattern with no in-repo
 * precedent and several obvious next adopters: the live-streaming work
 * (#3951) and the four dashboards that already poll (PlatformDashboard,
 * OrgDashboard, /runs via useRunStats).
 *
 * Feed it straight from a react-query result:
 *
 *   const q = useQuery({ ..., refetchInterval: 30_000 });
 *   <LastUpdated dataUpdatedAt={q.dataUpdatedAt} isFetching={q.isFetching} />
 */

import { Spinner } from '@/components/ui';
import { formatRelativeTime, formatDateTime } from '@/utils/format';

export interface LastUpdatedProps {
  /**
   * react-query's `dataUpdatedAt` (epoch ms). `0` means "no successful fetch
   * yet", in which case only the spinner (if fetching) is rendered.
   */
  dataUpdatedAt: number;
  /** react-query's `isFetching` — drives the background-refresh spinner. */
  isFetching: boolean;
  /** Optional extra classes for the wrapper. */
  className?: string;
}

export function LastUpdated({ dataUpdatedAt, isFetching, className = '' }: LastUpdatedProps) {
  const hasFetched = dataUpdatedAt > 0;

  return (
    <div
      className={`flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 ${className}`}
      data-testid="last-updated"
    >
      {isFetching && <Spinner size="sm" className="h-3 w-3 border" />}
      {hasFetched && (
        <span title={formatDateTime(new Date(dataUpdatedAt))}>
          Updated {formatRelativeTime(new Date(dataUpdatedAt))}
        </span>
      )}
    </div>
  );
}

export default LastUpdated;
