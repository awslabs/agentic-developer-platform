/**
 * Live-refreshing flow graph query (issue #4212).
 *
 * **Three settings here are binding, not stylistic** (design-contract §3.3.1) —
 * "updating live" is an acceptance criterion, and each of these is a way to
 * silently not do that:
 *
 * 1. **`refetchInterval` is explicit.** The app sets a global
 *    `staleTime: 5 * 60 * 1000`. A query that relies on cache invalidation alone
 *    therefore shows five-minute-old state on a view whose entire purpose is
 *    answering "where are we right now" — and it *looks* live, because the data is
 *    real, just old. The contract forbids depending on `staleTime`, and a test
 *    asserts this interval at the hook level.
 *
 * 2. **`usePollingStatus` is deliberately not used.** That hook is for the
 *    dashboard's shared cadence; binding this view to it means an unrelated tuning
 *    change silently alters graph freshness. §3.3.1 makes this a MUST NOT, and a
 *    source-level test asserts the import is absent.
 *
 * 3. **`placeholderData: keepPreviousData`** — the named v5 import, not the v4
 *    `keepPreviousData: true` boolean, which is a **silent no-op** on the pinned
 *    5.62.0: it type-errors nowhere and just stops working, so every poll would
 *    blank the graph to a spinner. On a 30-second cadence that is a visible flash
 *    twice a minute.
 */

import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import { Permission } from '@/types';
import { getFlowGraph } from '@/services/orchestration';
import type { FlowGraph } from '@/types/orchestration';
import { useFlowPollInterval } from '@/hooks/useFlowPollInterval';

/**
 * Poll cadence. Matches the engine's dispatch pass rather than being tuned for
 * feel: polling faster than the backend can change state adds load and shows the
 * same payload again.
 */
export const FLOW_GRAPH_REFETCH_INTERVAL_MS = 30_000;

export function useFlowScope() {
  const { user, isLoading, hasPermission } = useAuth();
  return isLoading || !user ? null : [user.id, user.orgId, user.deptId, user.teamId, user.role,
    [...(user.permissions ?? [])].sort(), hasPermission?.(Permission.PLAN_APPROVE), hasPermission?.(Permission.PLAN_DRAFT)] as const;
}

export function useFlowGraph(flowId: string | undefined) {
  const scope = useFlowScope();
  const interval = useFlowPollInterval(FLOW_GRAPH_REFETCH_INTERVAL_MS);
  return useQuery<FlowGraph>({
    queryKey: ['orchestration', 'flow-graph', scope, flowId],
    queryFn: ({ signal }) => getFlowGraph(flowId as string, signal),
    enabled: Boolean(flowId && scope),
    refetchInterval: interval,
    refetchIntervalInBackground: false,
    // An operator who tabs away to GitHub and back expects current state on
    // arrival, not on the next tick. `'always'` refetches even while fresh —
    // plain `true` defers to `staleTime` and would do nothing here.
    refetchOnWindowFocus: 'always',
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] && JSON.stringify(previousQuery.queryKey[2]) === JSON.stringify(scope) &&
      previousQuery.queryKey[3] === flowId ? keepPreviousData(previous) : undefined,
  });
}
