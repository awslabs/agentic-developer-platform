/**
 * Live-refreshing delivery execution query (issue #5145).
 *
 * Mirrors `useFlowGraph`'s three binding settings for the same reasons — the
 * explicit `refetchInterval` (the app's global `staleTime` is five minutes, so a
 * query relying on cache invalidation shows five-minute-old state on a view whose
 * purpose is "where are we right now", and it *looks* live because the data is
 * real, just old); no `usePollingStatus` (that hook carries the dashboard's shared
 * cadence, and binding this view to it lets an unrelated tuning change silently
 * alter delivery freshness); and `placeholderData: keepPreviousData` as the **named
 * v5 import**, never the v4 `keepPreviousData: true` boolean, which type-errors
 * nowhere and is a silent no-op on the pinned 5.62.0 — every poll would blank the
 * panel to a spinner.
 *
 * **Two guarantees this hook adds beyond the graph query**, both required by the
 * issue and both invisible until they fail:
 *
 * 1. **A stale response cannot overwrite a newer one.** Poll responses are not
 *    ordered: a slow request issued at t=0 can land after a fast one issued at
 *    t=30s. React Query's own last-write-wins is by *arrival*, so the older payload
 *    would replace the newer and the panel would go backwards — showing a block
 *    that was already cleared, or "runnable" for work that has since stopped. Each
 *    execution row carries a `revision` that advances by exactly one per applied
 *    write, so `select`-time merging against the previous value makes this a
 *    comparison rather than a guess about arrival order. The merge is **per
 *    execution**, not per response: a response can legitimately be newer for one
 *    node and older for another.
 *
 * 2. **Polling stops on navigation.** `queryFn` forwards React Query's
 *    `AbortSignal` to the request, so unmounting the page aborts the in-flight
 *    fetch instead of leaving it running against a screen nobody is looking at, and
 *    `enabled` gates the whole query on having a flow id.
 */

import { useRef } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { getFlowExecution } from '@/services/orchestration';
import type { ExecutionSummary, FlowExecution } from '@/types/orchestration';

/**
 * Poll cadence, matched to the engine's dispatch pass rather than tuned for feel:
 * polling faster than the backend can change state adds load and returns the same
 * payload again. Deliberately the same 30s as the graph, so the two panels on one
 * screen cannot disagree about how current they are.
 */
export const FLOW_EXECUTION_REFETCH_INTERVAL_MS = 30_000;

/** Key an execution by its ledger identity: one row per node per cycle. */
function executionKey(execution: ExecutionSummary): string {
  return `${execution.node_id}::${execution.cycle}`;
}

/**
 * Keep the higher `revision` for every execution present in `incoming`.
 *
 * Exported for direct test, because the failure mode is silent: with the merge
 * removed everything still renders, just occasionally one poll out of date.
 *
 * Rows are only *carried forward* when the incoming response is older for that
 * key. A row absent from `incoming` is not resurrected — the page may legitimately
 * have moved, or the row may have been dropped as unreadable, and inventing it back
 * would show an execution the server did not report.
 */
export function mergeByRevision(
  previous: FlowExecution | undefined,
  incoming: FlowExecution
): FlowExecution {
  if (!previous) return incoming;
  const known = new Map(previous.executions.map((execution) => [executionKey(execution), execution]));
  let superseded = false;
  const executions = incoming.executions.map((execution) => {
    const held = known.get(executionKey(execution));
    if (held && held.revision > execution.revision) {
      superseded = true;
      return held;
    }
    return execution;
  });
  // Nothing was stale: return `incoming` itself so the identity is unchanged and
  // React Query's structural sharing is not defeated by a fresh object per poll.
  if (!superseded) return incoming;
  // `server_time` comes from the newer response even when some rows are carried
  // forward. It timestamps the *observation*, and a row's own `progressed_at` is
  // what ages it — so keeping the older stamp here would misdate the live rows.
  return { ...incoming, executions };
}

export function useFlowExecution(flowId: string | undefined) {
  // Held in a ref rather than read from the query cache inside `select`: `select`
  // runs on cached data too, so comparing against the cache would compare a value
  // with itself and never detect a regression.
  const latest = useRef<FlowExecution | undefined>(undefined);

  return useQuery<FlowExecution>({
    queryKey: ['orchestration', 'flow-execution', flowId],
    // The signal is React Query's, tied to this observer: unmounting the flow page
    // aborts the request in flight rather than letting it resolve into a component
    // that no longer exists.
    queryFn: ({ signal }) => getFlowExecution(flowId as string, {}, signal),
    enabled: Boolean(flowId),
    refetchInterval: FLOW_EXECUTION_REFETCH_INTERVAL_MS,
    // An operator who tabs away to GitHub and back expects current state on
    // arrival, not on the next tick. Plain `true` defers to `staleTime`, which is
    // five minutes here, and would do nothing.
    refetchOnWindowFocus: 'always',
    placeholderData: keepPreviousData,
    select: (data) => {
      const merged = mergeByRevision(latest.current, data);
      latest.current = merged;
      return merged;
    },
  });
}
